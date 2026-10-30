"""Engine identity contract; uses stdlib only, matching the repository CI.

The pure fingerprint and health functions are compiled from the real API source.
FastAPI itself is optional for the calculator's standard-library-only tests.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path


API_SOURCE = Path(__file__).resolve().parents[1] / "api" / "main.py"


def api_functions():
    parsed = ast.parse(API_SOURCE.read_text(encoding="utf-8"))
    nodes = [copy.deepcopy(node) for node in parsed.body
             if isinstance(node, ast.FunctionDef)
             and node.name in {"_engine_fingerprint", "health"}]
    for node in nodes:
        node.decorator_list = []
    namespace = {"Path": Path, "hashlib": hashlib,
                 "_ENGINE_COMMIT": "sha256:test-content", "_SOURCE_COMMIT": ""}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(API_SOURCE), "exec"), namespace)
    return namespace


class EngineFingerprint(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.functions = api_functions()
        self.fingerprint = self.functions["_engine_fingerprint"]
        for name, content in {
            "data/parsed_nikke.json": '{"마리안 : 퀸": {"weapon": "MG"}}',
            "data/base_stat_tables/cube.json": '{"cube": 15}',
            "calculator/timeline.py": 'FRAMES = 60\n',
            "runner/spec.py": 'LEVEL = 400\n',
            "api/main.py": 'SCHEMA_VERSION = "0.3.0"\n',
        }.items():
            self.write(name, content)

    def write(self, name, content):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_identical_contents_are_stable_across_root_paths_and_creation_order(self):
        expected = self.fingerprint(self.root)
        self.assertRegex(expected, r"^sha256:[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as other:
            for target in reversed(list(self.root.rglob("*"))):
                if target.is_file():
                    duplicate = Path(other) / target.relative_to(self.root)
                    duplicate.parent.mkdir(parents=True, exist_ok=True)
                    duplicate.write_bytes(target.read_bytes())
            self.assertEqual(expected, self.fingerprint(Path(other)))

    def test_data_calculator_runner_and_wrapper_changes_are_detected_and_revertible(self):
        expected = self.fingerprint(self.root)
        for name in ["data/parsed_nikke.json", "data/base_stat_tables/cube.json",
                     "calculator/timeline.py", "runner/spec.py", "api/main.py"]:
            with self.subTest(name=name):
                path = self.root / name
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                self.assertNotEqual(expected, self.fingerprint(self.root))
                path.write_bytes(before)
                self.assertEqual(expected, self.fingerprint(self.root))

    def test_new_character_data_is_detected_and_removal_restores_identity(self):
        expected = self.fingerprint(self.root)
        self.write("data/temporary/queen.json", '{"skills": []}')
        self.assertNotEqual(expected, self.fingerprint(self.root))
        (self.root / "data/temporary/queen.json").unlink()
        self.assertEqual(expected, self.fingerprint(self.root))

    def test_source_path_changes_are_part_of_identity(self):
        expected = self.fingerprint(self.root)
        (self.root / "runner/spec.py").rename(self.root / "runner/renamed.py")
        self.assertNotEqual(expected, self.fingerprint(self.root))

    def test_docs_profiles_sessions_environment_and_file_timestamps_are_ignored(self):
        expected = self.fingerprint(self.root)
        for name in ["README.md", "docs/temporary.md", "profiles/private.json",
                     "scraper/.session_cookie", "scraper/nikke_scraped.json",
                     ".env", "tests/test_example.py", "runner/__pycache__/cached.py"]:
            self.write(name, "must never affect or expose engine identity")
        for target in self.root.rglob("*"):
            if target.is_file():
                os.utime(target, (1, 1))
        self.assertEqual(expected, self.fingerprint(self.root))

    def test_health_keeps_wrapper_version_and_uses_engine_content_identity(self):
        result = self.functions["health"]()
        self.assertEqual(result, {"status": "ok", "version": "0.3.0", "commit": "sha256:test-content"})

    def test_render_commit_is_provenance_and_does_not_replace_content_identity(self):
        self.functions["_SOURCE_COMMIT"] = "render-git-revision"
        result = self.functions["health"]()
        self.assertEqual(result["source_commit"], "render-git-revision")
        self.assertEqual(result["commit"], "sha256:test-content")


if __name__ == "__main__":
    unittest.main()
