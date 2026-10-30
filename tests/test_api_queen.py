"""HTTP contract smoke: optional API dependencies, no production traffic."""
from pathlib import Path
import unittest

try:
    from fastapi.testclient import TestClient
    from api.main import app, PROFILE_DIR
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "Optional API requirements are not installed")
class QueenApi(unittest.TestCase):
    def test_canonical_queen_request_returns_matching_engine_identity(self):
        client = TestClient(app)
        before = set(PROFILE_DIR.glob("req_*.json"))
        response = client.post("/simulate", json={
            "squad": "리타,크라운,마리안 : 퀸,모더니아,나가",
            "duration": 1, "expected": True,
            "profile_data": {"base": "default", "chars": {}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["returncode"], 0)
        self.assertIn("마리안 : 퀸", result["output"])
        self.assertEqual(result["engine"], client.get("/health").json()["commit"])
        self.assertRegex(result["engine"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(set(PROFILE_DIR.glob("req_*.json")), before)

    def test_unknown_names_still_fail_instead_of_becoming_another_character(self):
        response = TestClient(app).post("/simulate", json={"squad": "없는 니케", "duration": 1})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
