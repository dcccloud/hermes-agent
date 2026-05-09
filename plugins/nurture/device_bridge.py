"""HTTP bridge to the Python device service.

Synchronous httpx client used by Hermes tool handlers (which run on the
agent thread) and by the prompt hook. The async background sync thread
(Phase 3) will use a separate ``httpx.AsyncClient``.

Replaces ``extensions/nurture/src/device-bridge.ts``. Endpoint paths and
return shapes are 1:1 with OpenClaw to keep the Python service unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class DeviceBridge:
    """Sync HTTP client to the local Python device service (server.py).

    Default 30 minute timeout — VLM / multi-step navigation can take a
    while. Tools that know they're synchronous (health, capabilities)
    can pass a tighter ``timeout`` per call.
    """

    def __init__(self, host: str, port: int, *, timeout: float = 1800.0) -> None:
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # -- raw request --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            resp = self._client.request(
                method,
                url,
                json=json_body,
                params=params,
                timeout=timeout,
            )
        except httpx.RequestError as e:
            raise RuntimeError(f"Device service {method} {path} request failed: {e}") from e

        if resp.status_code >= 400:
            text = ""
            try:
                text = resp.text
            except Exception:
                pass
            raise RuntimeError(
                f"Device service {method} {path} returned {resp.status_code}: {text[:500]}"
            )
        return resp.json()

    # -- endpoints ----------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/health", timeout=5.0)

    def get_capabilities(self) -> Dict[str, Any]:
        """{ version, apps: { <app>: { operations, graph_summary } } }"""
        return self._request("GET", "/api/capabilities", timeout=10.0)

    def list_devices(self) -> List[Dict[str, Any]]:
        """[ { serial, status, model? } ]"""
        return self._request("GET", "/api/devices", timeout=10.0)

    def execute(
        self,
        *,
        operation: str,
        device_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        return self._request(
            "POST",
            "/api/execute",
            json_body={
                "operation": operation,
                "device_id": device_id,
                "params": params or {},
            },
        )

    def import_recipe(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/recipes/import", json_body=req)

    def merge_graph(self, app_name: str, pages: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/api/graph/{app_name}/merge",
            json_body={"pages": pages},
        )

    def get_pending_traces(
        self,
        *,
        operation: Optional[str] = None,
        limit: Optional[int] = None,
        include_consumed: bool = False,
        outcomes: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if operation:
            params["operation"] = operation
        if limit:
            params["limit"] = limit
        if include_consumed:
            params["include_consumed"] = "true"
        if outcomes:
            params["outcomes"] = outcomes
        return self._request("GET", "/api/traces/pending", params=params or None)

    def get_events(
        self,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        event_types: Optional[str] = None,
    ) -> Dict[str, Any]:
        """{ events, cursor, has_more }"""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        if event_types:
            params["event_types"] = event_types
        return self._request("GET", "/api/events", params=params or None, timeout=30.0)

    def get_persona(self, app: str) -> Dict[str, Any]:
        """{ exists, raw, app }"""
        return self._request("GET", f"/api/persona/{app}", timeout=5.0)

    def init_persona(self, app: str) -> Dict[str, Any]:
        # Persona init runs 4 device scrape ops + LLM — long timeout
        return self._request("POST", f"/api/persona/{app}/init", timeout=600.0)

    def get_goals(self) -> Dict[str, Any]:
        """{ exists, raw }"""
        return self._request("GET", "/api/goals", timeout=5.0)
