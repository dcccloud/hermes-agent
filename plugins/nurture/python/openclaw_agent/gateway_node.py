"""
Gateway Node Client — connects to the Agent Gateway via WebSocket as a node.

Implements the OpenClaw node protocol so the Python device service is visible
to the Pi agent through the standard ``nodes`` tool.

Usage (standalone):
    python -m openclaw_agent.gateway_node \
        --gateway-url ws://127.0.0.1:18791 \
        --node-id nurture-myhost

Usage (embedded in server.py):
    from openclaw_agent.gateway_node import GatewayNodeClient
    client = GatewayNodeClient(url, node_id, executor, agent_pool)
    asyncio.create_task(client.run())
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 3

# All commands this node exposes
NURTURE_COMMANDS: List[str] = [
    "nurture.execute",
    "nurture.execute_task",
    "nurture.devices.list",
    "nurture.capabilities",
    "nurture.recipes.list",
    "nurture.recipes.get",
    "nurture.recipes.generate",
    "nurture.recipes.import",
    "nurture.graph.get",
    "nurture.graph.merge",
    "nurture.health",
]

# Capability update interval (seconds)
CAPABILITY_UPDATE_INTERVAL = 60


# ------------------------------------------------------------------
# Ed25519 device identity helpers (mirrors src/infra/device-identity.ts)
# ------------------------------------------------------------------

# DER prefix for Ed25519 SPKI (same 12 bytes as the TS implementation)
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_device_identity() -> Dict[str, Any]:
    """Generate a new Ed25519 device identity."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    raw_pub = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    device_id = hashlib.sha256(raw_pub).hexdigest()

    return {
        "deviceId": device_id,
        "publicKeyBase64Url": _base64url_encode(raw_pub),
        "_privateKey": private_key,
    }


def _load_or_create_device_identity(identity_path: Optional[str] = None) -> Dict[str, Any]:
    """Load persisted device identity or generate + persist a new one.

    The identity file is stored next to the Python config by default
    (``~/.openclaw/nurture/device.json``).  This ensures the device ID
    (= gateway node ID) is stable across restarts.
    """
    if identity_path is None:
        identity_path = str(
            Path.home() / ".openclaw" / "nurture" / "device.json"
        )

    path = Path(identity_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            priv_bytes = base64.b64decode(data["privateKeyB64"])
            private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
            raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            return {
                "deviceId": data["deviceId"],
                "publicKeyBase64Url": _base64url_encode(raw_pub),
                "_privateKey": private_key,
            }
        except Exception:
            logger.warning("Corrupt device identity at %s, regenerating", path)

    identity = _generate_device_identity()

    # Persist
    path.parent.mkdir(parents=True, exist_ok=True)
    priv_raw = identity["_privateKey"].private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    stored = {
        "version": 1,
        "deviceId": identity["deviceId"],
        "privateKeyB64": base64.b64encode(priv_raw).decode(),
        "createdAtMs": int(time.time() * 1000),
    }
    path.write_text(json.dumps(stored, indent=2) + "\n")
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return identity


def _build_device_auth_payload_v3(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: List[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
    plat: str,
    device_family: str,
) -> str:
    """Build the v3 device auth payload string (mirrors buildDeviceAuthPayloadV3 in TS)."""
    return "|".join([
        "v3",
        device_id,
        client_id,
        client_mode,
        role,
        ",".join(scopes),
        str(signed_at_ms),
        token,
        nonce,
        plat.lower().strip() if plat else "",
        device_family.lower().strip() if device_family else "",
    ])


def _sign_device_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
    """Sign the payload with Ed25519 and return base64url-encoded signature."""
    sig = private_key.sign(payload.encode("utf-8"))
    return _base64url_encode(sig)


class GatewayNodeClient:
    """WebSocket client that registers as a node with the Agent Gateway."""

    def __init__(
        self,
        url: str,
        node_id: str,
        *,
        token: str = "",
        display_name: str = "",
        command_handler: Optional[Callable] = None,
        capability_provider: Optional[Callable] = None,
    ):
        self.url = url
        self.node_id = node_id
        self.token = token
        self.display_name = display_name or f"Nurture Device Service ({node_id})"
        self._command_handler = command_handler
        self._capability_provider = capability_provider
        self._ws: Optional[ClientConnection] = None
        self._connected = False
        self._stopping = False
        self._pending: Dict[str, asyncio.Future] = {}
        # Persisted device identity (required by gateway protocol; stable across restarts)
        self._device_identity = _load_or_create_device_identity()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def device_id(self) -> str:
        """The gateway node ID (= device identity fingerprint)."""
        return self._device_identity["deviceId"]

    # ------------------------------------------------------------------
    # Main loop with auto-reconnect
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect and reconnect in a loop until stopped."""
        backoff = 1.0
        while not self._stopping:
            try:
                await self._connect_and_serve()
                backoff = 1.0  # reset on clean disconnect
            except Exception as exc:
                if self._stopping:
                    break
                logger.warning(
                    "Gateway node connection error: %s. Reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def stop(self) -> None:
        self._stopping = True
        self._connected = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _connect_and_serve(self) -> None:
        logger.info("Connecting to gateway at %s as node %s", self.url, self.node_id)
        async with websockets.connect(
            self.url,
            max_size=25 * 1024 * 1024,
            close_timeout=5,
        ) as ws:
            self._ws = ws

            # 1. Wait for connect.challenge event
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if not (msg.get("type") == "event" and msg.get("event") == "connect.challenge"):
                raise RuntimeError(f"Expected connect.challenge, got: {msg.get('event')}")

            nonce = (msg.get("payload") or {}).get("nonce", "")
            if not nonce:
                raise RuntimeError("connect.challenge missing nonce")

            # 2. Send connect request
            connect_id = str(uuid.uuid4())
            connect_req = self._build_connect_frame(connect_id, nonce)
            logger.debug("Sending connect frame: commands=%s platform=%s",
                         connect_req["params"].get("commands"),
                         connect_req["params"].get("client", {}).get("platform"))
            await ws.send(json.dumps(connect_req))

            # 3. Wait for hello-ok response
            hello = await self._wait_for_response(ws, connect_id, timeout=10)
            logger.debug("Connect response: ok=%s payload_keys=%s",
                         hello.get("ok"), list(hello.get("payload", {}).keys()) if hello.get("payload") else "none")
            if not hello.get("ok"):
                error = hello.get("error", {})
                raise RuntimeError(
                    f"Gateway connect rejected: {error.get('message', 'unknown')}"
                )

            self._connected = True
            logger.info("Connected to gateway as node %s (device_id=%s)", self.node_id, self.device_id)

            # 4. Start capability update task + message loop
            cap_task = asyncio.create_task(self._capability_update_loop())
            try:
                await self._message_loop(ws)
            finally:
                cap_task.cancel()
                self._connected = False

    def _build_connect_frame(self, req_id: str, nonce: str) -> Dict[str, Any]:
        plat = platform.system().lower()
        role = "node"
        scopes: List[str] = []
        signed_at_ms = int(time.time() * 1000)

        # Build and sign device auth payload (v3 format)
        di = self._device_identity
        payload = _build_device_auth_payload_v3(
            device_id=di["deviceId"],
            client_id="node-host",
            client_mode="node",
            role=role,
            scopes=scopes,
            signed_at_ms=signed_at_ms,
            token=self.token or "",
            nonce=nonce,
            plat=plat,
            device_family="",
        )
        signature = _sign_device_payload(di["_privateKey"], payload)

        params: Dict[str, Any] = {
            "minProtocol": PROTOCOL_VERSION,
            "maxProtocol": PROTOCOL_VERSION,
            "client": {
                "id": "node-host",
                "displayName": self.display_name,
                "version": "1.0.0",
                "platform": plat,
                "mode": "node",
                "instanceId": self.node_id,
            },
            "device": {
                "id": di["deviceId"],
                "publicKey": di["publicKeyBase64Url"],
                "signature": signature,
                "signedAt": signed_at_ms,
                "nonce": nonce,
            },
            "caps": ["nurture"],
            "commands": list(NURTURE_COMMANDS),
            "role": role,
            "scopes": scopes,
        }
        if self.token:
            params["auth"] = {"token": self.token}

        return {"type": "req", "id": req_id, "method": "connect", "params": params}

    async def _wait_for_response(
        self, ws: ClientConnection, req_id: str, timeout: float = 30
    ) -> Dict[str, Any]:
        """Read messages until we get the response matching req_id."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
            msg = json.loads(raw)
            if msg.get("type") == "res" and msg.get("id") == req_id:
                return msg
            # Could be other events during handshake — ignore
        raise TimeoutError(f"Timed out waiting for response to {req_id}")

    # ------------------------------------------------------------------
    # Message loop — handle incoming events
    # ------------------------------------------------------------------

    async def _message_loop(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "event":
                event_name = msg.get("event", "")
                if event_name == "node.invoke.request":
                    asyncio.create_task(self._handle_invoke(ws, msg.get("payload", {})))
                # tick events are just keepalives — ignore

            elif msg_type == "res":
                # Response to a request we sent (e.g., node.event or node.invoke.result)
                req_id = msg.get("id")
                if req_id and req_id in self._pending:
                    self._pending[req_id].set_result(msg)
                    del self._pending[req_id]

    # ------------------------------------------------------------------
    # Invoke handling
    # ------------------------------------------------------------------

    async def _handle_invoke(self, ws: ClientConnection, payload: Dict[str, Any]) -> None:
        """Process a node.invoke.request and send back the result."""
        invoke_id = payload.get("id", "")
        command = payload.get("command", "")
        params_json = payload.get("paramsJSON")
        params = json.loads(params_json) if params_json else {}

        logger.info("Invoke request: command=%s id=%s", command, invoke_id)
        t_start = time.time()

        try:
            if self._command_handler:
                result = await asyncio.to_thread(self._command_handler, command, params)
                ok = True
            else:
                result = {"error": "No command handler registered"}
                ok = False
        except Exception as exc:
            logger.exception("Command %s failed", command)
            result = None
            ok = False
            error_payload = {"code": "EXECUTION_ERROR", "message": str(exc)}

        t_handler_done = time.time()
        logger.info(
            "[timing] command=%s id=%s handler_done elapsed=%.3fs, awaiting event loop for send...",
            command, invoke_id, t_handler_done - t_start,
        )

        result_frame = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "node.invoke.result",
            "params": {
                "id": invoke_id,
                "nodeId": self.device_id,
                "ok": ok,
            },
        }
        if ok:
            result_frame["params"]["payloadJSON"] = json.dumps(result, default=str)
        else:
            if not ok and result is None:
                result_frame["params"]["error"] = error_payload
            else:
                result_frame["params"]["error"] = {
                    "code": "EXECUTION_ERROR",
                    "message": json.dumps(result, default=str),
                }

        try:
            await ws.send(json.dumps(result_frame))
            t_sent = time.time()
            logger.info(
                "[timing] command=%s id=%s result_sent elapsed_since_handler=%.3fs total=%.3fs",
                command, invoke_id, t_sent - t_handler_done, t_sent - t_start,
            )
        except Exception:
            logger.warning("Failed to send invoke result for %s", invoke_id)

    # ------------------------------------------------------------------
    # Capability updates
    # ------------------------------------------------------------------

    async def _capability_update_loop(self) -> None:
        """Periodically report capabilities to the gateway."""
        while self._connected and not self._stopping:
            try:
                await self._send_capability_update()
            except Exception:
                logger.debug("Capability update failed (best-effort)", exc_info=True)
            await asyncio.sleep(CAPABILITY_UPDATE_INTERVAL)

    async def _send_capability_update(self) -> None:
        if not self._ws or not self._connected:
            return

        payload: Dict[str, Any] = {}
        if self._capability_provider:
            try:
                payload = self._capability_provider()
            except Exception:
                logger.debug("Capability provider error", exc_info=True)
                return

        frame = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "node.event",
            "params": {
                "event": "nurture.capability.update",
                "payloadJSON": json.dumps(payload, default=str),
            },
        }
        await self._ws.send(json.dumps(frame))

    # ------------------------------------------------------------------
    # Utility: send a request and optionally wait for response
    # ------------------------------------------------------------------

    async def send_request(
        self, method: str, params: Dict[str, Any], *, wait: bool = False
    ) -> Optional[Dict[str, Any]]:
        if not self._ws or not self._connected:
            return None
        req_id = str(uuid.uuid4())
        frame = {"type": "req", "id": req_id, "method": method, "params": params}

        if wait:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = fut

        await self._ws.send(json.dumps(frame))

        if wait:
            return await asyncio.wait_for(fut, timeout=30)
        return None
