"""Small client for the installed Agents Anywhere DSH bridge (protocol 1.x)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from typing import Any

DEFAULT_ENDPOINT = Path.home() / ".agents-anywhere/dsh-bridge/endpoint.json"
LEGACY_ENDPOINT = Path.home() / ".dsh/agents-anywhere/bridge/endpoint.json"
NAMESPACE = "codex-dsh-workflow"
MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0


class BridgeError(RuntimeError):
    """Safe diagnostic; remote payloads and credentials are never included."""


def _endpoint(path: str | Path) -> dict[str, Any]:
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise BridgeError("DSH endpoint must be a private file owned by this user")
            value = json.loads(handle.read(65537))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BridgeError("Cannot read a valid private DSH endpoint; start DSH Desktop") from None
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("host") != "127.0.0.1"
            or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535
            or not isinstance(value.get("token"), str) or not value["token"]
            or type(value.get("pid")) is not int or value["pid"] <= 0):
        raise BridgeError("Invalid DSH loopback endpoint")
    return value


class _Client:
    def __init__(self, endpoint_path: str | Path, timeout: float):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.deadline = time.monotonic() + timeout
        self.buffer = bytearray()
        self.counter = 0
        paths = [endpoint_path]
        if Path(endpoint_path).expanduser() == DEFAULT_ENDPOINT:
            paths.append(LEGACY_ENDPOINT)
        failure = None
        for path in paths:
            try:
                endpoint = _endpoint(path)
                self.socket = socket.create_connection((endpoint["host"], endpoint["port"]), self.remaining())
                self.buffer.clear()
                self.identity = self.call("initialize", {
                    "authToken": endpoint["token"], "protocolVersion": "1.0", "runtime": "dsh",
                    "connectorId": NAMESPACE, "sessionNamespace": NAMESPACE,
                    "clientInfo": {"name": NAMESPACE, "version": "1.0"},
                })
                identity = self.identity.get("identity", {})
                if not isinstance(identity, dict) or identity.get("runtime") != "dsh" or not str(identity.get("protocolVersion", "")).startswith("1."):
                    raise BridgeError("DSH bridge returned an incompatible identity")
                return
            except (BridgeError, OSError) as exc:
                if hasattr(self, "socket"):
                    self.close()
                failure = exc if isinstance(exc, BridgeError) else BridgeError("DSH bridge is unavailable; start DSH Desktop")
        raise failure from None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("DSH bridge deadline exceeded")
        return remaining

    def close(self) -> None:
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        request_id = self.counter
        frame = (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                             "params": params or {}}, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(frame) > MAX_FRAME_BYTES:
            raise BridgeError("DSH request exceeds the frame limit")
        try:
            self.socket.settimeout(self.remaining())
            self.socket.sendall(frame)
            while True:
                self.remaining()
                while b"\n" not in self.buffer:
                    if len(self.buffer) >= MAX_FRAME_BYTES:
                        raise BridgeError("DSH response exceeds the frame limit")
                    self.socket.settimeout(self.remaining())
                    part = self.socket.recv(min(65536, MAX_FRAME_BYTES - len(self.buffer)))
                    if not part:
                        raise BridgeError("DSH bridge closed the connection")
                    self.buffer.extend(part)
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(raw) + 1 > MAX_FRAME_BYTES:
                    raise BridgeError("DSH response exceeds the frame limit")
                try:
                    response = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise BridgeError("DSH bridge returned invalid JSON") from None
                if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                    raise BridgeError("DSH bridge returned an invalid RPC envelope")
                if "id" not in response and isinstance(response.get("method"), str):
                    continue
                if response.get("id") != request_id:
                    raise BridgeError("DSH bridge returned an unexpected response ID")
                if "error" in response:
                    error = response["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    raise BridgeError(f"DSH RPC {method} failed (code {code if type(code) is int else 'unknown'})")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise BridgeError("DSH bridge returned an invalid result")
                return result
        except (OSError, TimeoutError):
            raise BridgeError("DSH bridge connection failed or deadline exceeded") from None


def _catalogs(client: _Client) -> dict[str, Any]:
    return {
        "models": client.call("catalog.listModels").get("models", []),
        "permissions": client.call("catalog.listPermissions").get("permissions", []),
        "agent_presets": client.call("catalog.listAgentPresets"),
    }


def _enabled(items):
    return [item for item in items if isinstance(item, dict) and item.get("enabled") is True]


def _model(catalog, explicit):
    available = []
    defaults = []
    for item in _enabled(catalog):
        if isinstance(item.get("selectionId"), str):
            available.append(item["selectionId"])
            if item.get("default") is True or item.get("isDefault") is True:
                defaults.append(item["selectionId"])
        available.extend(option["selectionId"] for option in _enabled(item.get("reasoningItems", []))
                         if isinstance(option.get("selectionId"), str))
    if explicit:
        if explicit not in available:
            raise BridgeError("Choose an enabled model selection ID from discover()")
        return explicit
    base = [item["selectionId"] for item in _enabled(catalog) if isinstance(item.get("selectionId"), str)]
    if len(defaults) == 1:
        return defaults[0]
    if len(base) == 1:
        return base[0]
    raise BridgeError("DSH exposes multiple models without a default; pass model from discover()")


def _permission(catalog, explicit):
    available = _enabled(catalog)
    if explicit:
        if not any(item.get("selectionId") == explicit for item in available):
            raise BridgeError("Choose an enabled permission selection ID from discover()")
        return explicit
    # Only known confining presets are eligible for an automatic selection.
    for preset in ("read-only", "workspace-write"):
        for item in available:
            if item.get("metadata", {}).get("preset") == preset:
                return item["selectionId"]
    raise BridgeError("No recognized confining permission is available; pass an explicit permission selection ID")


def _preset(catalog, explicit):
    chosen = explicit or catalog.get("configField", {}).get("default")
    if not chosen:
        available = _enabled(catalog.get("presets", []))
        if len(available) == 1:
            chosen = available[0].get("id")
    if not chosen or not any(item.get("id") == chosen for item in _enabled(catalog.get("presets", []))):
        raise BridgeError("Choose an enabled agent preset from discover()")
    return chosen


def discover(*, endpoint_path: str | Path = DEFAULT_ENDPOINT, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Read live catalogs and workspace metadata; never start a model turn."""
    with _Client(endpoint_path, timeout) as client:
        catalogs = _catalogs(client)
        defaults = {}
        for name, selector, catalog in (("model", _model, catalogs["models"]),
                                        ("permission", _permission, catalogs["permissions"]),
                                        ("preset", _preset, catalogs["agent_presets"])):
            try:
                defaults[name] = selector(catalog, None)
            except BridgeError:
                defaults[name] = None
        return {"available": True, "identity": client.identity.get("identity", {}),
                **catalogs, "defaults": defaults,
                "workspaces": client.call("workspace.list").get("workspaces", []),
                "capabilities": client.call("runtime.getCapabilities")}


def _session(session_id: str) -> dict[str, str]:
    if not isinstance(session_id, str) or not re.fullmatch(r"[\w-]{1,128}", session_id, flags=re.ASCII):
        raise ValueError("session_id must contain 1-128 ASCII letters, digits, underscores or hyphens")
    return {"sessionId": session_id,
            "externalSessionId": f"aa_{hashlib.sha256(NAMESPACE.encode()).hexdigest()[:16]}_{session_id}"}


def submit(cwd: str | Path, content: str, session_id: str, client_message_id: str,
           model: str | None = None, permission: str | None = None, preset: str | None = None,
           *, endpoint_path: str | Path = DEFAULT_ENDPOINT, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Create one durable DSH session and submit its first prompt, idempotently."""
    identity = _session(session_id)
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    if not isinstance(content, str) or not content.strip() or len(content) > 1000000:
        raise ValueError("content must contain non-whitespace text of at most 1000000 characters")
    if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 512:
        raise ValueError("client_message_id must contain 1-512 characters")
    with _Client(endpoint_path, timeout) as client:
        catalogs = _catalogs(client)
        selections = {"model": _model(catalogs["models"], model),
                      "permission": _permission(catalogs["permissions"], permission)}
        agent_preset = _preset(catalogs["agent_presets"], preset)
        result = client.call("session.createAndStart", {
            "sessionId": session_id, "content": content, "clientMessageId": client_message_id,
            "cwd": str(path.resolve()), "selections": selections, "agentPreset": agent_preset,
        })
        if result.get("accepted") is not True:
            raise BridgeError("DSH did not accept this task; inspect DSH Desktop before retrying")
        if result.get("sessionId") != session_id or result.get("externalSessionId") != identity["externalSessionId"]:
            raise BridgeError("DSH accepted the task with an unexpected session identity")
        return {"accepted": True, **identity, "cwd": str(path.resolve()),
                "clientMessageId": client_message_id, "selections": selections, "agentPreset": agent_preset}


def status(session_id: str, *, endpoint_path: str | Path = DEFAULT_ENDPOINT,
           timeout: float = DEFAULT_TIMEOUT, limit: int = 200) -> dict[str, Any]:
    """Return state plus the latest turn's answer; omit tool activity and history."""
    identity = _session(session_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    with _Client(endpoint_path, timeout) as client:
        state = client.call("session.getState", identity)
        snapshot = client.call("session.getSnapshot", {**identity, "limit": limit})
        items = list(snapshot.get("items", []))
        cursor = snapshot.get("nextCursor")
        while cursor:
            page = client.call("session.getSnapshot", {**identity, "cursor": cursor})
            items.extend(page.get("items", []))
            cursor = page.get("nextCursor")
        items = [item for item in items if isinstance(item, dict)]
        items.sort(key=lambda item: item.get("orderSeq", 0))
        end = next((item for item in reversed(items) if item.get("type") == "turn.end"), None)
        latest_start = next((item for item in reversed(items) if item.get("type") == "turn.start"), None)
        latest_user = next((item for item in reversed(items) if item.get("role") == "user"), None)
        end_order = end.get("orderSeq", -1) if end else -1
        terminal = bool(end and state.get("status") in ("idle", "error")
                        and (not latest_start or latest_start.get("orderSeq", 0) < end_order)
                        and (not latest_user or latest_user.get("orderSeq", 0) < end_order))
        turn_id = end.get("turnId") if terminal else (latest_start or {}).get("turnId")
        answers = [item.get("content", {}).get("text", "") for item in items
                   if terminal and item.get("turnId") == turn_id and item.get("role") == "assistant"
                   and item.get("type") == "message" and item.get("status") == "done"
                   and item.get("content", {}).get("kind") in ("markdown", "text")]
        return {**identity, "status": state.get("status"), "terminal": terminal,
                "outcome": end.get("status") if terminal else None,
                "reason": end.get("content", {}).get("reason") if terminal else None,
                "answer": next((text for text in reversed(answers) if isinstance(text, str) and text), ""),
                "turnId": turn_id, "sourceState": state.get("sourceState"),
                "selections": state.get("selections", {}), "watermark": snapshot.get("watermark"),
                "snapshotComplete": snapshot.get("snapshotComplete", False),
                "needsAttention": state.get("status") in ("waiting_approval", "blocked")}
