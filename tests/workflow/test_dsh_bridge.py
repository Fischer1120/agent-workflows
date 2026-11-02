import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".agents/skills/codex-tiered-workflow/scripts"))

import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dsh_bridge as bridge


class FakeBridge:
    def __init__(self, models=None, items=None, status="idle", behavior=None):
        self.calls = []
        self.models = models if models is not None else [
            {"selectionId": "model-live", "enabled": True, "reasoningItems": []}]
        self.items = items or []
        self.state = status
        self.behavior = behavior
        parent = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while True:
                    try:
                        line = self.rfile.readline()
                    except ConnectionResetError:
                        return
                    if not line:
                        return
                    request = json.loads(line)
                    method = request["method"]
                    params = request["params"]
                    parent.calls.append((method, params))
                    if parent.behavior and parent.behavior(self, request):
                        continue
                    if method == "initialize":
                        assert params["authToken"] == "test-private-token"
                        result = {"identity": {"runtime": "dsh", "protocolVersion": "1.0"}}
                    elif method == "catalog.listModels":
                        result = {"models": parent.models}
                    elif method == "catalog.listPermissions":
                        result = {"permissions": [
                            {"selectionId": "unsafe", "enabled": True,
                             "metadata": {"preset": "danger-full-access"}},
                            {"selectionId": "confined", "enabled": True,
                             "metadata": {"preset": "workspace-write"}}]}
                    elif method == "catalog.listAgentPresets":
                        result = {"presets": [{"id": "standard", "enabled": True}],
                                  "configField": {"default": "standard"}}
                    elif method == "workspace.list":
                        result = {"workspaces": []}
                    elif method == "runtime.getCapabilities":
                        result = {"metadata": {"readOnly": False}}
                    elif method == "session.createAndStart":
                        result = {"accepted": True, **bridge._session(params["sessionId"])}
                    elif method == "session.getState":
                        result = {"status": parent.state, "selections": {}}
                    elif method == "session.getSnapshot":
                        result = {"items": parent.items, "snapshotComplete": True, "nextCursor": None}
                    else:
                        raise AssertionError(method)
                    encoded = (json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n").encode()
                    # Split writes exercise NDJSON framing without relying on TCP packet boundaries.
                    self.wfile.write(encoded[:7])
                    self.wfile.flush()
                    self.wfile.write(encoded[7:])
                    self.wfile.flush()

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.directory = tempfile.TemporaryDirectory()
        self.endpoint = Path(self.directory.name) / "endpoint.json"
        self.endpoint.write_text(json.dumps({"version": 1, "host": "127.0.0.1",
                                             "port": self.server.server_address[1],
                                             "token": "test-private-token", "pid": os.getpid()}))
        self.endpoint.chmod(0o600)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()


def item(order, type_, role="system", text=None, status="done", turn="turn-1", reason=None):
    content = {"kind": "markdown", "text": text} if text else {"kind": "turn_end", "reason": reason}
    return {"orderSeq": order, "type": type_, "role": role, "turnId": turn,
            "status": status, "content": content}


class BridgeTests(unittest.TestCase):
    def test_discover_reads_only_and_never_returns_token(self):
        with FakeBridge() as fake:
            result = bridge.discover(endpoint_path=fake.endpoint)
            self.assertTrue(result["available"])
            self.assertEqual(result["defaults"], {"model": "model-live", "permission": "confined", "preset": "standard"})
            self.assertNotIn("test-private-token", json.dumps(result))
            self.assertFalse(any(method.startswith("session.") for method, _ in fake.calls))

    def test_submit_uses_live_catalog_and_shared_cwd(self):
        with FakeBridge() as fake:
            result = bridge.submit(fake.directory.name, "Small task", "task_1", "message_1", endpoint_path=fake.endpoint)
            self.assertTrue(result["accepted"])
            payload = fake.calls[-1][1]
            self.assertEqual(payload["cwd"], str(Path(fake.directory.name).resolve()))
            self.assertEqual(payload["selections"]["permission"], "confined")
            self.assertEqual(payload["agentPreset"], "standard")

    def test_ambiguous_model_does_not_submit(self):
        models = [{"selectionId": value, "enabled": True} for value in ("one", "two")]
        with FakeBridge(models=models) as fake:
            with self.assertRaisesRegex(bridge.BridgeError, "multiple models"):
                bridge.submit(fake.directory.name, "Task", "task", "message", endpoint_path=fake.endpoint)
            self.assertFalse(any(method == "session.createAndStart" for method, _ in fake.calls))
            bridge.submit(fake.directory.name, "Task", "task", "message", model="two", endpoint_path=fake.endpoint)
            self.assertEqual(fake.calls[-1][1]["selections"]["model"], "two")

    def test_terminal_requires_turn_end_and_excludes_tools_reasoning(self):
        items = [item(1, "message", "user", "Task"), item(2, "turn.start"),
                 item(3, "system", "assistant", "private reasoning"),
                 item(4, "tool", "assistant", "tool output"),
                 item(5, "message", "assistant", "Final answer"),
                 item(6, "turn.end", reason={"kind": "completed"})]
        with FakeBridge(items=items) as fake:
            result = bridge.status("task", endpoint_path=fake.endpoint)
            self.assertTrue(result["terminal"])
            self.assertEqual(result["answer"], "Final answer")
            self.assertEqual(result["outcome"], "done")
        with FakeBridge(items=items[:-1]) as fake:
            self.assertFalse(bridge.status("task", endpoint_path=fake.endpoint)["terminal"])
        with FakeBridge(items=items, status="waiting_approval") as fake:
            result = bridge.status("task", endpoint_path=fake.endpoint)
            self.assertFalse(result["terminal"])
            self.assertTrue(result["needsAttention"])

    def test_old_turn_end_does_not_complete_queued_followup(self):
        items = [item(1, "turn.start"), item(2, "turn.end"), item(3, "message", "user", "New prompt")]
        with FakeBridge(items=items) as fake:
            self.assertFalse(bridge.status("task", endpoint_path=fake.endpoint)["terminal"])

    def test_read_only_has_priority_and_full_access_never_selected(self):
        catalog = [{"selectionId": "write", "enabled": True, "metadata": {"preset": "workspace-write"}},
                   {"selectionId": "read", "enabled": True, "metadata": {"preset": "read-only"}}]
        self.assertEqual(bridge._permission(catalog, None), "read")
        unsafe = [{"selectionId": "full", "enabled": True, "metadata": {"preset": "danger-full-access"}}]
        with self.assertRaises(bridge.BridgeError):
            bridge._permission(unsafe, None)
        self.assertEqual(bridge._permission(unsafe, "full"), "full")

    def test_failed_and_interrupted_turns_keep_distinct_outcomes(self):
        for outcome, reason, state in (("failed", "error", "error"),
                                       ("interrupted", "aborted", "idle")):
            with self.subTest(outcome=outcome), FakeBridge(items=[item(1, "turn.start"),
                item(2, "turn.end", status=outcome, reason={"kind": reason})], status=state) as fake:
                result = bridge.status("task", endpoint_path=fake.endpoint)
                self.assertTrue(result["terminal"])
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["reason"]["kind"], reason)

    def test_symlink_endpoint_is_rejected(self):
        with FakeBridge() as fake:
            alias = Path(fake.directory.name) / "alias.json"
            alias.symlink_to(fake.endpoint)
            with self.assertRaises(bridge.BridgeError):
                bridge.discover(endpoint_path=alias)

    def test_notifications_are_skipped_before_response(self):
        def notify(handler, request):
            handler.wfile.write(b'{"jsonrpc":"2.0","method":"runtime.event","params":{}}\n')
            handler.wfile.flush()
            return False
        with FakeBridge(behavior=notify) as fake:
            self.assertTrue(bridge.discover(endpoint_path=fake.endpoint)["available"])

    def test_private_endpoint_and_loopback_enforced(self):
        with FakeBridge() as fake:
            fake.endpoint.chmod(0o644)
            with self.assertRaisesRegex(bridge.BridgeError, "private file"):
                bridge.discover(endpoint_path=fake.endpoint)
            fake.endpoint.chmod(0o600)
            raw = json.loads(fake.endpoint.read_text())
            raw["host"] = "example.com"
            fake.endpoint.write_text(json.dumps(raw))
            with self.assertRaisesRegex(bridge.BridgeError, "loopback"):
                bridge.discover(endpoint_path=fake.endpoint)

    def test_deadline(self):
        def delay(handler, request):
            time.sleep(0.12)
            return True
        with FakeBridge(behavior=delay) as fake:
            with self.assertRaisesRegex(bridge.BridgeError, "deadline"):
                bridge.discover(endpoint_path=fake.endpoint, timeout=0.04)

    def test_remote_errors_do_not_expose_payload(self):
        def error(handler, request):
            handler.wfile.write((json.dumps({"jsonrpc": "2.0", "id": request["id"],
                "error": {"code": -1, "message": "test-private-token secret prompt"}}) + "\n").encode())
            handler.wfile.flush()
            return True
        with FakeBridge(behavior=error) as fake:
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.discover(endpoint_path=fake.endpoint)
            self.assertNotIn("test-private-token", str(caught.exception))
            self.assertNotIn("secret prompt", str(caught.exception))

    def test_response_frame_limit(self):
        def oversized(handler, request):
            handler.wfile.write(b"x" * 2048)
            handler.wfile.flush()
            return True
        with FakeBridge(behavior=oversized) as fake, patch.object(bridge, "MAX_FRAME_BYTES", 1024):
            with self.assertRaisesRegex(bridge.BridgeError, "frame limit"):
                bridge.discover(endpoint_path=fake.endpoint)


if __name__ == "__main__":
    unittest.main()
