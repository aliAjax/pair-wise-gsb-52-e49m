"""业务用例编排、权限检查与审计。"""
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, integer, month_value, text
from .repository import Repository
from .rules import (
    BATCH_RECALCULATING,
    BATCH_SETTLED,
    DomainRules,
    MonthlyRules,
    RecomputeFailure,
)


class Service:
    def __init__(
        self,
        repository: Repository,
        rules: DomainRules,
        audit: AuditRecorder = None,
        monthly: MonthlyRules = None,
        recomputer: Callable[..., Dict[str, Any]] = None,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.monthly = monthly or MonthlyRules()
        # 可注入故障的重算器，默认使用规则实现
        self._recompute = recomputer

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
        record = self.repository.get(record_id)
        record["batch_summary"] = self._batch_summary((record.get("payload") or {}).get("student_id", ""))
        return record

    def _batch_summary(self, student_id: str) -> Dict[str, Any]:
        batches = self.repository.list_batches(limit=1000)
        mine = [batch for batch in batches if batch["student_id"] == student_id]
        return {
            "total": len(mine),
            "settled": sum(1 for batch in mine if batch["state"] == BATCH_SETTLED),
            "recalculating": sum(1 for batch in mine if batch["state"] == BATCH_RECALCULATING),
        }

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
        self._after_plan_action(actor, action, data or {}, record, updated)
        return updated

    def _after_plan_action(self, actor: Actor, action: str, data: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any]) -> None:
        student_id = (after.get("payload") or {}).get("student_id", "")
        if not student_id:
            return
        # log_service 同步写入服务台账：重复导入键不重复计数
        if action == "log_service":
            month = data.get("month") or self.monthly.current_month()
            import_key = data.get("import_key")
            if not import_key:
                import_key = "log-%s-%s-v%s" % (after["id"], month, after["version"])
            self.repository.upsert_service_record(
                student_id=student_id,
                month=month,
                minutes=int(data["session_minutes"]),
                provider=data.get("provider", ""),
                import_key=str(import_key),
                source="plan_log",
                actor_id=actor.user_id,
            )
            self._recompute_if_open(actor, student_id, month, "service_logged")
        # 监护人同意或支持计划改动：已结算月份保留，未结算月份失效重算
        if action in {"consent", "amend"}:
            months = self.repository.mark_stale_unsettled(student_id, actor.user_id)
            for month in months:
                self._recompute_student_month(actor, student_id, month, trigger=action)

    def _recompute_if_open(self, actor: Actor, student_id: str, month: str, trigger: str) -> None:
        batch = self.repository.find_batch(student_id, month)
        if batch is not None and batch["state"] != BATCH_SETTLED:
            self._recompute_batch(actor, batch, trigger=trigger)

    # ---- 服务记录台账 ----

    def import_service_record(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        payload = payload or {}
        student_id = text(payload, "student_id")
        month = month_value(payload, "month")
        minutes = integer(payload, "minutes", 1)
        provider = text(payload, "provider")
        import_key = text(payload, "import_key")
        # UNIQUE(student_id, month, import_key) 保证重复导入只计数一次
        row = self.repository.upsert_service_record(
            student_id=student_id, month=month, minutes=minutes, provider=provider,
            import_key=import_key, source=payload.get("source", "import"), actor_id=actor.user_id,
        )
        row["duplicate"] = not row.get("inserted", False)
        if row.get("inserted"):
            batch = self.repository.find_batch(student_id, month)
            # 新台账并入已有批次；已结算月份结论保留，只重算未结算月份
            if batch is not None and batch["state"] != BATCH_SETTLED:
                self._recompute_batch(actor, batch, trigger="service_imported")
        return row

    def list_service_records(self, actor: Actor, student_id: str = None, month: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_service_records(student_id=student_id, month=month)

    # ---- 复核批次 ----

    def submit_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """提交复核批次：提交时固定依据；同一学生同月只留一个有效批次。

        两人同时提交只放一个批次：后到提交并入既有批次。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        payload = payload or {}
        student_id = text(payload, "student_id")
        month = month_value(payload, "month")
        plan = self.repository.eligible_plan(student_id)
        try:
            self.monthly.validate_basis(plan)
        except RecomputeFailure as exc:
            raise ValidationError(str(exc)) from exc
        basis = self.monthly.basis_snapshot(plan)
        batch_no = self.monthly.batch_number(student_id, month)
        existing = self.repository.find_batch(student_id, month)
        if existing is not None:
            # 同一学生同月只留一个有效批次：并入既有批次
            attached = self.repository.attach_service_records(existing["id"], student_id, month)
            self.repository.add_batch_entry(existing["id"], actor.user_id, "submit_merged", {
                "summary": "同月重复提交已并入批次%s" % existing["batch_no"], "attached": attached,
            })
            return self._recompute_batch(actor, existing, trigger="submit_merged")
        batch = self.repository.insert_batch(
            batch_no=batch_no, student_id=student_id, month=month,
            state=BATCH_RECALCULATING, basis=basis, result=None, actor_id=actor.user_id,
        )
        self.repository.add_batch_entry(batch["id"], actor.user_id, "submitted", {
            "summary": "批次已创建并固定依据", "basis": basis,
        })
        return self._recompute_batch(actor, batch, trigger="submitted")

    def list_batches(self, actor: Actor, state: str = None, stale_only: bool = False) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(state=state, stale_only=stale_only)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        batch["entries"] = self.repository.batch_entries(batch_id)
        batch["service_records"] = self.repository.service_records_for(batch["student_id"], batch["month"])
        return batch

    def retry_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        """重试仍在重算的批次：重算失败时保留上一版结论。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        if batch["state"] == BATCH_SETTLED:
            return batch
        return self._recompute_batch(actor, batch, trigger="retry")

    def _do_recompute(self, basis: Dict[str, Any], records: List[Dict[str, Any]], month: str) -> Dict[str, Any]:
        if self._recompute is not None:
            return self._recompute(basis, records, month)
        return self.monthly.recompute(basis, records, month)

    def _recompute_batch(self, actor: Actor, batch: Dict[str, Any], trigger: str) -> Dict[str, Any]:
        student_id, month = batch["student_id"], batch["month"]
        # stale 批次（计划/同意改动后失效）以当前计划刷新依据；非失效批次沿用提交时固定依据
        basis = batch["basis"]
        if batch.get("stale"):
            plan = self.repository.eligible_plan(student_id)
            try:
                self.monthly.validate_basis(plan)
                basis = self.monthly.basis_snapshot(plan)
            except RecomputeFailure as exc:
                self.repository.add_batch_entry(batch["id"], actor.user_id, "recompute_failed", {"trigger": trigger, "error": str(exc)})
                return self.repository.get_batch(batch["id"])
        self.repository.attach_service_records(batch["id"], student_id, month)
        records = self.repository.service_records_for(student_id, month)
        try:
            result = self._do_recompute(basis, records, month)
        except RecomputeFailure as exc:
            # 重算失败：保留上一版结论与依据，仅记录错误，等待重试该月
            saved = self.repository.save_batch_result(
                batch["id"], state=BATCH_RECALCULATING, basis=batch["basis"], result=batch["result"],
                error=str(exc), stale=bool(batch.get("stale")), actor_id=actor.user_id,
            )
            self.repository.add_batch_entry(batch["id"], actor.user_id, "recompute_failed", {
                "trigger": trigger, "error": str(exc), "previous_result_retained": batch["result"] is not None,
            })
            return self.repository.get_batch(batch["id"])
        status = BATCH_SETTLED if result.get("settled") else BATCH_RECALCULATING
        saved = self.repository.save_batch_result(
            batch["id"], state=status, basis=basis, result=result, error=None, stale=False, actor_id=actor.user_id,
        )
        self.repository.add_batch_entry(batch["id"], actor.user_id, "recomputed", {
            "trigger": trigger, "state": status, "conclusion": result.get("conclusion"),
        })
        return saved

    def _recompute_student_month(self, actor: Actor, student_id: str, month: str, trigger: str) -> None:
        batch = self.repository.find_batch(student_id, month)
        if batch is not None and batch["state"] != BATCH_SETTLED:
            self._recompute_batch(actor, batch, trigger=trigger)

    def settle_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        if batch["state"] != BATCH_SETTLED:
            batch = self._recompute_batch(actor, batch, trigger="settle")
        if batch["state"] == BATCH_SETTLED:
            return batch
        if batch.get("error"):
            raise ValidationError("批次仍重算失败，无法结算：%s" % batch["error"])
        result = dict(batch["result"] or {})
        result["settled"] = True
        saved = self.repository.save_batch_result(
            batch["id"], state=BATCH_SETTLED, basis=batch["basis"], result=result,
            error=None, stale=False, actor_id=actor.user_id,
        )
        self.repository.add_batch_entry(batch["id"], actor.user_id, "settled", {"summary": "月份结论已结算保留"})
        return saved

    def backfill_legacy_batches(self, actor: Actor) -> Dict[str, Any]:
        """旧数据缺批次号：按计划回填已完成月份的复核批次。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可回填旧数据批次号")
        now_month = self.monthly.current_month()
        created, skipped = [], []
        # 1) 台账中未归批次的已完成月份
        for group in self.repository.unbatched_service_groups(completed_only=True, now_month=now_month):
            student_id, month = group["student_id"], group["month"]
            plan = self.repository.eligible_plan(student_id)
            if plan is None or plan["state"] not in {"active", "under_review", "consented", "closed"}:
                skipped.append({"student_id": student_id, "month": month, "reason": "缺少可作为依据的计划"})
                continue
            self._backfill_one(plan, student_id, month, actor, created, skipped)
        # 2) 计划 payload 中的已交付分钟（旧数据）：回填到上一个已完成月份
        for plan in self.repository.list_records(limit=1000):
            payload = plan.get("payload") or {}
            student_id = payload.get("student_id")
            delivered = int(payload.get("delivered_minutes", 0) or 0)
            if not student_id or delivered <= 0:
                continue
            self._synthesize_legacy_service(plan, student_id, delivered, actor)
            month = self._previous_month(now_month)
            if self.repository.find_batch(student_id, month) is None:
                self._backfill_one(plan, student_id, month, actor, created, skipped)
        return {"created": created, "skipped": skipped, "now_month": now_month}

    def _synthesize_legacy_service(self, plan: Dict[str, Any], student_id: str, minutes: int, actor: Actor) -> None:
        month = self._previous_month(self.monthly.current_month())
        import_key = "legacy-plan-%s" % plan["id"]
        if self.repository.service_record_exists(student_id, month, import_key):
            return
        self.repository.upsert_service_record(
            student_id=student_id, month=month, minutes=minutes,
            provider=str((plan.get("payload") or {}).get("last_provider", "")),
            import_key=import_key, source="legacy", actor_id=actor.user_id,
        )

    def _backfill_one(self, plan: Dict[str, Any], student_id: str, month: str, actor: Actor, created: List, skipped: List) -> None:
        batch_no = self.monthly.batch_number(student_id, month)
        if self.repository.find_batch(student_id, month) is not None:
            return
        batch = self.repository.insert_batch(
            batch_no=batch_no, student_id=student_id, month=month,
            state=BATCH_RECALCULATING, basis=None, result=None, actor_id=actor.user_id,
        )
        self.repository.attach_service_records(batch["id"], student_id, month)
        records = self.repository.service_records_for(student_id, month)
        basis = self.monthly.basis_snapshot(plan)
        try:
            result = self._do_recompute(basis, records, month)
        except RecomputeFailure as exc:
            self.repository.save_batch_result(
                batch["id"], state=BATCH_RECALCULATING, basis=basis, result=None,
                error=str(exc), stale=False, actor_id=actor.user_id,
            )
            skipped.append({"student_id": student_id, "month": month, "reason": str(exc)})
            return
        # 回填只处理已完成月份：直接结算保留
        saved = self.repository.save_batch_result(
            batch["id"], state=BATCH_SETTLED, basis=basis, result=result, error=None, stale=False, actor_id=actor.user_id,
        )
        self.repository.add_batch_entry(batch["id"], actor.user_id, "backfilled", {"summary": "旧数据缺批次号，按计划回填完成月份"})
        created.append({"batch_no": saved["batch_no"], "student_id": student_id, "month": month})

    @staticmethod
    def _previous_month(month: str) -> str:
        year, mon = month.split("-")
        year, mon = int(year), int(mon)
        if mon == 1:
            return "%04d-12" % (year - 1)
        return "%04d-%02d" % (year, mon - 1)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
