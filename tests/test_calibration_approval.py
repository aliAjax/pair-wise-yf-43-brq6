import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.domain import Actor, ConflictError, NotFoundError, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

STATIC_DIR = str(Path(__file__).resolve().parent.parent / "static")


class CalibrationApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.authorizer = Actor("auth-1", "authorizer")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self, name="Analyzer", serial="A-1"):
        return self.service.create(
            self.admin, "instrument", {"name": name, "serial": serial}
        )

    def _passed_calibration(self, instrument_id, requested_at, performed_at, due_at,
                            number=None, uncertainty=0.01):
        data = {"instrument_id": instrument_id, "requested_at": requested_at}
        if number:
            data["calibration_no"] = number
        calibration = self.service.create(self.admin, "calibration", data)
        return self.service.transition(
            self.admin,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": performed_at,
                "uncertainty": uncertainty,
                "due_at": due_at,
            },
        )

    def _validated_method(self, instrument_id):
        method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        return self.service.transition(
            self.authorizer,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument_id]},
        )

    def test_approval_takes_effect_on_instrument_and_keeps_snapshot(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01", number="CAL-001"
        )

        approved = self.service.transition(
            self.authorizer,
            calibration["id"],
            "approve",
            {"authorized_by": "QA-1"},
        )

        self.assertEqual(approved["status"], "approved")
        updated = self.service.get(instrument["id"])
        self.assertEqual(updated["version"], instrument["version"] + 1)
        self.assertEqual(updated["data"]["effective_calibration_id"], calibration["id"])
        self.assertEqual(updated["data"]["effective_calibration_no"], "CAL-001")
        self.assertEqual(updated["data"]["due_at"], "2099-01-01")

        history = self.service.calibration_history(instrument["id"])
        self.assertEqual(len(history), 1)
        snapshot = history[0]
        self.assertEqual(snapshot["calibration_id"], calibration["id"])
        self.assertEqual(snapshot["instrument_id"], instrument["id"])
        self.assertEqual(snapshot["calibration_no"], "CAL-001")
        self.assertEqual(snapshot["due_at"], "2099-01-01")
        self.assertEqual(snapshot["authorized_by"], "QA-1")
        self.assertEqual(snapshot["status"], "approved")
        self.assertEqual(snapshot["version"], approved["version"])
        self.assertEqual(snapshot["approved_by"], "auth-1")
        self.assertEqual(snapshot["data"]["uncertainty"], 0.01)

        # 校准与仪器各保留一条审计，且仪器审计指向生效校准。
        cal_audit = self.service.audit_log(calibration["id"])
        self.assertEqual([a["action"] for a in cal_audit], ["create", "perform", "approve"])
        inst_audit = self.service.audit_log(instrument["id"])
        effect = [a for a in inst_audit if a["action"] == "effective_calibration_approved"]
        self.assertEqual(len(effect), 1)
        self.assertEqual(effect[0]["detail"]["calibration_id"], calibration["id"])
        self.assertEqual(effect[0]["detail"]["calibration_no"], "CAL-001")

    def test_approval_uses_calibration_id_as_number_when_missing(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01"
        )
        self.service.transition(
            self.authorizer, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        updated = self.service.get(instrument["id"])
        self.assertEqual(
            updated["data"]["effective_calibration_no"], calibration["id"]
        )
        self.assertEqual(
            self.service.calibration_history(instrument["id"])[0]["calibration_no"],
            calibration["id"],
        )

    def test_quarantined_instrument_rejects_approval_without_changes(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2026-02-01", "2026-02-02", "2099-02-01", number="CAL-002"
        )
        quarantined = self.service.transition(
            self.admin, instrument["id"], "quarantine", {"reason": "broken"}
        )

        audit_before = self.service.audit_log()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.authorizer, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )

        # 校准仍停留在 passed，仪器版本/状态/生效校准不变，没有新增审计或快照。
        self.assertEqual(self.service.get(calibration["id"])["status"], "passed")
        instrument_after = self.service.get(instrument["id"])
        self.assertEqual(instrument_after["version"], quarantined["version"])
        self.assertEqual(instrument_after["status"], "quarantined")
        self.assertNotIn("effective_calibration_id", instrument_after["data"])
        self.assertEqual(self.service.audit_log(), audit_before)
        self.assertEqual(self.service.calibration_history(instrument["id"]), [])

    def test_newer_effective_calibration_rejects_older_approval(self):
        instrument = self._instrument()
        newer = self._passed_calibration(
            instrument["id"], "2026-03-01", "2026-03-02", "2099-03-01", number="CAL-NEW"
        )
        older = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01", number="CAL-OLD"
        )
        self.service.transition(
            self.authorizer, newer["id"], "approve", {"authorized_by": "QA-1"}
        )

        audit_count = len(self.service.audit_log())
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.authorizer, older["id"], "approve", {"authorized_by": "QA-1"}
            )

        self.assertEqual(self.service.get(older["id"])["status"], "passed")
        updated = self.service.get(instrument["id"])
        self.assertEqual(updated["data"]["effective_calibration_id"], newer["id"])
        self.assertEqual(updated["data"]["effective_calibration_no"], "CAL-NEW")
        self.assertEqual(len(self.service.audit_log()), audit_count)
        history = self.service.calibration_history(instrument["id"])
        self.assertEqual([s["calibration_no"] for s in history], ["CAL-NEW"])

    def test_stale_expected_version_conflicts_and_changes_nothing(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01"
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.authorizer,
                calibration["id"],
                "approve",
                {"authorized_by": "QA-1"},
                expected_version=calibration["version"] - 1,
            )
        self.assertEqual(self.service.get(calibration["id"])["status"], "passed")
        self.assertEqual(
            self.service.calibration_history(instrument["id"]), []
        )

    def test_release_uses_current_effective_calibration(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01", number="CAL-001"
        )
        self.service.transition(
            self.authorizer, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        method = self._validated_method(instrument["id"])
        result = self.service.create(
            self.admin, "result", {"sample_id": "S-1", "measurement": "initial"}
        )

        released = self.service.transition(
            self.admin,
            result["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": 4.2,
                "unit": "mg/L",
            },
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["calibration_id"], calibration["id"])
        self.assertEqual(released["data"]["calibration_no"], "CAL-001")
        self.assertEqual(released["data"]["due_at"], "2099-01-01")

    def test_expired_effective_calibration_blocks_release(self):
        instrument = self._instrument()
        calibration = self._passed_calibration(
            instrument["id"], "2025-01-01", "2025-01-02", "2025-06-01", number="CAL-EXP"
        )
        self.service.transition(
            self.authorizer, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        method = self._validated_method(instrument["id"])
        result = self.service.create(
            self.admin, "result", {"sample_id": "S-2", "measurement": "initial"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                result["id"],
                "release",
                {
                    "instrument_id": instrument["id"],
                    "method_id": method["id"],
                    "value": 1.0,
                    "unit": "mg/L",
                },
            )

    def test_history_is_traceable_in_effect_time_order(self):
        instrument = self._instrument()
        first = self._passed_calibration(
            instrument["id"], "2026-01-01", "2026-01-02", "2099-01-01", number="CAL-1"
        )
        second = self._passed_calibration(
            instrument["id"], "2026-02-01", "2026-02-02", "2099-02-01", number="CAL-2"
        )
        self.service.transition(
            self.authorizer, first["id"], "approve", {"authorized_by": "QA-1"}
        )
        self.service.transition(
            self.authorizer, second["id"], "approve", {"authorized_by": "QA-1"}
        )

        history = self.service.calibration_history(instrument["id"])
        self.assertEqual([s["calibration_no"] for s in history], ["CAL-1", "CAL-2"])
        self.assertEqual(
            self.service.get(instrument["id"])["data"]["effective_calibration_id"],
            second["id"],
        )

    def test_history_for_unknown_instrument_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.calibration_history("missing")


class CalibrationApprovalHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(repo, RuleEngine())
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), STATIC_DIR)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def _post(self, path, payload, role="admin", user="admin"):
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-User-Id": user,
                "X-Role": role,
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_quarantine_approval_returns_409_and_exposes_history(self):
        _, instrument = self._post(
            "/api/instruments", {"name": "Analyzer", "serial": "A-1"}
        )
        _, calibration = self._post(
            "/api/calibrations",
            {"instrument_id": instrument["id"], "requested_at": "2026-02-01"},
        )
        self._post(
            "/api/entities/%s/actions" % calibration["id"],
            {
                "action": "perform",
                "data": {
                    "result": "passed",
                    "performed_at": "2026-02-02",
                    "uncertainty": 0.01,
                    "due_at": "2099-02-01",
                },
            },
            role="metrology",
        )
        self._post(
            "/api/entities/%s/actions" % instrument["id"],
            {"action": "quarantine", "data": {"reason": "broken"}},
            role="metrology",
        )

        request = urllib.request.Request(
            self._url("/api/entities/%s/actions" % calibration["id"]),
            data=json.dumps(
                {"action": "approve", "data": {"authorized_by": "QA-1"}}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "auth-1",
                "X-Role": "authorizer",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 409)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertEqual(payload["type"], "ConflictError")

        with urllib.request.urlopen(
            self._url("/api/entities/%s/effective-calibrations" % instrument["id"])
        ) as response:
            history = json.loads(response.read().decode("utf-8"))
        self.assertEqual(history["items"], [])


if __name__ == "__main__":
    unittest.main()
