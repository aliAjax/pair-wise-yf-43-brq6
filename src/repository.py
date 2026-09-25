import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS calibration_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id TEXT NOT NULL,
                    calibration_id TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    performed_at TEXT,
                    approved_by TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_instrument
                    ON calibration_snapshots(instrument_id, id);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def apply_calibration_approval(
        self,
        calibration_id,
        calibration_version,
        calibration_status,
        calibration_data,
        instrument_id,
        instrument_version,
        instrument_data,
        snapshot,
        audit_entries,
        verify=None,
    ):
        """Approve a calibration and make it effective on its instrument.

        Calibration update, instrument update, approval snapshot and audit
        entries commit in one transaction. ``verify`` runs against the rows
        re-read inside the transaction; raising there rolls everything back.
        """
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            calibration_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (calibration_id,)
            ).fetchone()
            if not calibration_row:
                raise NotFoundError("entity not found: " + calibration_id)
            instrument_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (instrument_id,)
            ).fetchone()
            if not instrument_row:
                raise NotFoundError("entity not found: " + instrument_id)
            found_calibration_version = int(calibration_row["version"])
            if found_calibration_version != int(calibration_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (calibration_version, found_calibration_version)
                )
            found_instrument_version = int(instrument_row["version"])
            if found_instrument_version != int(instrument_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (instrument_version, found_instrument_version)
                )
            if verify:
                verify(
                    self._entity_from_row(calibration_row),
                    self._entity_from_row(instrument_row),
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    calibration_status,
                    json.dumps(calibration_data, ensure_ascii=False, sort_keys=True),
                    now,
                    calibration_id,
                    found_calibration_version,
                ),
            )
            connection.execute(
                "UPDATE entities SET version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (
                    json.dumps(instrument_data, ensure_ascii=False, sort_keys=True),
                    now,
                    instrument_id,
                    found_instrument_version,
                ),
            )
            connection.execute(
                "INSERT INTO calibration_snapshots(instrument_id, calibration_id, due_at, performed_at, approved_by, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot["instrument_id"],
                    snapshot["calibration_id"],
                    snapshot["due_at"],
                    snapshot.get("performed_at"),
                    snapshot["approved_by"],
                    json.dumps(snapshot.get("detail", {}), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            for entry in audit_entries:
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["entity_id"],
                        entry["actor_id"],
                        entry["actor_role"],
                        entry["action"],
                        entry["from_status"],
                        entry["to_status"],
                        json.dumps(entry["detail"], ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(calibration_id)

    def list_calibration_snapshots(self, instrument_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM calibration_snapshots WHERE instrument_id = ? ORDER BY id",
                (instrument_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "instrument_id": row["instrument_id"],
                "calibration_id": row["calibration_id"],
                "due_at": row["due_at"],
                "performed_at": row["performed_at"],
                "approved_by": row["approved_by"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
