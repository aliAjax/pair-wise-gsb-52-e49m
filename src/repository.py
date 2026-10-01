"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    month TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    import_key TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, import_key)
                );
                CREATE TABLE IF NOT EXISTS review_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    student_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    status TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    conclusion TEXT,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    submitters TEXT NOT NULL,
                    backfilled INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(student_id, month)
                );
                CREATE TABLE IF NOT EXISTS batch_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES review_batches(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_entries_record_month ON service_entries(record_id, month);
                CREATE INDEX IF NOT EXISTS idx_batches_record ON review_batches(record_id);
                CREATE INDEX IF NOT EXISTS idx_batches_student_status ON review_batches(student_id, status);
                CREATE INDEX IF NOT EXISTS idx_batch_versions_batch ON batch_versions(batch_id, sequence);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # 服务记录（重复导入按record_id+import_key去重，不重复计数）
    # ------------------------------------------------------------------
    def import_service_entry(self, record_id: int, month: str, minutes: int, provider: str, import_key: str, actor_id: str) -> Tuple[Dict[str, Any], bool]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM service_entries WHERE record_id=? AND import_key=?",
                (record_id, import_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing), False
            cursor = connection.execute(
                "INSERT INTO service_entries(record_id,month,minutes,provider,import_key,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, month, int(minutes), provider, import_key, actor_id, now),
            )
            entry = connection.execute("SELECT * FROM service_entries WHERE id=?", (int(cursor.lastrowid),)).fetchone()
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "import_service", actor_id,
                 int(connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()["version"]),
                 json.dumps({"month": month, "minutes": int(minutes), "import_key": import_key, "provider": provider}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return dict(entry), True

    def list_service_entries(self, record_id: int, month: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if month:
                rows = connection.execute(
                    "SELECT * FROM service_entries WHERE record_id=? AND month=? ORDER BY id",
                    (record_id, month),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM service_entries WHERE record_id=? ORDER BY month, id", (record_id,)
                ).fetchall()
        return [dict(row) for row in rows]

    def sum_service_entry_minutes(self, record_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(minutes),0) AS total FROM service_entries WHERE record_id=?", (record_id,)
            ).fetchone()
        return int(row["total"])

    # ------------------------------------------------------------------
    # 复核批次
    # ------------------------------------------------------------------
    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"])
        item["conclusion"] = json.loads(item["conclusion"]) if item.get("conclusion") else None
        item["submitters"] = json.loads(item["submitters"])
        item["backfilled"] = bool(item["backfilled"])
        return item

    def _next_batch_no(self, connection: sqlite3.Connection) -> str:
        row = connection.execute("SELECT COUNT(*) AS total FROM review_batches").fetchone()
        return "RB%06d" % (int(row["total"]) + 1)

    def submit_batch(self, record_id: int, student_id: str, month: str, basis: Dict[str, Any],
                     conclusion: Dict[str, Any], actor_id: str) -> Tuple[Dict[str, Any], bool]:
        """同一学生同月只有一个有效批次：并发提交时后到者并入已有批次。

        返回(批次, 是否新建)。UNIQUE(student_id, month)是最终防线。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM review_batches WHERE student_id=? AND month=?",
                (student_id, month),
            ).fetchone()
            if existing is not None:
                submitters = json.loads(existing["submitters"])
                merged = actor_id not in submitters
                if merged:
                    submitters.append(actor_id)
                    connection.execute(
                        "UPDATE review_batches SET submitters=?, updated_at=? WHERE id=?",
                        (json.dumps(submitters, ensure_ascii=False), now, int(existing["id"])),
                    )
                    self._append_batch_audit(connection, record_id, "batch_merged", actor_id, int(existing["id"]),
                                             {"batch_no": existing["batch_no"], "submitters": submitters}, now)
                result = connection.execute("SELECT * FROM review_batches WHERE id=?", (int(existing["id"]),)).fetchone()
                connection.commit()
                return self._batch_row(result), False
            batch_no = self._next_batch_no(connection)
            submitters = [actor_id]
            cursor = connection.execute(
                "INSERT INTO review_batches(batch_no,record_id,student_id,month,status,basis,conclusion,error,attempts,submitters,backfilled,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_no, record_id, student_id, month, "submitted",
                 json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                 None, 1, json.dumps(submitters, ensure_ascii=False), 0, actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO batch_versions(batch_id,sequence,basis,conclusion,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, 1, json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True), "提交时固定依据", actor_id, now),
            )
            self._append_batch_audit(connection, record_id, "submit_batch", actor_id, batch_id,
                                     {"batch_no": batch_no, "month": month}, now)
            result = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result), True

    def insert_backfilled_batch(self, record_id: int, student_id: str, month: str, basis: Dict[str, Any],
                                conclusion: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """旧数据回填：已存在（任何状态）则跳过，保证可重复执行。"""
        now = _now()
        actor_id = "system-backfill"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM review_batches WHERE student_id=? AND month=?", (student_id, month)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return None
            batch_no = self._next_batch_no(connection)
            cursor = connection.execute(
                "INSERT INTO review_batches(batch_no,record_id,student_id,month,status,basis,conclusion,error,attempts,submitters,backfilled,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_no, record_id, student_id, month, "settled",
                 json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                 None, 1, json.dumps([actor_id], ensure_ascii=False), 1, actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO batch_versions(batch_id,sequence,basis,conclusion,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, 1, json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True), "旧数据回填完成月份", actor_id, now),
            )
            self._append_batch_audit(connection, record_id, "backfill_batch", actor_id, batch_id,
                                     {"batch_no": batch_no, "month": month, "backfilled": True}, now)
            result = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("复核批次不存在")
        return self._batch_row(row)

    def list_batches(self, student_id: Optional[str] = None, status: Optional[str] = None,
                     month: Optional[str] = None, recalculating_only: bool = False, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: List[str] = []
        params: List[Any] = []
        if student_id:
            clauses.append("student_id=?")
            params.append(student_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if month:
            clauses.append("month=?")
            params.append(month)
        if recalculating_only:
            clauses.append("status='recalculating'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_batches" + where + " ORDER BY month DESC, id DESC LIMIT ?", params
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def batches_for_student(self, student_id: str, statuses: Optional[set] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    "SELECT * FROM review_batches WHERE student_id=? AND status IN (%s) ORDER BY month" % placeholders,
                    [student_id] + sorted(statuses),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM review_batches WHERE student_id=? ORDER BY month", (student_id,)
                ).fetchall()
        return [self._batch_row(row) for row in rows]

    def mark_batches_recalculating(self, record_id: int, actor_id: str) -> int:
        """依据改动后：未结算(submitted/recalculating)批次失效进入重算，已结算结论保留。"""
        now = _now()
        count = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM review_batches WHERE record_id=? AND status IN ('submitted','recalculating') ORDER BY month",
                (record_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE review_batches SET status='recalculating', updated_at=? WHERE id=?",
                    (now, int(row["id"])),
                )
                self._append_batch_audit(connection, record_id, "batch_invalidated", actor_id, int(row["id"]),
                                         {"month": row["month"], "reason": "依据已改动，等待重算"}, now)
                count += 1
            connection.commit()
        return count

    def apply_recalculation(self, batch_id: int, basis: Dict[str, Any], conclusion: Dict[str, Any],
                            actor_id: str, note: str) -> Dict[str, Any]:
        """重算成功：上一版存入batch_versions，批次回到submitted。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("复核批次不存在")
            if row["status"] == "settled":
                connection.rollback()
                raise Conflict("已结算月份结论保留，不能重算")
            next_sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0) AS s FROM batch_versions WHERE batch_id=?", (batch_id,)
            ).fetchone()["s"]) + 1
            connection.execute(
                "INSERT INTO batch_versions(batch_id,sequence,basis,conclusion,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, next_sequence, json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True), note, actor_id, now),
            )
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE review_batches SET status='submitted',basis=?,conclusion=?,error=NULL,attempts=?,updated_at=? WHERE id=?",
                (json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(conclusion, ensure_ascii=False, sort_keys=True), attempts, now, batch_id),
            )
            self._append_batch_audit(connection, int(row["record_id"]), "batch_recalculated", actor_id, batch_id,
                                     {"month": row["month"], "sequence": next_sequence}, now)
            result = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def record_recalculation_failure(self, batch_id: int, basis: Dict[str, Any], message: str, actor_id: str) -> Dict[str, Any]:
        """重算失败：保留上一版结论，仅记录错误并维持recalculating以便重试。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("复核批次不存在")
            if row["status"] == "settled":
                connection.rollback()
                raise Conflict("已结算月份结论保留，不能重算")
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE review_batches SET status='recalculating',basis=?,error=?,attempts=?,updated_at=? WHERE id=?",
                (json.dumps(basis, ensure_ascii=False, sort_keys=True), message, attempts, now, batch_id),
            )
            self._append_batch_audit(connection, int(row["record_id"]), "batch_recalc_failed", actor_id, batch_id,
                                     {"month": row["month"], "error": message, "retained_version": True}, now)
            result = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def settle_batch(self, batch_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("复核批次不存在")
            if row["status"] != "submitted":
                connection.rollback()
                raise Conflict("只有未结算批次可以结算，当前状态：%s" % row["status"])
            connection.execute("UPDATE review_batches SET status='settled',updated_at=? WHERE id=?", (now, batch_id))
            self._append_batch_audit(connection, int(row["record_id"]), "settle_batch", actor_id, batch_id,
                                     {"month": row["month"], "batch_no": row["batch_no"]}, now)
            result = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def batch_versions(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_versions WHERE batch_id=? ORDER BY sequence", (batch_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["basis"] = json.loads(item["basis"])
            item["conclusion"] = json.loads(item["conclusion"])
            result.append(item)
        return result

    def batch_stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS total FROM review_batches GROUP BY status").fetchall()
        stats = {str(row["status"]): int(row["total"]) for row in rows}
        stats.setdefault("submitted", 0)
        stats.setdefault("settled", 0)
        stats["recalculating"] = stats.get("recalculating", 0)
        stats["total"] = sum(stats[key] for key in ("submitted", "settled", "recalculating"))
        return stats

    @staticmethod
    def _append_batch_audit(connection: sqlite3.Connection, record_id: int, action: str, actor_id: str,
                            batch_id: int, details: Dict[str, Any], now: str) -> None:
        version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        version = int(version_row["version"]) if version_row else 0
        payload = dict(details)
        payload["batch_id"] = batch_id
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
