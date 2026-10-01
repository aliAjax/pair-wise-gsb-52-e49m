"""特殊教育支持计划合规领域规则与状态转换。"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {'consent': {'parent_rep'}, 'activate': {'case_manager'}, 'log_service': {'case_manager', 'specialist'}, 'review': {'administrator'}, 'amend': {'case_manager'}, 'close': {'administrator'}}
TRANSITIONS = {'consent': {'draft': 'consented'}, 'activate': {'consented': 'active'}, 'log_service': {'active': 'active'}, 'review': {'active': 'under_review'}, 'amend': {'under_review': 'active'}, 'close': {'active': 'closed', 'under_review': 'closed'}}

# 复核批次状态：recalculating=仍在重算（含尚未结算的月份结论），settled=已结算月份结论保留
BATCH_RECALCULATING = "recalculating"
BATCH_SETTLED = "settled"
# 已结算（月份早于当前月）的批次不再随计划/同意改动失效
BASIS_STATES = {"active", "under_review", "consented"}


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
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
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


def current_month(now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def month_key(value: str) -> Tuple[int, int]:
    year, month = value.split("-")
    return int(year), int(month)


class RecomputeFailure(Exception):
    """重算失败：保留上一版结论并记录错误，等待后续重试。"""


class MonthlyRules:
    """复核批次的依据固定、月份结算与结论重算规则。"""

    BATCH_RECALCULATING = BATCH_RECALCULATING
    BATCH_SETTLED = BATCH_SETTLED

    def __init__(self, clock=None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def current_month(self) -> str:
        return current_month(self.clock())

    @staticmethod
    def is_settled(month: str, now_month: str = None) -> bool:
        now_month = now_month or current_month()
        return month_key(month) < month_key(now_month)

    @staticmethod
    def batch_number(student_id: str, month: str) -> str:
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", student_id.strip())
        return "RB-%s-%s" % (safe, month)

    @staticmethod
    def basis_snapshot(plan: Dict[str, Any]) -> Dict[str, Any]:
        """提交时固定依据：之后计划/同意改动不影响已固定的批次。"""
        payload = plan.get("payload") or {}
        return {
            "plan_id": plan["id"],
            "plan_version": int(plan["version"]),
            "student_id": payload.get("student_id", ""),
            "plan_state": plan.get("state", ""),
            "service_minutes": int(payload.get("service_minutes", 0)),
            "goals_count": int(payload.get("goals_count", 0)),
            "consent": bool(payload.get("consent")),
            "consent_scope": payload.get("consent_scope", ""),
        }

    def validate_basis(self, plan: Dict[str, Any]) -> None:
        if plan is None:
            raise RecomputeFailure("缺少支持计划，无法确定计算依据")
        if plan.get("state") not in BASIS_STATES:
            raise RecomputeFailure("支持计划当前状态不可作为复核依据")
        payload = plan.get("payload") or {}
        if not payload.get("consent"):
            raise RecomputeFailure("缺少监护人同意，无法确定计算依据")
        if int(payload.get("service_minutes", 0)) <= 0:
            raise RecomputeFailure("计划服务分钟数无效")

    def recompute(self, basis: Dict[str, Any], service_records: Iterable[Dict[str, Any]], month: str, settle: bool = None) -> Dict[str, Any]:
        """按固定依据与该学生该月台账汇总重算结论。台账已去重，不会重复计数。"""
        if not basis:
            raise RecomputeFailure("批次缺少固定依据")
        records = list(service_records)
        delivered = sum(int(row["minutes"]) for row in records)
        planned = int(basis.get("service_minutes", 0))
        if planned <= 0:
            raise RecomputeFailure("计划服务分钟数无效")
        if delivered > planned:
            raise RecomputeFailure("台账服务分钟数%d超过计划%d，需先核对" % (delivered, planned))
        compliant = delivered >= planned
        now_month = self.current_month()
        settled = self.is_settled(month, now_month) if settle is None else bool(settle)
        return {
            "month": month,
            "delivered_minutes": delivered,
            "service_minutes": planned,
            "missing_minutes": max(0, planned - delivered),
            "compliance_rate": round(delivered / planned * 100, 2),
            "compliant": compliant,
            "conclusion": "达标" if compliant else "未达标",
            "record_count": len(records),
            "settled": settled,
        }

    def status_for(self, month: str) -> str:
        return BATCH_SETTLED if self.is_settled(month) else BATCH_RECALCULATING
