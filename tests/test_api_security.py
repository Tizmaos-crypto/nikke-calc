"""Security boundary tests, no production requests or personal profiles."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from api.main import app
    from api.security import RequestLimits
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "Optional API dependencies are not installed")
class ApiSecurity(unittest.TestCase):
    def test_legacy_gear_labels_preserve_all_stats_and_calculation(self):
        equipment = {
            "머리": {"level": 5, "_track": "오버로드"},
            "몸통": {"tier": "T9", "level": 3, "corp": "미실리스", "_track": "T9"},
            "팔": {"tier": "T8", "level": 2},
            "다리": {"tier": "없음"},
        }
        profile = {"base": "default", "chars": {"리타": {
            "equipment": equipment,
            "equip_skills": {"atk_pct": 11.81, "max_ammo_pct": [68.93]},
            "skill_levels": {"1": 7, "2": 8, "3": 9},
            "collection_stage": "SR15", "favorite_stage": 0,
        }}}
        original = copy.deepcopy(profile)
        from api.main import _write_temp_profile
        _, path = _write_temp_profile(profile)
        try:
            transported = json.loads(path.read_text(encoding="utf-8"))["profile"]
        finally:
            path.unlink()
        clean = copy.deepcopy(profile)
        for part in clean["chars"]["리타"]["equipment"].values():
            part.pop("_track", None)
        self.assertEqual(transported, clean)
        self.assertEqual(profile, original)
        client = TestClient(app)
        payload = {"squad": "리타", "duration": 1, "expected": True}
        legacy = client.post("/simulate", json={**payload, "profile_data": profile})
        canonical = client.post("/simulate", json={**payload, "profile_data": clean})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(canonical.status_code, 200, canonical.text)
        self.assertEqual(legacy.json()["output"], canonical.json()["output"])

    def test_other_unknown_gear_fields_are_still_rejected(self):
        response = TestClient(app).post("/simulate", json={"squad": "리타", "duration": 1,
            "profile_data": {"chars": {"리타": {"equipment": {
                "머리": {"level": 5, "_track": "오버로드", "unknown_field": 1}}}}}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown_field", response.text)

    def test_webapp_shaped_growth_profile_still_simulates(self):
        response = TestClient(app).post("/simulate", json={
            "squad": "리타", "duration": 1, "expected": True, "profile_level": "sync",
            "profile_data": {"_meta": {"name": "test-fixture", "roster": 1},
                "_account": {"synchro_level": 400},
                "chars": {"리타": {"breakthrough": 3, "core_enhancement": 0,
                    "affinity": 30, "skill_levels": {"1": 7, "2": 7, "3": 7}}}},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["returncode"], 0)
        self.assertEqual(response.json()["profile_used"], "inline")

    def test_profile_paths_and_cli_options_never_start_process(self):
        with patch("api.main.subprocess.run") as run:
            for profile in ["me", "../private", "/etc/passwd", "C:\\private", ""]:
                response = TestClient(app).post("/simulate", json={"squad": "리타", "profile": profile})
                self.assertEqual(response.status_code, 422)
            self.assertEqual(TestClient(app).post("/simulate", json={"squad": "--help"}).status_code, 422)
            run.assert_not_called()

    def test_invalid_duration_and_option_limits(self):
        with patch("api.main.subprocess.run") as run:
            for duration in [0, -1, 601]:
                self.assertEqual(TestClient(app).post("/simulate", json={"squad": "리타", "duration": duration}).status_code, 422)
            for extra in [{"tap": ["a"] * 33}, {"squad": "a," * 5 + "a"}, {"camera": "x" * 513}]:
                self.assertEqual(TestClient(app).post("/simulate", json={"squad": "리타", **extra}).status_code, 422)
            for data in ['{"squad":"x","duration":NaN}', '{"squad":"x","profile_data":{"x":Infinity}}']:
                self.assertEqual(TestClient(app).post("/simulate", content=data, headers={"content-type": "application/json"}).status_code, 422)
            run.assert_not_called()

    def test_oversize_rejected_with_cors_before_process(self):
        with patch("api.main.subprocess.run") as run:
            response = TestClient(app).post("/simulate", content=b"x" * (512 * 1024 + 1), headers={"Origin": "https://nikke-raid.vercel.app"})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.headers["access-control-allow-origin"], "https://nikke-raid.vercel.app")
            run.assert_not_called()

    def test_inline_profile_is_private_and_cleaned_on_all_exit_paths(self):
        for outcome in [0, 2, 1, "timeout"]:
            paths = []
            def child(cmd, **kwargs):
                path = Path(cmd[cmd.index("--request") + 1])
                paths.append(path)
                self.assertNotIn("profiles", path.parts)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"profile": {"base": "default", "chars": {}}})
                self.assertNotIn("--profile", cmd)
                if outcome == "timeout":
                    raise subprocess.TimeoutExpired(cmd, 60)
                return SimpleNamespace(returncode=outcome, stdout="ok" if outcome == 0 else "invalid", stderr="private-path")
            with patch("api.main.subprocess.run", side_effect=child):
                response = TestClient(app).post("/simulate", json={"squad": "리타", "profile_data": {"base": "default", "chars": {}}})
            self.assertEqual(response.status_code, {0: 200, 2: 400, 1: 500, "timeout": 504}[outcome])
            self.assertTrue(paths)
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertNotIn("private-path", response.text)
            if outcome == 0:
                self.assertEqual(response.json()["cmd"], [])
                self.assertEqual(response.json()["profile_used"], "inline")


@unittest.skipIf(TestClient is None, "Optional API dependencies are not installed")
class LimitMiddleware(unittest.IsolatedAsyncioTestCase):
    async def request(self, middleware, chunks=(b"{}",), path="/simulate"):
        messages = [{"type": "http.request", "body": chunk, "more_body": i < len(chunks)-1} for i, chunk in enumerate(chunks)]
        async def receive():
            return messages.pop(0)
        output = []
        async def send(message):
            output.append(message)
        await middleware({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)
        return next(message["status"] for message in output if message["type"] == "http.response.start")

    async def test_chunked_body_limit_and_slot_release(self):
        async def child(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
        limiter = RequestLimits(child, max_body=4)
        self.assertEqual(await self.request(limiter, (b"123", b"45")), 413)
        self.assertEqual(limiter.active, 0)
        self.assertEqual(await self.request(limiter), 200)

    async def test_concurrency_rejects_without_queue_and_health_remains_available(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def child(scope, receive, send):
            if scope["path"] == "/simulate":
                entered.set()
                await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
        limiter = RequestLimits(child, concurrency=1)
        first = asyncio.create_task(self.request(limiter))
        await entered.wait()
        try:
            self.assertEqual(await self.request(limiter), 429)
            self.assertEqual(await self.request(limiter, path="/health"), 200)
        finally:
            release.set()
            await first
        self.assertEqual(limiter.active, 0)

    async def test_rate_window_expires_and_exceptions_release_slots(self):
        async def child(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
        limiter = RequestLimits(child, per_minute=1)
        with patch("api.security.time.monotonic", return_value=100):
            self.assertEqual(await self.request(limiter), 200)
            self.assertEqual(await self.request(limiter), 429)
        with patch("api.security.time.monotonic", return_value=161):
            self.assertEqual(await self.request(limiter), 200)
        async def fail(*args):
            raise RuntimeError("fixture")
        limiter = RequestLimits(fail)
        with self.assertRaises(RuntimeError):
            await self.request(limiter)
        self.assertEqual(limiter.active, 0)
