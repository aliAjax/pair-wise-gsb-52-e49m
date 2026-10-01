"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
                CREATE TABLE IF NOT EXISTS service_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    import_key TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'import',
                    batch_id INTEGER REFERENCES review_batches(id) ON DELETE SET NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(student_id, month, import_key)
                );
                CREATE TABLE IF NOT EXISTS review_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    student_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    state TEXT NOT NULL,
                    basis TEXT,
                    result TEXT,
                    error TEXT,
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(student_id, month)
                );
                CREATE TABLE IF NOT EXISTS batch_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES review_batches(id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_service_student_month ON service_records(student_id, month);
                CREATE INDEX IF NOT EXISTS idx_service_batch ON service_records(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batches_student_month ON review_batches(student_id, month);
                CREATE INDEX IF NOT EXISTS idx_batches_state ON review_batches(state);
                CREATE INDEX IF NOT EXISTS idx_batch_entries ON batch_entries(batch_id, id);
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
            batch_rows = connection.execute("SELECT state, COUNT(*) AS total FROM review_batches GROUP BY state").fetchall()
            error_rows = connection.execute("SELECT COUNT(*) AS total FROM review_batches WHERE state=? AND error IS NOT NULL", ("recalculating",)).fetchone()
            stale_rows = connection.execute("SELECT COUNT(*) AS total FROM review_batches WHERE stale=1").fetchone()
        result = {str(row["state"]): int(row["total"]) for row in rows}
        result["batch_total"] = sum(int(row["total"]) for row in batch_rows)
        result["batch_settled"] = sum(int(row["total"]) for row in batch_rows if row["state"] == "settled")
        result["batch_recalculating"] = sum(int(row["total"]) for row in batch_rows if row["state"] == "recalculating")
        result["batch_recompute_errors"] = int(error_rows["total"])
        result["batch_stale"] = int(stale_rows["total"])
        return result

    # ---- 服务记录台账与复核批次 ----

    @staticmethod
    def _service_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("basis", "result"):
            item[key] = json.loads(item[key]) if item.get(key) else None
        item["stale"] = bool(item["stale"])
        return item

    def upsert_service_record(self, student_id: str, month: str, minutes: int, provider: str, import_key: str, source: str, actor_id: str) -> Dict[str, Any]:
        """导入一条服务记录；同一学生、月份、导入键重复导入不重复计数。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM service_records WHERE student_id=? AND month=? AND import_key=?",
                (student_id, month, import_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                row_data = self._service_row(existing)
                row_data["inserted"] = False
                return row_data
            cursor = connection.execute(
                "INSERT INTO service_records(student_id,month,minutes,provider,import_key,source,batch_id,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,NULL,?,?)",
                (student_id, month, int(minutes), provider, import_key, source, actor_id, now),
            )
            record_id = int(cursor.lastrowid)
            # 若该月已有批次，新台账并入批次计数
            batch = connection.execute("SELECT id FROM review_batches WHERE student_id=? AND month=?", (student_id, month)).fetchone()
            if batch is not None:
                connection.execute("UPDATE service_records SET batch_id=? WHERE id=?", (int(batch["id"]), record_id))
            row = connection.execute("SELECT * FROM service_records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        row_data = self._service_row(row)
        row_data["inserted"] = True
        return row_data

    def service_record_exists(self, student_id: str, month: str, import_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM service_records WHERE student_id=? AND month=? AND import_key=?",
                (student_id, month, import_key),
            ).fetchone()
        return row is not None

    def list_service_records(self, student_id: str = None, month: str = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if student_id:
            clauses.append("student_id=?")
            params.append(student_id)
        if month:
            clauses.append("month=?")
            params.append(month)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM service_records" + where + " ORDER BY month DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [self._service_row(row) for row in rows]

    def service_records_for(self, student_id: str, month: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM service_records WHERE student_id=? AND month=? ORDER BY id",
                (student_id, month),
            ).fetchall()
        return [self._service_row(row) for row in rows]

    def unbatched_service_groups(self, completed_only: bool = True, now_month: str = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT student_id, month, COUNT(*) AS total FROM service_records WHERE batch_id IS NULL GROUP BY student_id, month ORDER BY month, student_id"
            ).fetchall()
        groups = [{"student_id": row["student_id"], "month": row["month"], "record_count": int(row["total"])} for row in rows]
        if completed_only:
            from .rules import month_key
            groups = [group for group in groups if month_key(group["month"]) < month_key(now_month)]
        return groups

    def find_batch(self, student_id: str, month: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_batches WHERE student_id=? AND month=?",
                (student_id, month),
            ).fetchone()
        return self._batch_row(row) if row is not None else None

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("复核批次不存在")
        return self._batch_row(row)

    def list_batches(self, state: str = None, stale_only: bool = False, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if state:
            clauses.append("state=?")
            params.append(state)
        if stale_only:
            clauses.append("stale=1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_batches" + where + " ORDER BY month DESC, id DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def insert_batch(self, batch_no: str, student_id: str, month: str, state: str, basis: Optional[Dict[str, Any]], result: Optional[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM review_batches WHERE student_id=? AND month=?",
                (student_id, month),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._batch_row(existing)
            cursor = connection.execute(
                "INSERT INTO review_batches(batch_no,student_id,month,state,basis,result,error,stale,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,NULL,0,?,?,?)",
                (batch_no, student_id, month, state,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True) if basis else None,
                 json.dumps(result, ensure_ascii=False, sort_keys=True) if result else None,
                 actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE service_records SET batch_id=? WHERE student_id=? AND month=? AND batch_id IS NULL",
                (batch_id, student_id, month),
            )
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(row)

    def attach_service_records(self, batch_id: int, student_id: str, month: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE service_records SET batch_id=? WHERE student_id=? AND month=? AND batch_id IS NULL",
                (batch_id, student_id, month),
            )
        return cursor.rowcount

    def add_batch_entry(self, batch_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_entries(batch_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",
                (batch_id, actor_id, action, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def batch_entries(self, batch_id: int) -> List[Dict[str, Any]]:
        self.get_batch(batch_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_entries WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def save_batch_result(self, batch_id: int, state: str, basis: Optional[Dict[str, Any]], result: Optional[Dict[str, Any]], error: Optional[str], stale: bool, actor_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE review_batches SET state=?,basis=?,result=?,error=?,stale=?,updated_at=? WHERE id=?",
                (state,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True) if basis else None,
                 json.dumps(result, ensure_ascii=False, sort_keys=True) if result else None,
                 error, 1 if stale else 0, _now(), batch_id),
            )
            row = connection.execute("SELECT * FROM review_batches WHERE id=?", (batch_id,)).fetchone()
        return self._batch_row(row)

    def mark_stale_unsettled(self, student_id: str, actor_id: str) -> List[str]:
        """计划/监护人同意改动后：已结算月份结论保留，仅未结算月份失效重算。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, month FROM review_batches WHERE student_id=? AND state<>? AND stale=0",
                (student_id, "settled"),
            ).fetchall()
            months = [str(row["month"]) for row in rows]
            for row in rows:
                connection.execute("UPDATE review_batches SET stale=1, updated_at=? WHERE id=?", (now, int(row["id"])))
                connection.execute(
                    "INSERT INTO batch_entries(batch_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",
                    (int(row["id"]), actor_id, "invalidated",
                     json.dumps({"reason": "计划或监护人同意改动，未结算月份失效重算"}, ensure_ascii=False), now),
                )
            connection.commit()
        return months

    def eligible_plan(self, student_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records ORDER BY id").fetchall()
        plans = [self._row(row) for row in rows]
        plans = [plan for plan in plans if (plan.get("payload") or {}).get("student_id") == student_id]
        if not plans:
            return None
        for preferred in ("active", "under_review", "consented"):
            for plan in plans:
                if plan["state"] == preferred:
                    return plan
        return plans[-1]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
