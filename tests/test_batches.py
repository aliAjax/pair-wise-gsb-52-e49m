import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import BATCH_RECALCULATING, BATCH_SETTLED, MonthlyRules, RecomputeFailure, current_month


CM = Actor("cm", "case_manager")
PARENT = Actor("parent", "parent_rep")
SPECIALIST = Actor("sp", "specialist")
ADMIN = Actor("admin", "administrator")
SYSADMIN = Actor("system", "admin")

CREATE = {'student_id': 'S-1', 'disability': 'hearing', 'service_minutes': 100,
          'delivered_minutes': 0, 'review_due_days': 15, 'goals_count': 2, 'consent': False}


def make_plan(service, reference="IEP-1", data=None, actor_prefix=""):
    data = dict(CREATE, **(data or {}))
    plan = service.create(Actor("cm" + actor_prefix, "case_manager"), reference, data)
    plan = service.act(Actor("p" + actor_prefix, "parent_rep"), plan["id"], plan["version"], "consent",
                       {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
    plan = service.act(Actor("cm" + actor_prefix, "case_manager"), plan["id"], plan["version"], "activate", {})
    return plan


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_submit_fixes_basis_and_single_valid_batch(self):
        make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-10', 'minutes': 40,
            'provider': 'SP', 'import_key': 'K1'})
        batch = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
        self.assertEqual(batch['state'], BATCH_RECALCULATING)
        self.assertEqual(batch['basis']['plan_version'], 3)
        self.assertEqual(batch['result']['delivered_minutes'], 40)
        # 同一学生同月再次提交：并入同一批次，不新建
        again = self.service.submit_batch(Actor('cm2', 'case_manager'), {'student_id': 'S-1', 'month': '2026-10'})
        self.assertEqual(again['id'], batch['id'])
        actions = [row['action'] for row in self.service.repository.batch_entries(batch['id'])]
        self.assertIn('submit_merged', actions)
        groups = [b for b in self.service.list_batches(CM) if b['student_id'] == 'S-1' and b['month'] == '2026-10']
        self.assertEqual(len(groups), 1)

    def test_concurrent_submits_keep_one_batch(self):
        import threading
        make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-09', 'minutes': 50,
            'provider': 'SP', 'import_key': 'K1'})
        results, errors = [], []

        def submit(uid):
            try:
                results.append(self.service.submit_batch(Actor(uid, 'case_manager'),
                                                         {'student_id': 'S-1', 'month': '2026-09'}))
            except Exception as exc:  # pragma: no cover - 并发下也不应抛出
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=('u%d' % i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual({batch['id'] for batch in results}, {results[0]['id']})
        groups = [b for b in self.service.list_batches(CM) if b['student_id'] == 'S-1' and b['month'] == '2026-09']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['state'], BATCH_SETTLED)

    def test_settled_month_retained_open_month_invalidated(self):
        plan = make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-09', 'minutes': 60, 'provider': 'SP', 'import_key': 'K1'})
        settled = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-09'})
        self.assertEqual(settled['state'], BATCH_SETTLED)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-10', 'minutes': 40, 'provider': 'SP', 'import_key': 'K2'})
        open_batch = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
        self.assertEqual(open_batch['state'], BATCH_RECALCULATING)
        fixed_version = open_batch['basis']['plan_version']

        # 计划修订（监护人同意变化同理）：已结算保留，未结算失效重算
        plan = self.service.act(ADMIN, plan['id'], plan['version'], 'review', {'progress_note': '复盘'})
        plan = self.service.act(CM, plan['id'], plan['version'], 'amend',
                                {'amendment_reason': '调整目标', 'updated_goals': ['目标A', '目标B', '目标C']})

        kept = self.service.repository.find_batch('S-1', '2026-09')
        self.assertEqual(kept['state'], BATCH_SETTLED)
        self.assertEqual(kept['result']['delivered_minutes'], 60)
        self.assertEqual(kept['basis']['plan_version'], fixed_version)

        recalculated = self.service.repository.find_batch('S-1', '2026-10')
        self.assertEqual(recalculated['state'], BATCH_RECALCULATING)
        self.assertFalse(recalculated['stale'])
        self.assertEqual(recalculated['basis']['plan_version'], plan['version'])
        self.assertGreater(recalculated['basis']['plan_version'], fixed_version)

    def test_recompute_failure_keeps_previous_result_and_retry(self):
        make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-10', 'minutes': 40, 'provider': 'SP', 'import_key': 'K1'})
        batch = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
        previous = batch['result']

        calls = {'n': 0}

        def flaky(basis, records, month):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RecomputeFailure('模拟重算失败')
            return MonthlyRules().recompute(basis, records, month)

        self.service._recompute = flaky
        failed = self.service.retry_batch(CM, batch['id'])
        self.assertEqual(failed['error'], '模拟重算失败')
        # 保留上一版结论
        self.assertEqual(failed['result'], previous)
        self.assertEqual(failed['state'], BATCH_RECALCULATING)

        recovered = self.service.retry_batch(CM, batch['id'])
        self.assertIsNone(recovered['error'])
        self.assertEqual(recovered['result']['delivered_minutes'], 40)

    def test_settled_batch_is_not_retried(self):
        make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-09', 'minutes': 40, 'provider': 'SP', 'import_key': 'K1'})
        batch = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-09'})
        again = self.service.retry_batch(CM, batch['id'])
        self.assertEqual(again['id'], batch['id'])
        self.assertEqual(again['state'], BATCH_SETTLED)


class ServiceRecordDedupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        make_plan(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_import_not_double_counted(self):
        payload = {'student_id': 'S-1', 'month': '2026-10', 'minutes': 40, 'provider': 'SP', 'import_key': 'K1'}
        first = self.service.import_service_record(SPECIALIST, payload)
        second = self.service.import_service_record(SPECIALIST, dict(payload, minutes=999))
        self.assertTrue(first['inserted'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['minutes'], 40)
        rows = self.service.repository.service_records_for('S-1', '2026-10')
        self.assertEqual(len(rows), 1)

        batch = self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
        self.assertEqual(batch['result']['record_count'], 1)
        self.assertEqual(batch['result']['delivered_minutes'], 40)

    def test_log_service_writes_ledger_once(self):
        plan = self.service.repository.eligible_plan('S-1')
        self.service.act(SPECIALIST, plan['id'], plan['version'], 'log_service',
                         {'session_minutes': 25, 'provider': 'SP-9'})
        rows = self.service.repository.service_records_for('S-1', current_month())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['minutes'], 25)
        self.assertEqual(rows[0]['source'], 'plan_log')


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_plan_completed_month_backfilled(self):
        make_plan(self.service, reference='IEP-OLD', data={'delivered_minutes': 30})
        result = self.service.backfill_legacy_batches(SYSADMIN)
        previous = self.service._previous_month(current_month())
        batch = self.service.repository.find_batch('S-1', previous)
        self.assertIsNotNone(batch)
        self.assertEqual(batch['state'], BATCH_SETTLED)
        self.assertEqual(batch['result']['delivered_minutes'], 30)
        self.assertTrue(any(item['month'] == previous for item in result['created']))
        # 幂等：再次回填不产生重复批次
        self.service.backfill_legacy_batches(SYSADMIN)
        again = self.service.repository.find_batch('S-1', previous)
        self.assertEqual(again['id'], batch['id'])

    def test_legacy_unbatched_ledger_backfilled_with_batch_no(self):
        make_plan(self.service, reference='IEP-OLD')
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-08', 'minutes': 20, 'provider': 'SP', 'import_key': 'L1'})
        self.service.backfill_legacy_batches(SYSADMIN)
        batch = self.service.repository.find_batch('S-1', '2026-08')
        self.assertIsNotNone(batch)
        self.assertEqual(batch['state'], BATCH_SETTLED)
        for row in self.service.repository.service_records_for('S-1', '2026-08'):
            self.assertEqual(row['batch_id'], batch['id'])

    def test_backfill_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill_legacy_batches(CM)


class StatsAndRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_stats_flags_recalculating(self):
        make_plan(self.service)
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-09', 'minutes': 20, 'provider': 'SP', 'import_key': 'K1'})
        self.service.import_service_record(SPECIALIST, {
            'student_id': 'S-1', 'month': '2026-10', 'minutes': 20, 'provider': 'SP', 'import_key': 'K2'})
        self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-09'})
        self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
        stats = self.service.stats(CM)
        self.assertEqual(stats['batch_settled'], 1)
        self.assertEqual(stats['batch_recalculating'], 1)
        self.assertEqual(stats['batch_total'], 2)

    def test_submit_without_consent_rejected(self):
        plan = self.service.create(CM, 'IEP-X', CREATE)
        with self.assertRaises(ValidationError):
            self.service.submit_batch(CM, {'student_id': 'S-1', 'month': '2026-10'})
