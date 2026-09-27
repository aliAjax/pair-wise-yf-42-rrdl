import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BirthHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(repo, RuleEngine())
        static_dir = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, self.service, self.service.rules, str(static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {"X-User-Id": "admin", "X-Role": "admin", "Content-Type": "application/json"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _request(self, method, path, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            method=method,
            headers={**self.headers, **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _create(self, kind, data):
        status, body = self._request("POST", "/api/%s" % kind, data)
        self.assertEqual(status, 201, body)
        return body

    def _approved_pairing(self):
        sire = self._create("animal", {"name": "公", "sex": "male"})
        dam = self._create("animal", {"name": "母", "sex": "female"})
        pairing = self._create("pairing", {"proposed_by": "coordinator"})
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % pairing["id"],
            {"action": "approve", "data": {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet"]}},
        )
        self.assertEqual(status, 200, body)
        return pairing, sire, dam

    def test_batch_success_and_parent_query(self):
        pairing, sire, dam = self._approved_pairing()
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % pairing["id"],
            {
                "action": "complete",
                "data": {
                    "offspring": [
                        {"id": "cub-a", "name": "大宝", "sex": "male", "birth_date": "2026-06-01"},
                        {"id": "cub-b", "name": "二宝", "sex": "female", "birth_date": "2026-06-01"},
                    ]
                },
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "completed")

        status, body = self._request(
            "GET", "/api/animals?sire_id=%s&dam_id=%s" % (sire["id"], dam["id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in body["items"]}, {"cub-a", "cub-b"})

    def test_batch_conflict_returns_per_cub_errors_and_stays_approved(self):
        pairing, sire, dam = self._approved_pairing()
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % pairing["id"],
            {
                "action": "complete",
                "data": {
                    "offspring": [
                        {"id": "cub-ok", "name": "大宝", "sex": "male", "birth_date": "2026-06-01"},
                        {"id": sire["id"], "name": "撞号", "sex": "female", "birth_date": "2026-06-01"},
                    ]
                },
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["type"], "BatchValidationError")
        self.assertEqual(body["errors"][0]["index"], 1)
        self.assertEqual(body["errors"][0]["code"], "already_archived")

        status, body = self._request("GET", "/api/entities/%s" % pairing["id"])
        self.assertEqual(body["status"], "approved")
        status, body = self._request("GET", "/api/animals?status=active")
        self.assertNotIn("cub-ok", {item["id"] for item in body["items"]})


if __name__ == "__main__":
    unittest.main()
