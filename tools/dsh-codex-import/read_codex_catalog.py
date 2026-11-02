#!/usr/bin/env python3
"""Write the Codex skill catalog to a local JSON file without an inference turn.

The script starts ``codex app-server --stdio``, performs the initialize
handshake, sends the ``initialized`` notification, and calls only
``skills/list``. It never sends a model prompt. The output contains absolute
local paths, so it is written with mode 0600 and should stay in
``.verification/`` or another ignored directory.

Usage (from the repository root)::

    python3 tools/dsh-codex-import/read_codex_catalog.py \\
        --cwd /absolute/project --output tools/dsh-codex-import/.verification/codex-catalog.json
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / ".verification" / "codex-catalog.json"
REQUEST_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
STDERR_TAIL_LINES = 40


class CatalogError(RuntimeError):
    """A catalog read failed before a usable result was written."""


class AppServer:
    """Minimal JSON-RPC client for ``codex app-server --stdio``."""

    def __init__(self):
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as error:
            raise CatalogError(
                "codex CLI not found in PATH; install Codex or run this script where the codex command is available"
            ) from error
        self.responses = queue.Queue()
        self.stderr_lines = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.responses.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip())
            del self.stderr_lines[:-STDERR_TAIL_LINES]

    def _stderr_tail(self):
        if not self.stderr_lines:
            return ""
        return "\ncodex stderr:\n" + "\n".join(self.stderr_lines)

    def _send(self, message):
        if self.process.poll() is not None:
            raise CatalogError(
                "codex app-server exited before a request could be sent "
                f"(exit code {self.process.returncode}){self._stderr_tail()}"
            )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, request_id, method, params):
        """Send one request and return its result."""
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                message = self.responses.get(timeout=REQUEST_TIMEOUT_SECONDS)
            except queue.Empty as error:
                raise CatalogError(
                    f"timed out after {REQUEST_TIMEOUT_SECONDS:g}s waiting for {method}{self._stderr_tail()}"
                ) from error
            if message is None:
                raise CatalogError(
                    f"codex app-server exited before replying to {method} "
                    f"(exit code {self.process.poll()}){self._stderr_tail()}"
                )
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise CatalogError(f"{method} failed: {message['error']}{self._stderr_tail()}")
            if "result" not in message:
                raise CatalogError(f"{method} returned no result{self._stderr_tail()}")
            return message["result"]

    def notify(self, method, params):
        """Send one JSON-RPC notification (no response expected)."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self):
        """Stop the app-server, escalating from terminate to kill."""
        if self.process.poll() is not None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            self.process.kill()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def write_private(path, text):
    """Write UTF-8 text with mode 0600, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Read the Codex skill catalog with skills/list only; no inference turn is started.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Project directory whose Codex skills are listed (default: current directory).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Catalog JSON path, written with mode 0600 (default: {DEFAULT_OUTPUT}).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cwd = (args.cwd or Path.cwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise CatalogError(f"--cwd is not a directory: {cwd}")
    output = (args.output or DEFAULT_OUTPUT).expanduser().resolve()

    server = AppServer()
    try:
        server.request(
            1,
            "initialize",
            {"clientInfo": {"name": "codex_dsh_skills_import", "version": "1"}},
        )
        server.notify("initialized", {})
        result = server.request(2, "skills/list", {"cwds": [str(cwd)], "forceReload": True})
    finally:
        server.close()

    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise CatalogError("skills/list result must be an object with a 'data' array")

    write_private(output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    data = result["data"]
    summary = {
        "output": str(output),
        "cwd": str(cwd),
        "entries": len(data),
        "skills": sum(len(entry.get("skills", [])) for entry in data if isinstance(entry, dict)),
    }
    if data and isinstance(data[0], dict):
        summary["entry_keys"] = list(data[0])
        skills = data[0].get("skills")
        if isinstance(skills, list) and skills and isinstance(skills[0], dict):
            summary["skill_keys"] = list(skills[0])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (CatalogError, OSError) as error:
        print(f"read_codex_catalog: {error}", file=sys.stderr)
        sys.exit(1)