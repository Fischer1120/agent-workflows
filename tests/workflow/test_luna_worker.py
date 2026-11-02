import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".agents/skills/codex-tiered-workflow/scripts"))

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import luna_worker


class FakeAppServer:
    def __init__(self, thread_response, events=()):
        self.thread_response = thread_response
        self.events = list(events)
        self.pending = []
        self.calls = []
        self.sent = []
        self.closed = False

    def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "thread/start":
            return self.thread_response
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(f"Unexpected AppServer request: {method}")

    def send(self, message):
        self.sent.append(message)

    def receive(self, deadline):
        if not self.events:
            raise AssertionError("Worker requested an unexpected AppServer event")
        return self.events.pop(0)

    def close(self):
        self.closed = True


class LunaWorkerTests(unittest.TestCase):
    def make_job(self, root):
        root = Path(root)
        self.cwd = str(root)
        job = {"cwd": str(root), "sandbox": "workspace-write", "timeout": 1}
        (root / "job.json").write_text(json.dumps(job))
        (root / "brief.txt").write_text("Complete the small task.")
        return job

    def thread_response(self, service_tier):
        return {
            "model": "gpt-6-luna",
            "reasoningEffort": "max",
            "serviceTier": service_tier,
            "cwd": self.cwd,
            "sandbox": {"type": "workspaceWrite"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "thread": {"id": "thread-1"},
        }

    def run_with_server(self, job_dir, server):
        with mock.patch.object(luna_worker, "AppServer", return_value=server) as app_server:
            luna_worker.run(Path(job_dir))
        app_server.assert_called_once()

    def read_status(self, job_dir):
        return json.loads((Path(job_dir) / "status.json").read_text())

    def test_default_service_tier_fails_before_turn_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.make_job(temp_dir)
            server = FakeAppServer(self.thread_response("default"))

            self.run_with_server(temp_dir, server)

            status = self.read_status(temp_dir)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["effective"]["serviceTier"], "default")
            self.assertTrue(status["error"])
            self.assertIn("thread/start", [method for method, _ in server.calls])
            self.assertNotIn("turn/start", [method for method, _ in server.calls])
            self.assertTrue(server.closed)

    def test_priority_tier_agent_message_and_completed_turn_save_result(self):
        events = [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {"type": "agentMessage", "text": "Task result"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            self.make_job(temp_dir)
            server = FakeAppServer(self.thread_response("priority"), events)

            self.run_with_server(temp_dir, server)

            status = self.read_status(temp_dir)
            self.assertEqual(status["effective"]["model"], "gpt-6-luna")
            self.assertEqual(status["effective"]["reasoningEffort"], "max")
            self.assertEqual(status["effective"]["serviceTier"], "priority")
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["turn_status"], "completed")
            self.assertEqual((Path(temp_dir) / "result.txt").read_text(), "Task result\n")
            self.assertIn("turn/start", [method for method, _ in server.calls])

    def test_server_permission_request_needs_attention_without_approval(self):
        permission_request = {
            "id": 42,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            self.make_job(temp_dir)
            server = FakeAppServer(self.thread_response("priority"), [permission_request])

            self.run_with_server(temp_dir, server)

            status = self.read_status(temp_dir)
            self.assertEqual(status["status"], "needs_attention")
            self.assertEqual(status["request_method"], "item/commandExecution/requestApproval")
            self.assertFalse(any("id" in message for message in server.sent))
            self.assertTrue(server.closed)

    def test_broader_sandbox_is_rejected_before_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.make_job(temp_dir)
            response = self.thread_response("priority")
            response["sandbox"] = {"type": "dangerFullAccess"}
            server = FakeAppServer(response)
            self.run_with_server(temp_dir, server)
            self.assertEqual(self.read_status(temp_dir)["status"], "failed")
            self.assertNotIn("turn/start", [method for method, _ in server.calls])

    def test_wrong_project_is_rejected_before_task(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.make_job(temp_dir)
            response = self.thread_response("priority")
            response["cwd"] = str(Path(temp_dir).parent)
            server = FakeAppServer(response)
            self.run_with_server(temp_dir, server)
            self.assertEqual(self.read_status(temp_dir)["status"], "failed")
            self.assertNotIn("turn/start", [method for method, _ in server.calls])


if __name__ == "__main__":
    unittest.main()
