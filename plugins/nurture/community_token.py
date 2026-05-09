"""Bearer token persistence for the device-side community connector.

Token lifecycle:
  1. Plugin starts. If ``community-token.json`` exists, load it.
  2. If missing or expiring within 7 days, call ``POST /api/avatar/register``
     (or future re-register endpoint) to get a fresh one.
  3. All MCP + REST requests use the token in ``Authorization: Bearer ...``.
  4. On 401 from the community, force re-registration on next tick.

Storage: ``$HERMES_HOME/nurture/agent/community-token.json``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class CommunityToken:
    avatarId: str
    token: str
    expiresAt: int  # unix epoch seconds

    def is_expiring_soon(self, *, within_days: int = 7) -> bool:
        cutoff = (
            datetime.now(timezone.utc).timestamp()
            + within_days * 24 * 60 * 60
        )
        return self.expiresAt < cutoff

    def to_dict(self) -> Dict[str, Any]:
        return {
            "avatarId": self.avatarId,
            "token": self.token,
            "expiresAt": self.expiresAt,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CommunityToken":
        return cls(
            avatarId=str(d.get("avatarId") or ""),
            token=str(d.get("token") or ""),
            expiresAt=int(d.get("expiresAt") or 0),
        )


class TokenStore:
    """Disk-backed token cache."""

    def __init__(self, token_path: Path) -> None:
        self._path = Path(token_path)
        self._cached: Optional[CommunityToken] = None

    def load(self) -> Optional[CommunityToken]:
        if self._cached is not None:
            return self._cached
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        token = CommunityToken.from_dict(data)
        if not token.token:
            return None
        self._cached = token
        return token

    def save(self, token: CommunityToken) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(token.to_dict(), indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._cached = token

    def clear(self) -> None:
        self._cached = None
        try:
            self._path.unlink()
        except OSError:
            pass


def register_avatar_sync(
    *,
    community_url: str,
    agent_id: str,
    device_id: str,
    device_model: str = "",
    platform: str = "android",
    timeout: float = 30.0,
) -> CommunityToken:
    """Hit ``POST /api/avatar/register`` and return the new token.

    Synchronous (httpx.Client) so the plugin's on_session_start hook can
    use it directly.
    """
    url = f"{community_url.rstrip('/')}/api/avatar/register"
    payload = {
        "deviceId": device_id,
        "agentId": agent_id,
        "platform": platform,
        "deviceModel": device_model,
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return CommunityToken(
        avatarId=str(data.get("avatarId") or ""),
        token=str(data.get("token") or ""),
        expiresAt=int(data.get("expiresAt") or 0),
    )


def ensure_token(
    *,
    store: TokenStore,
    community_url: str,
    agent_id: str,
    device_id: str,
    device_model: str = "",
    platform: str = "android",
) -> Optional[CommunityToken]:
    """Return a valid token, registering or refreshing as needed.

    Returns None if no community URL is configured or registration
    fails (community offline). The caller should treat None as "skip
    community sync this tick".
    """
    if not community_url:
        return None

    existing = store.load()
    if existing is not None and not existing.is_expiring_soon():
        return existing

    try:
        token = register_avatar_sync(
            community_url=community_url,
            agent_id=agent_id,
            device_id=device_id,
            device_model=device_model,
            platform=platform,
        )
    except Exception as e:
        logger.warning(
            "nurture: avatar registration failed (community offline?): %s", e
        )
        return existing  # may be None or stale-but-still-functional

    store.save(token)
    logger.info(
        "nurture: registered avatar %s (token expires at %s)",
        token.avatarId,
        datetime.fromtimestamp(token.expiresAt, tz=timezone.utc).isoformat(),
    )
    return token
