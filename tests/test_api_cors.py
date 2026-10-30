"""Production web app connectivity; optional API dependencies."""
import unittest
try:
    from fastapi.testclient import TestClient
    from api.main import app
except ImportError:
    TestClient = None

@unittest.skipIf(TestClient is None, "Optional API requirements are not installed")
class ApiCors(unittest.TestCase):
    def test_production_web_origin_can_call_the_simulator(self):
        client = TestClient(app)
        headers = {"Origin": "https://nikke-raid.vercel.app", "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type"}
        response = client.options("/simulate", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], headers["Origin"])
        response = client.options("/simulate", headers={**headers, "Origin": "https://unrelated.example"})
        self.assertEqual(response.status_code, 400)
