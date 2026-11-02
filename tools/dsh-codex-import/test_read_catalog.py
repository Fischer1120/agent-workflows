#!/usr/bin/env python3
"""Tests for read_codex_catalog.py.

Run with: python3 -m unittest -v test_read_catalog.py

A fake ``codex app-server`` executable is placed first on PATH. The tests
assert the initialize/initialized/skills-list handshake, the private output
mode, and that the child process is stopped when the catalog call fails.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parent / "read_codex_catalog.py"

FAKE_SERVER = '''#!/usr/bin/env python3
import json
import os
import sys
import time

log_path = os.environ.get("FAKE_CODEX_LOG")
mode = os.environ.get("FAKE_CODEX_MODE", "ok")


def log(message):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(message + "\\n")


log("start")
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    log(method)
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif method == "initialized":
        continue
    elif method == "skills/list":
        if mode == "error":
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "boom"}}), flush=True)
            log("pid " + str(os.getpid()))
            time.sleep(60)
        elif mode == "malformed":
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"nope": True}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"data": [{"cwd": "/fake", "skills": [{"name": "Alpha", "path": "/fake/SKILL.md", "enabled": True, "scope": "global"}]}]}}), flush=True)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "error": {"message": "unexpected " + str(method)}}), flush=True)
'''


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ReadCatalogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="read catalog test "))
        self.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.empty_bin = self.root / "empty-bin"
        self.empty_bin.mkdir()
        self.log = self.root / "fake.log"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.output = self.root / "out" / "codex-catalog.json"
        self.write_fake_server()

    def write_fake_server(self):
        codex = self.bin / "codex"
        codex.write_text(FAKE_SERVER, encoding="utf-8")
        codex.chmod(0o755)

    def environment(self, mode="ok", include_bin=True):
        env = dict(os.environ)
        env["FAKE_CODEX_LOG"] = str(self.log)
        env["FAKE_CODEX_MODE"] = mode
        if include_bin:
            env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        else:
            env["PATH"] = str(self.empty_bin)
        return env

    def run_script(self, mode="ok", args=None, include_bin=True):
        command = [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            str(self.cwd),
            "--output",
            str(self.output),
        ]
        command.extend(args or [])
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self.environment(mode=mode, include_bin=include_bin),
        )

    def logged_methods(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_successful_catalog_read_uses_only_the_handshake_and_skills_list(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["entries"], 1)
        self.assertEqual(summary["skills"], 1)
        self.assertEqual(Path(summary["output"]).resolve(), self.output.resolve())
        catalog = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(catalog["data"][0]["skills"][0]["name"], "Alpha")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(
            self.logged_methods(),
            ["start", "initialize", "initialized", "skills/list"],
            "only initialize, initialized, and skills/list may be sent",
        )

    def test_skills_list_error_exits_nonzero_and_stops_the_child(self):
        result = self.run_script(mode="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("boom", result.stderr)
        self.assertFalse(self.output.exists())
        entries = self.logged_methods()
        self.assertIn("skills/list", entries)
        pid_line = next((entry for entry in entries if entry.startswith("pid ")), None)
        self.assertIsNotNone(pid_line, "fake server did not record its pid")
        pid = int(pid_line.split()[1])
        deadline = time.time() + 3
        while process_exists(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_exists(pid), "app-server child must be stopped on failure")

    def test_unexpected_result_shape_exits_nonzero(self):
        result = self.run_script(mode="malformed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'data' array", result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_codex_command_reports_the_prerequisite(self):
        result = self.run_script(include_bin=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("codex CLI not found", result.stderr)


if __name__ == "__main__":
    unittest.main()