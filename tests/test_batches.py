import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError

def make_data(**overrides):
    data = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600,
            'delivered_minutes': 0, 'review_due_days': 15, 'goals_count': 4,
            'consent': False, 'start_month': '2026-01'}
    data.update(overrides)
    return data


CM = Actor("cm", "case_manager")
PARENT = Actor("parent", "parent_rep")
SPECIALIST = Actor("sp", "specialist")
ADMIN = Actor("admin", "administrator")


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _active_plan(self, student_id='S-200'):
        record = self.service.create(CM, "IEP-%s" % student_id, make_data(student_id=student_id))
        record = self.service.act(PARENT, record["id"], record["version"], "consent",
                                  {'guardian_confirmed': True, 'consent_scope': '个别化服务'})
        record = self.service.act(CM, record["id"], record["version"], "activate", {})
        return record

    def test_submit_fixes_basis_and_settled_month_is_retained(self):
        record = self._active_plan()
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 600, 'provider': 'SP-3', 'import_key': 'k-1'})
        batch = self.service.submit_batch(CM, record["id"], "2026-08")
        self.assertEqual(batch["status"], "submitted")
        self.assertFalse(batch["merged"])
        self.assertEqual(batch["basis"]["plan_version"], record["version"])
        self.assertEqual(batch["conclusion"]["delivered_minutes"], 600)
        self.assertTrue(batch["conclusion"]["compliant"])

        # 结算后的月份结论固定保留。
        settled = self.service.settle_batch(ADMIN, batch["id"])
        self.assertEqual(settled["status"], "settled")

        # 另一个未结算月份。
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-09', 'minutes': 300, 'provider': 'SP-3', 'import_key': 'k-2'})
        open_batch = self.service.submit_batch(CM, record["id"], "2026-09")
        self.assertFalse(open_batch["conclusion"]["compliant"])

        # 修订计划：进入复查再修订。
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {'progress_note': '复查'})
        record = self.service.act(CM, record["id"], record["version"], "amend",
                                  {'amendment_reason': '调整目标', 'updated_goals': ['目标X']})

        settled_after = self.service.get_batch(ADMIN, batch["id"])
        self.assertEqual(settled_after["status"], "settled")
        self.assertEqual(settled_after["conclusion"]["delivered_minutes"], 600)
        self.assertEqual(settled_after["basis"]["plan_version"], 3)

        open_after = self.service.get_batch(ADMIN, open_batch["id"])
        # 未结算月份已按新依据重算（默认计算器立即成功）。
        self.assertEqual(open_after["status"], "submitted")
        self.assertEqual(open_after["basis"]["plan_version"], record["version"])
        self.assertGreater(record["version"], 3)

        # 已结算批次不能重算。
        with self.assertRaises(Conflict):
            self.service.recalculate_batch(CM, batch["id"])

    def test_same_student_same_month_only_one_batch_with_merge(self):
        record = self._active_plan(student_id='S-300')
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 200, 'provider': 'SP-3', 'import_key': 'k-1'})
        first = self.service.submit_batch(CM, record["id"], "2026-08")
        second = self.service.submit_batch(Actor("cm2", "case_manager"), record["id"], "2026-08")
        self.assertTrue(second["merged"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["submitters"], ["cm", "cm2"])

        # 同学生同月只有一个有效批次。
        batches = self.service.list_batches(ADMIN, student_id='S-300')
        self.assertEqual(len(batches), 1)

        # 重复并入不再追加提交人。
        third = self.service.submit_batch(CM, record["id"], "2026-08")
        self.assertTrue(third["merged"])
        self.assertEqual(third["submitters"], ["cm", "cm2"])

    def test_concurrent_submits_result_in_single_batch(self):
        record = self._active_plan(student_id='S-400')
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 200, 'provider': 'SP-3', 'import_key': 'k-1'})
        barrier = threading.Barrier(2)
        results = []

        def submit(user_id):
            barrier.wait()
            results.append(self.service.submit_batch(Actor(user_id, "case_manager"), record["id"], "2026-08"))

        t1 = threading.Thread(target=submit, args=("cm-a",))
        t2 = threading.Thread(target=submit, args=("cm-b",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        # 两个提交并入同一批次；提交人在落库后的批次上聚合。
        persisted = self.service.get_batch(ADMIN, results[0]["id"])
        self.assertEqual(sorted(persisted["submitters"]), ["cm-a", "cm-b"])
        self.assertEqual(len(self.service.list_batches(ADMIN, student_id='S-400')), 1)

    def test_duplicate_service_import_is_not_double_counted(self):
        record = self._active_plan(student_id='S-500')
        payload = {'month': '2026-08', 'minutes': 250, 'provider': 'SP-3', 'import_key': 'dup-1'}
        first = self.service.import_service(SPECIALIST, record["id"], payload)
        self.assertFalse(first["duplicate"])
        second = self.service.import_service(SPECIALIST, record["id"], payload)
        self.assertTrue(second["duplicate"])

        entries = self.service.list_services(ADMIN, record["id"], month="2026-08")
        self.assertEqual(len(entries), 1)

        batch = self.service.submit_batch(CM, record["id"], "2026-08")
        self.assertEqual(batch["conclusion"]["delivered_minutes"], 250)
        self.assertEqual(batch["conclusion"]["service_entry_count"], 1)

    def test_recalculation_failure_keeps_previous_version_then_retry_succeeds(self):
        record = self._active_plan(student_id='S-600')
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 200, 'provider': 'SP-3', 'import_key': 'k-1'})
        batch = self.service.submit_batch(CM, record["id"], "2026-08")
        original_conclusion = dict(batch["conclusion"])

        # 让重算先失败一次：服务分钟数超过计划。
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 500, 'provider': 'SP-4', 'import_key': 'k-2'})
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {'progress_note': '复查'})
        record = self.service.act(CM, record["id"], record["version"], "amend",
                                  {'amendment_reason': '调整', 'updated_goals': ['G']})

        failed = self.service.get_batch(ADMIN, batch["id"])
        self.assertEqual(failed["status"], "recalculating")
        self.assertTrue(failed["recalculating"])
        self.assertIsNotNone(failed["error"])
        # 上一版结论保留。
        self.assertEqual(failed["conclusion"]["delivered_minutes"], original_conclusion["delivered_minutes"])

        # 修正数据后重试该月，成功生成新版本。
        with self.service.repository._connect() as connection:
            connection.execute("UPDATE service_entries SET minutes=100 WHERE import_key='k-2'")
            connection.commit()
        retried = self.service.recalculate_batch(CM, batch["id"])
        self.assertEqual(retried["status"], "submitted")
        self.assertIsNone(retried["error"])
        self.assertEqual(retried["conclusion"]["delivered_minutes"], 300)

        versions = self.service.batch_versions(ADMIN, batch["id"])
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["conclusion"]["delivered_minutes"], 200)

    def test_backfill_completed_months_for_legacy_data(self):
        # 旧记录：没有逐条服务记录，仅有累计分钟数，1200/600 覆盖两个完成月份。
        record = self.service.create(CM, "IEP-LEGACY-1", make_data(student_id='S-700', delivered_minutes=1200))
        result = self.service.backfill_batches(ADMIN)
        self.assertEqual(sorted(result["created"]), ["2026-01", "2026-02"])
        batches = self.service.list_batches(ADMIN, student_id='S-700')
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(b["status"] == "settled" and b["backfilled"] for b in batches))
        self.assertTrue(all(b["batch_no"] for b in batches))

        # 回填幂等：再次执行不产生重复批次号。
        again = self.service.backfill_batches(ADMIN)
        self.assertEqual(again["created_count"], 0)
        self.assertEqual(len(self.service.list_batches(ADMIN, student_id='S-700')), 2)

        # 回填的已结算月份在同意/计划改动后仍然保留。
        record = self.service.act(PARENT, record["id"], record["version"], "consent",
                                  {'guardian_confirmed': True, 'consent_scope': '范围'})
        for batch in self.service.list_batches(ADMIN, student_id='S-700'):
            self.assertEqual(batch["status"], "settled")

    def test_backfill_from_service_entries_only_for_completed_months(self):
        record = self._active_plan(student_id='S-800')
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-03', 'minutes': 600, 'provider': 'SP-3', 'import_key': 'a'})
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-04', 'minutes': 300, 'provider': 'SP-3', 'import_key': 'b'})
        result = self.service.backfill_batches(ADMIN)
        self.assertEqual(result["created"], ["2026-03"])
        self.assertIn("2026-04", result["skipped"])

    def test_stats_flags_recalculating_batches(self):
        record = self._active_plan(student_id='S-900')
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-08', 'minutes': 900, 'provider': 'SP-3', 'import_key': 'k-1'})
        # 900 > 600 会导致计算失败；先直接插一个有效批次再制造重算失败。
        record = self.service.repository.get(record["id"])
        # 通过正常流程先提交（200分钟）。
        with self.service.repository._connect() as connection:
            connection.execute("UPDATE service_entries SET minutes=200 WHERE import_key='k-1'")
            connection.commit()
        batch = self.service.submit_batch(CM, record["id"], "2026-08")
        with self.service.repository._connect() as connection:
            connection.execute("UPDATE service_entries SET minutes=900 WHERE import_key='k-1'")
            connection.commit()
        record = self.service.act(ADMIN, record["id"], record["version"], "review", {'progress_note': 'x'})
        self.service.act(CM, record["id"], record["version"], "amend",
                         {'amendment_reason': 'r', 'updated_goals': ['G']})
        stats = self.service.stats(ADMIN)
        self.assertEqual(stats["recalculating_batches"], 1)
        self.assertEqual(stats["batches"]["recalculating"], 1)

        flagged = self.service.list_batches(ADMIN, recalculating_only=True)
        self.assertEqual([b["id"] for b in flagged], [batch["id"]])

    def test_permissions_and_month_validation(self):
        record = self.service.create(CM, "IEP-X1", make_data(student_id='S-950'))
        with self.assertRaises(PermissionDenied):
            self.service.import_service(PARENT, record["id"],
                                        {'month': '2026-02', 'minutes': 10, 'provider': 'p', 'import_key': 'k'})
        with self.assertRaises(ValidationError):
            self.service.import_service(CM, record["id"],
                                        {'month': '2026-13', 'minutes': 10, 'provider': 'p', 'import_key': 'k'})
        with self.assertRaises(ValidationError):
            self.service.import_service(CM, record["id"],
                                        {'month': '2020-12', 'minutes': 10, 'provider': 'p', 'import_key': 'k'})
        # 未同意不能提交批次。
        with self.assertRaises(ValidationError):
            self.service.submit_batch(CM, record["id"], "2026-02")
        # specialist 不能提交批次。
        record = self.service.act(PARENT, record["id"], record["version"], "consent",
                                  {'guardian_confirmed': True, 'consent_scope': '范围'})
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch(SPECIALIST, record["id"], "2026-02")
        # case_manager 不能结算。
        record = self.service.act(CM, record["id"], record["version"], "activate", {})
        self.service.import_service(CM, record["id"],
                                    {'month': '2026-02', 'minutes': 10, 'provider': 'p', 'import_key': 'k'})
        batch = self.service.submit_batch(CM, record["id"], "2026-02")
        with self.assertRaises(PermissionDenied):
            self.service.settle_batch(CM, batch["id"])


if __name__ == "__main__":
    unittest.main()
