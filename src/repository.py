import json
import sqlite3
from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
)


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
                    calibration_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    calibration_no TEXT NOT NULL,
                    performed_at TEXT,
                    due_at TEXT,
                    authorized_by TEXT,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_instrument
                    ON calibration_snapshots(instrument_id, id);
                CREATE INDEX IF NOT EXISTS idx_snapshots_calibration
                    ON calibration_snapshots(calibration_id, id);
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

    def approve_calibration(self, calibration_id, expected_version, actor, patch):
        """在同一事务内审批校准并使其在仪器上生效。

        - 仪器已隔离，或存在更新的已生效校准：抛 ConflictError，全部回滚。
        - 校准状态非 passed：抛 InvalidTransition；版本不符：抛 ConflictError。
        - 更新校准 -> 更新仪器生效校准/到期日/版本 -> 写审批快照与两条审计。
        返回 (updated_calibration, updated_instrument, snapshot)。
        """
        now = utcnow()
        calibration_payload = json.dumps(patch["calibration_data"], ensure_ascii=False, sort_keys=True)
        instrument_payload = json.dumps(patch["instrument_data"], ensure_ascii=False, sort_keys=True)
        snapshot_data = json.dumps(patch["snapshot"]["data"], ensure_ascii=False, sort_keys=True)
        snapshot = patch["snapshot"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")

            cal_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'calibration'",
                (calibration_id,),
            ).fetchone()
            if not cal_row:
                raise NotFoundError("entity not found: " + calibration_id)
            current_version = int(cal_row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            if cal_row["status"] != "passed":
                raise InvalidTransition(
                    "cannot approve from status %s" % cal_row["status"]
                )

            cal_data = json.loads(cal_row["data"])
            instrument_id = cal_data.get("instrument_id")
            inst_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'instrument'",
                (instrument_id,),
            ).fetchone()
            if not inst_row:
                raise NotFoundError("entity not found: " + str(instrument_id))
            if inst_row["status"] == "quarantined":
                raise ConflictError(
                    "instrument %s is quarantined; calibration approval rejected" % instrument_id
                )

            # 存在更新的已生效校准则拒绝（按 performed_at，缺省回退 requested_at）。
            this_date = str(cal_data.get("performed_at") or cal_data.get("requested_at") or "")
            rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'calibration' AND status = 'approved'"
            ).fetchall()
            for row in rows:
                other = json.loads(row["data"])
                if other.get("instrument_id") != instrument_id or row["id"] == calibration_id:
                    continue
                other_date = str(other.get("performed_at") or other.get("requested_at") or "")
                if other_date > this_date:
                    raise ConflictError(
                        "newer effective calibration %s already approved for instrument %s"
                        % (row["id"], instrument_id)
                    )

            cal_update = connection.execute(
                "UPDATE entities SET status = 'approved', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (calibration_payload, now, calibration_id, current_version),
            )
            if cal_update.rowcount != 1:
                raise ConflictError("calibration changed concurrently: " + calibration_id)
            instrument_version = int(inst_row["version"])
            inst_update = connection.execute(
                "UPDATE entities SET version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (instrument_payload, now, instrument_id, instrument_version),
            )
            if inst_update.rowcount != 1:
                raise ConflictError("instrument changed concurrently: " + str(instrument_id))

            cursor = connection.execute(
                "INSERT INTO calibration_snapshots(calibration_id, instrument_id, calibration_no, "
                "performed_at, due_at, authorized_by, status, version, data, approved_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot["calibration_id"],
                    snapshot["instrument_id"],
                    snapshot["calibration_no"],
                    snapshot.get("performed_at"),
                    snapshot.get("due_at"),
                    snapshot.get("authorized_by"),
                    snapshot["status"],
                    snapshot["version"],
                    snapshot_data,
                    actor.user_id,
                    now,
                ),
            )
            snapshot_id = cursor.lastrowid

            self._insert_audit(
                connection,
                calibration_id,
                actor,
                "approve",
                "passed",
                "approved",
                {
                    "patch": patch["patch"],
                    "instrument_id": instrument_id,
                    "snapshot_id": snapshot_id,
                },
                now,
            )
            self._insert_audit(
                connection,
                instrument_id,
                actor,
                "effective_calibration_approved",
                inst_row["status"],
                inst_row["status"],
                {
                    "calibration_id": calibration_id,
                    "calibration_no": snapshot["calibration_no"],
                    "due_at": snapshot.get("due_at"),
                    "from_calibration_id": json.loads(inst_row["data"]).get("effective_calibration_id"),
                },
                now,
            )

            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        updated_calibration = self.get_entity(calibration_id)
        updated_instrument = self.get_entity(instrument_id)
        stored_snapshot = self.get_snapshot(snapshot_id)
        return updated_calibration, updated_instrument, stored_snapshot

    @staticmethod
    def _insert_audit(connection, entity_id, actor, action, from_status, to_status, detail, now):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor.user_id,
                actor.role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

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

    @staticmethod
    def _snapshot_from_row(row):
        return {
            "id": row["id"],
            "calibration_id": row["calibration_id"],
            "instrument_id": row["instrument_id"],
            "calibration_no": row["calibration_no"],
            "performed_at": row["performed_at"],
            "due_at": row["due_at"],
            "authorized_by": row["authorized_by"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "approved_by": row["approved_by"],
            "created_at": row["created_at"],
        }

    def get_snapshot(self, snapshot_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM calibration_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return self._snapshot_from_row(row) if row else None

    def list_snapshots(self, instrument_id=None, calibration_id=None):
        clauses = []
        params = []
        if instrument_id:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)
        if calibration_id:
            clauses.append("calibration_id = ?")
            params.append(calibration_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM calibration_snapshots" + where + " ORDER BY id", params
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
