"""特殊教育支持计划合规领域规则与状态转换。"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, ValidationError, boolean, integer, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'import_service': {'case_manager', 'specialist'}, 'submit_batch': {'case_manager', 'administrator'}, 'settle_batch': {'administrator'}, 'recalc_batch': {'case_manager', 'administrator'}, 'backfill_batches': {'administrator'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}

MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# 监护人同意或支持计划改动后，只有这些动作会让未结算月份失效重算。
INVALIDATING_ACTIONS = {'consent', 'amend'}


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def parse_month(value: Any) -> str:
    if not isinstance(value, str) or not MONTH_RE.match(value.strip()):
        raise ValidationError("月份必须是YYYY-MM格式")
    return value.strip()


def shift_month(month: str, offset: int) -> str:
    year = int(month[:4])
    zero_indexed = int(month[5:7]) - 1 + offset
    return "%04d-%02d" % (year + zero_indexed // 12, zero_indexed % 12 + 1)


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if "start_month" in p and p["start_month"] is not None:
            p["start_month"] = parse_month(p["start_month"])
        else:
            p["start_month"] = current_month()
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        # service_minutes为每月计划分钟数，累计delivered_minutes可覆盖多个完成月份（回填依赖该语义）。
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(min(int(p["delivered_minutes"]), int(p["service_minutes"])) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            session = integer(data, "session_minutes", 1)
            if session + int(p["delivered_minutes"]) > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            changes["delivered_minutes"] = int(p["delivered_minutes"]) + session
            changes["last_provider"] = text(data, "provider")
            changes["missing_minutes"] = int(p["service_minutes"]) - changes["delivered_minutes"]
            changes["compliance_rate"] = round(changes["delivered_minutes"] / int(p["service_minutes"]) * 100, 2)
            summary = "服务记录已登记"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 月度复核批次
    # ------------------------------------------------------------------
    def validate_service_entry(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        month = parse_month((data or {}).get("month"))
        start_month = str(record["payload"].get("start_month") or month)
        if month < start_month:
            raise ValidationError("服务月份不能早于计划起始月份")
        minutes = integer(data, "minutes", 1)
        provider = text(data, "provider")
        import_key = text(data, "import_key")
        return {"month": month, "minutes": minutes, "provider": provider, "import_key": import_key}

    def ensure_submittable(self, record: Dict[str, Any], month: str) -> None:
        start_month = str(record["payload"].get("start_month") or month)
        if month < start_month:
            raise ValidationError("复核月份不能早于计划起始月份")
        if not record["payload"].get("consent"):
            raise ValidationError("缺少监护人同意，不能提交复核批次")

    def build_basis(self, record: Dict[str, Any], month: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """提交时固定依据：此后计划/同意改动不会改变已快照的依据。"""
        payload = record["payload"]
        return {
            "month": month,
            "plan_version": int(record["version"]),
            "plan_reference": record["reference"],
            "consent": bool(payload.get("consent")),
            "consent_scope": payload.get("consent_scope", ""),
            "plan_status": payload.get("plan_status", record["state"]),
            "service_minutes": int(payload["service_minutes"]),
            "goals_count": int(payload.get("goals_count", 0)),
            "updated_goals": list(payload.get("updated_goals") or []),
            "service_entry_count": len(entries),
            "service_entry_ids": [int(entry["id"]) for entry in entries],
        }

    def compute_conclusion(self, basis: Dict[str, Any], entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """依据固定的依据快照与当月服务记录计算月度结论。"""
        planned = int(basis["service_minutes"])
        delivered = sum(int(entry["minutes"]) for entry in entries)
        if delivered > planned:
            raise ValidationError("当月服务记录合计%s分钟超过计划%s分钟" % (delivered, planned))
        rate = round(delivered / planned * 100, 2) if planned > 0 else 0.0
        return {
            "planned_minutes": planned,
            "delivered_minutes": delivered,
            "missing_minutes": planned - delivered,
            "compliance_rate": rate,
            "compliant": delivered >= planned,
            "service_entry_count": len(entries),
            "providers": sorted({str(entry["provider"]) for entry in entries}),
        }
