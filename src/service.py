from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, check_calibration_approvable


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
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "calibration" and action == "approve":
            return self._approve_calibration(actor, entity, next_status, patch, expected)
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

    def _approve_calibration(self, actor, calibration, next_status, patch, expected_version):
        """Approve a calibration and make it effective on its instrument atomically."""
        instrument_id = calibration["data"].get("instrument_id")
        instrument = self.repository.get_entity(instrument_id)
        check_calibration_approvable(instrument, calibration)
        instrument_patch = {
            "effective_calibration_id": calibration["id"],
            "effective_performed_at": calibration["data"].get("performed_at"),
            "due_at": calibration["data"].get("due_at"),
        }
        merged_calibration = dict(calibration["data"])
        merged_calibration.update(patch)
        merged_instrument = dict(instrument["data"])
        merged_instrument.update(instrument_patch)
        snapshot = {
            "instrument_id": instrument_id,
            "calibration_id": calibration["id"],
            "due_at": calibration["data"].get("due_at"),
            "performed_at": calibration["data"].get("performed_at"),
            "approved_by": actor.user_id,
            "detail": {
                "authorized_by": patch.get("authorized_by"),
                "calibration_version": calibration["version"] + 1,
                "instrument_version": instrument["version"] + 1,
                "instrument_patch": instrument_patch,
            },
        }
        audit_entries = [
            {
                "entity_id": calibration["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "approve",
                "from_status": calibration["status"],
                "to_status": next_status,
                "detail": {"patch": patch},
            },
            {
                "entity_id": instrument_id,
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "calibration_effective",
                "from_status": instrument["status"],
                "to_status": instrument["status"],
                "detail": {"patch": instrument_patch},
            },
        ]

        def verify(calibration_row, instrument_row):
            check_calibration_approvable(instrument_row, calibration_row)

        return self.repository.apply_calibration_approval(
            calibration_id=calibration["id"],
            calibration_version=expected_version,
            calibration_status=next_status,
            calibration_data=merged_calibration,
            instrument_id=instrument_id,
            instrument_version=instrument["version"],
            instrument_data=merged_instrument,
            snapshot=snapshot,
            audit_entries=audit_entries,
            verify=verify,
        )

    def calibration_history(self, instrument_id):
        instrument = self.repository.get_entity(instrument_id)
        if not instrument or instrument["kind"] != "instrument":
            raise NotFoundError("instrument not found: " + instrument_id)
        return self.repository.list_calibration_snapshots(instrument_id)

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
