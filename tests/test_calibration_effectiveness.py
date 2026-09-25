import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CalibrationEffectivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("metrology-1", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        return self.service.create(
            self.actor, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )

    def _calibration(self, instrument_id, performed_at, due_at):
        calibration = self.service.create(
            self.actor,
            "calibration",
            {"instrument_id": instrument_id, "requested_at": "2026-01-01"},
        )
        return self.service.transition(
            self.actor,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": performed_at,
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )

    def _approve(self, calibration_id):
        return self.service.transition(
            self.actor, calibration_id, "approve", {"authorized_by": "QA-1"}
        )

    def _validated_method(self, instrument_id):
        method = self.service.create(
            self.actor, "method", {"name": "Assay", "version": "v1"}
        )
        return self.service.transition(
            self.actor,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument_id]},
        )

    def _release(self, instrument_id, method_id):
        result = self.service.create(
            self.actor, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        return self.service.transition(
            self.actor,
            result["id"],
            "release",
            {
                "instrument_id": instrument_id,
                "method_id": method_id,
                "value": 1.0,
                "unit": "mg/L",
            },
        )

    def test_approval_updates_instrument_in_same_transaction(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], "2026-01-02", "2099-01-01")
        approved = self._approve(calibration["id"])
        self.assertEqual(approved["status"], "approved")

        updated = self.service.get(instrument["id"])
        self.assertEqual(updated["version"], instrument["version"] + 1)
        self.assertEqual(updated["data"]["effective_calibration_id"], calibration["id"])
        self.assertEqual(updated["data"]["due_at"], "2099-01-01")

        history = self.service.calibration_history(instrument["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["calibration_id"], calibration["id"])
        self.assertEqual(history[0]["due_at"], "2099-01-01")
        self.assertEqual(history[0]["approved_by"], self.actor.user_id)

        instrument_actions = [
            entry["action"] for entry in self.service.audit_log(instrument["id"])
        ]
        self.assertIn("calibration_effective", instrument_actions)
        calibration_actions = [
            entry["action"] for entry in self.service.audit_log(calibration["id"])
        ]
        self.assertIn("approve", calibration_actions)

    def test_approval_rejected_when_instrument_quarantined(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], "2026-01-02", "2099-01-01")
        self.service.transition(
            self.actor, instrument["id"], "quarantine", {"reason": "leak"}
        )
        before = self.service.get(instrument["id"])
        audit_before = len(self.service.audit_log())

        with self.assertRaises(ConflictError):
            self._approve(calibration["id"])

        self.assertEqual(self.service.get(instrument["id"]), before)
        self.assertNotIn("effective_calibration_id", before["data"])
        self.assertEqual(len(self.service.audit_log()), audit_before)
        self.assertEqual(self.service.calibration_history(instrument["id"]), [])
        self.assertEqual(self.service.get(calibration["id"])["status"], "passed")

    def test_approval_rejected_when_newer_effective_calibration_exists(self):
        instrument = self._instrument()
        newer = self._calibration(instrument["id"], "2026-02-01", "2099-01-01")
        self._approve(newer["id"])
        older = self._calibration(instrument["id"], "2026-01-01", "2099-06-01")
        audit_before = len(self.service.audit_log())

        with self.assertRaises(ConflictError):
            self._approve(older["id"])

        updated = self.service.get(instrument["id"])
        self.assertEqual(updated["data"]["effective_calibration_id"], newer["id"])
        self.assertEqual(updated["data"]["due_at"], "2099-01-01")
        self.assertEqual(len(self.service.audit_log()), audit_before)
        self.assertEqual(len(self.service.calibration_history(instrument["id"])), 1)
        self.assertEqual(self.service.get(older["id"])["status"], "passed")

    def test_newer_calibration_supersedes_and_history_is_traceable(self):
        instrument = self._instrument()
        first = self._calibration(instrument["id"], "2026-01-01", "2026-12-01")
        self._approve(first["id"])
        second = self._calibration(instrument["id"], "2026-03-01", "2099-01-01")
        self._approve(second["id"])

        updated = self.service.get(instrument["id"])
        self.assertEqual(updated["data"]["effective_calibration_id"], second["id"])
        self.assertEqual(updated["data"]["due_at"], "2099-01-01")

        history = self.service.calibration_history(instrument["id"])
        self.assertEqual(
            [row["calibration_id"] for row in history], [first["id"], second["id"]]
        )
        self.assertEqual(history[0]["due_at"], "2026-12-01")
        self.assertEqual(history[1]["due_at"], "2099-01-01")

    def test_release_uses_effective_calibration_and_reports_it(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], "2026-01-02", "2099-01-01")
        self._approve(calibration["id"])
        method = self._validated_method(instrument["id"])

        released = self._release(instrument["id"], method["id"])
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["calibration_id"], calibration["id"])
        self.assertEqual(released["data"]["calibration_due_at"], "2099-01-01")

    def test_release_blocked_without_effective_calibration(self):
        instrument = self._instrument()
        method = self._validated_method(instrument["id"])
        with self.assertRaises(ValidationError):
            self._release(instrument["id"], method["id"])

    def test_release_blocked_when_effective_calibration_expired(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], "2020-01-02", "2020-12-31")
        self._approve(calibration["id"])
        method = self._validated_method(instrument["id"])
        with self.assertRaises(ValidationError):
            self._release(instrument["id"], method["id"])


if __name__ == "__main__":
    unittest.main()
