from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if entity["kind"] == "calibration" and action == "approve":
            return self._approve_calibration(actor, entity, data or {}, expected)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _approve_calibration(self, actor, calibration, data, expected_version):
        # 状态机与权限仍由规则引擎判定；放行规则依赖仪器当前生效校准，
        # 因此校准与仪器的一致性检查放在单事务仓储方法内完成。
        next_status, patch = self.rules.validate_transition(
            actor, calibration, "approve", dict(data), self._lookup
        )
        if next_status != "approved":
            raise ConflictError("calibration approval did not resolve to approved")

        calibration_data = dict(calibration["data"])
        calibration_data.update(patch)
        instrument = self._require_instrument(calibration_data.get("instrument_id"))
        instrument_data = dict(instrument["data"])
        number = calibration_data.get("calibration_no") or calibration["id"]
        instrument_data.update(
            {
                "effective_calibration_id": calibration["id"],
                "effective_calibration_no": number,
                "due_at": calibration_data.get("due_at"),
            }
        )
        snapshot = {
            "calibration_id": calibration["id"],
            "instrument_id": instrument["id"],
            "calibration_no": number,
            "performed_at": calibration_data.get("performed_at"),
            "due_at": calibration_data.get("due_at"),
            "authorized_by": calibration_data.get("authorized_by"),
            "status": "approved",
            "version": calibration["version"] + 1,
            "data": calibration_data,
        }
        updated, _, _ = self.repository.approve_calibration(
            calibration["id"],
            expected_version,
            actor,
            {
                "calibration_data": calibration_data,
                "instrument_data": instrument_data,
                "snapshot": snapshot,
                "patch": patch,
            },
        )
        return updated

    def _require_instrument(self, instrument_id):
        instrument = None
        if instrument_id:
            instrument = self.repository.get_entity(instrument_id)
        if not instrument or instrument["kind"] != "instrument":
            raise NotFoundError("instrument not found: " + str(instrument_id))
        return instrument

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def calibration_history(self, instrument_id):
        """仪器历次生效校准的审批快照，按生效时间排序，可按时间追溯。"""
        instrument = self.repository.get_entity(instrument_id)
        if not instrument or instrument["kind"] != "instrument":
            raise NotFoundError("instrument not found: " + str(instrument_id))
        return self.repository.list_snapshots(instrument_id=instrument_id)
