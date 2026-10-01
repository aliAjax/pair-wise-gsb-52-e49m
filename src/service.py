"""业务用例编排、权限检查与审计。"""
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import INVALIDATING_ACTIONS, DomainRules, parse_month, shift_month


Calculator = Callable[[Dict[str, Any], List[Dict[str, Any]]], Dict[str, Any]]


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 calculator: Calculator = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 可注入的结论计算器，默认走领域规则；测试可用来模拟重算失败后重试成功。
        self.calculator: Calculator = calculator or self.rules.compute_conclusion

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        # 监护人同意或支持计划改动后：已结算月份结论保留，未结算月份失效重算。
        if action in INVALIDATING_ACTIONS:
            invalidated = self.repository.mark_batches_recalculating(record_id, actor.user_id)
            if invalidated:
                self._retry_student(record_id, actor.user_id)
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        stats = dict(self.repository.stats())
        stats["batches"] = self.repository.batch_stats()
        stats["recalculating_batches"] = stats["batches"]["recalculating"]
        return stats

    # ------------------------------------------------------------------
    # 服务记录
    # ------------------------------------------------------------------
    def import_service(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "import_service"):
            raise PermissionDenied("角色无权导入服务记录")
        record = self.repository.get(record_id)
        entry_data = self.rules.validate_service_entry(record, data or {})
        entry, created = self.repository.import_service_entry(
            record_id=record_id,
            month=entry_data["month"],
            minutes=entry_data["minutes"],
            provider=entry_data["provider"],
            import_key=entry_data["import_key"],
            actor_id=actor.user_id,
        )
        entry["duplicate"] = not created
        return entry

    def list_services(self, actor: Actor, record_id: int, month: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        if month:
            month = parse_month(month)
        return self.repository.list_service_entries(record_id, month=month)

    # ------------------------------------------------------------------
    # 复核批次
    # ------------------------------------------------------------------
    def _current_basis_and_entries(self, record: Dict[str, Any], month: str):
        entries = self.repository.list_service_entries(int(record["id"]), month=month)
        basis = self.rules.build_basis(record, month, entries)
        return basis, entries

    def submit_batch(self, actor: Actor, record_id: int, month: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "submit_batch"):
            raise PermissionDenied("角色无权提交复核批次")
        month = parse_month(month)
        record = self.repository.get(record_id)
        self.rules.ensure_submittable(record, month)
        basis, entries = self._current_basis_and_entries(record, month)
        conclusion = self.calculator(basis, entries)
        # 同一学生同月只留一个有效批次：并发时第二个提交并入已有批次。
        batch, created = self.repository.submit_batch(
            record_id=int(record["id"]),
            student_id=str(record["payload"]["student_id"]),
            month=month,
            basis=basis,
            conclusion=conclusion,
            actor_id=actor.user_id,
        )
        batch["merged"] = not created
        return batch

    def list_batches(self, actor: Actor, student_id: Optional[str] = None, status: Optional[str] = None,
                     month: Optional[str] = None, recalculating_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(
            student_id=student_id, status=status, month=month,
            recalculating_only=recalculating_only, limit=limit,
        )

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        batch["recalculating"] = batch["status"] == "recalculating"
        return batch

    def batch_versions(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batch_versions(batch_id)

    def settle_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "settle_batch"):
            raise PermissionDenied("角色无权结算复核批次")
        return self.repository.settle_batch(batch_id, actor.user_id)

    def recalculate_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "recalc_batch"):
            raise PermissionDenied("角色无权重算复核批次")
        batch = self.repository.get_batch(batch_id)
        if batch["status"] == "settled":
            raise Conflict("已结算月份结论保留，不能重算")
        return self._recalculate_one(batch, actor.user_id)

    def _recalculate_one(self, batch: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        record = self.repository.get(int(batch["record_id"]))
        month = str(batch["month"])
        basis, entries = self._current_basis_and_entries(record, month)
        try:
            conclusion = self.calculator(basis, entries)
        except Exception as exc:  # 重算失败：保留上一版结论，仍标记重算中以便重试。
            return self.repository.record_recalculation_failure(int(batch["id"]), basis, str(exc), actor_id)
        return self.repository.apply_recalculation(int(batch["id"]), basis, conclusion, actor_id, "依据改动后重算")

    def _retry_student(self, record_id: int, actor_id: str) -> int:
        record = self.repository.get(record_id)
        student_id = str(record["payload"]["student_id"])
        pending = self.repository.batches_for_student(student_id, statuses={"recalculating"})
        for batch in pending:
            self._recalculate_one(batch, actor_id)
        return len(pending)

    # ------------------------------------------------------------------
    # 旧数据回填：缺批次号的完成月份按计划回填（已结算，后续改动不影响）
    # ------------------------------------------------------------------
    def backfill_status(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"batches": self.repository.batch_stats()}

    def backfill_batches(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "backfill_batches"):
            raise PermissionDenied("角色无权执行旧数据回填")
        created: List[str] = []
        skipped: List[str] = []
        for record in self.repository.list_records(limit=500):
            new_months, skipped_months = self._backfill_record(record)
            created.extend(new_months)
            skipped.extend(skipped_months)
        return {"created": created, "skipped": skipped, "created_count": len(created)}

    def _backfill_record(self, record: Dict[str, Any]):
        new_months: List[str] = []
        skipped_months: List[str] = []
        student_id = str(record["payload"]["student_id"])
        start_month = str(record["payload"].get("start_month") or "")
        entries = self.repository.list_service_entries(int(record["id"]))
        entries_by_month: Dict[str, Any] = {}
        for entry in entries:
            entries_by_month.setdefault(str(entry["month"]), []).append(entry)

        def fill(month: str, basis_entries: List[Dict[str, Any]]) -> None:
            basis = self.rules.build_basis(record, month, basis_entries)
            try:
                conclusion = self.calculator(basis, basis_entries)
            except Exception:
                skipped_months.append(month)
                return
            batch = self.repository.insert_backfilled_batch(
                record_id=int(record["id"]), student_id=student_id, month=month,
                basis=basis, conclusion=conclusion,
            )
            if batch is None:
                skipped_months.append(month)
            else:
                new_months.append(month)

        if entries_by_month:
            # 有服务记录：按月足额（完成）才回填。
            for month in sorted(entries_by_month):
                month_entries = entries_by_month[month]
                total = sum(int(item["minutes"]) for item in month_entries)
                if total >= int(record["payload"]["service_minutes"]):
                    fill(month, month_entries)
                else:
                    skipped_months.append(month)
        elif start_month and int(record["payload"].get("delivered_minutes", 0)) >= int(record["payload"]["service_minutes"]) and int(record["payload"]["service_minutes"]) > 0:
            # 旧数据无逐条记录：从计划起始月起按计划分钟数摊满的完成月份回填。
            delivered = int(record["payload"]["delivered_minutes"])
            planned = int(record["payload"]["service_minutes"])
            months_covered = delivered // planned
            for offset in range(max(months_covered, 0)):
                fill(shift_month(start_month, offset), [])
        return new_months, skipped_months
