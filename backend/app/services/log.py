"""采集日志业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "log"
REQUIRED_FIELDS = ["日志编号", "关联任务", "开始时间"]
STATUS_ORDER = ["成功", "部分失败", "完全失败"]
# 状态类动作：只允许沿 STATUS_ORDER 向前推进，不允许回退改写历史结论
ACTION_RULES = {"标记已读": "成功", "归档日志": "完全失败"}
NEGATIVE_ACTIONS: list[str] = []
# 重试动作：不再改写原记录，而是生成独立的重试执行日志
RETRY_ACTION = "触发重试"
RETRYABLE_STATUSES = ["部分失败", "完全失败"]
# 重试记录与原日志的关联字段；既有日志记录没有这两个字段，按普通日志处理
RETRY_SOURCE_NO = "重试来源"
RETRY_SOURCE_ID = "来源日志ID"


class LogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        task: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if task:
            rows = [row for row in rows if task in str(row.get("关联任务", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"执行日志 {entry_id} 不存在或已归档"
        if action == RETRY_ACTION:
            return self._trigger_retry(entry)
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集日志可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or "")
        if current in STATUS_ORDER and STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
            return None, (
                f"状态只能按{'→'.join(STATUS_ORDER)}推进，"
                f"不允许从「{current}」回退到「{target}」"
            )
        if current == target:
            return entry, f"执行日志已处于「{target}」状态，本次不重复变更"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"执行日志已{action}"

    def _trigger_retry(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """触发重试：生成独立的重试执行日志，原记录作为历史证据原样保留。

        - 原日志的结束时间、成功条数、失败条数、错误信息与采集行数一律不改；
        - 重试记录通过「重试来源 / 来源日志ID」关联原日志，关联任务保持一致，
          日志编号在原编号后追加 -R1，按编号或关联任务都能检索到；
        - 同一失败日志重复触发只落一份重试结果，后续提交直接返回已有记录。
        """
        status = str(entry.get("status") or "")
        if status not in RETRYABLE_STATUSES:
            return None, f"当前状态为「{status}」，仅{'、'.join(RETRYABLE_STATUSES)}的日志可以触发重试"
        source_no = str(entry.get("日志编号") or "").strip()
        if not source_no:
            return None, "原日志缺少日志编号，无法生成重试记录"
        source_id = int(entry.get("id", 0))
        rows = store.rows(MODULE)
        for row in rows:
            if int(row.get(RETRY_SOURCE_ID) or 0) == source_id:
                return row, f"已存在重试记录 {row.get('日志编号')}，同一失败日志只落一份重试结果"
        record = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "日志编号": f"{source_no}-R1",
            "关联任务": entry.get("关联任务"),
            "开始时间": date.today().isoformat(),
            "结束时间": "",
            "采集行数": "0",
            "成功条数": "0",
            "失败条数": "0",
            "错误信息": "",
            RETRY_SOURCE_NO: source_no,
            RETRY_SOURCE_ID: source_id,
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        rows.append(record)
        self._sync_task(entry, record)
        self._append_audit(entry, record)
        return record, (
            f"已生成独立重试记录 {record['日志编号']}，"
            f"原日志 {source_no} 的采集行数与错误信息保持原样"
        )

    def _sync_task(self, source: dict[str, Any], record: dict[str, Any]) -> None:
        """任务工作台跟随同一次变更：关联任务重新进入待执行，不停留在旧结论。"""
        task_key = str(source.get("关联任务") or "").strip()
        if not task_key:
            return
        for task in store.rows("task"):
            names = {str(task.get("任务编号") or ""), str(task.get("任务名称") or "")}
            if task_key in names:
                task["status"] = "待执行"
                task["pending"] = True

    def _append_audit(self, source: dict[str, Any], record: dict[str, Any]) -> None:
        """审计跟随重试变更：登记变更前后结论，历史日志不被覆盖。"""
        rows = store.rows("audit")
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({
            "id": next_id,
            "审计编号": f"AUDI-{next_id:04d}",
            "操作类型": RETRY_ACTION,
            "操作对象": str(source.get("日志编号") or ""),
            "操作人": "系统",
            "操作时间": date.today().isoformat(),
            "变更前": f"{source.get('日志编号')}：{source.get('status')}，失败条数 {source.get('失败条数')}",
            "变更后": f"生成独立重试记录 {record.get('日志编号')}，状态 {record.get('status')}",
            "审计备注": "原日志保留结束时间、成功条数、失败条数与错误信息作为历史证据",
            "status": "已记录",
            "pending": True,
            "abnormal": False,
        })
