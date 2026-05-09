"""JWT authentication for the community FastAPI + MCP server.

Token shape (per ``mcp-http-protocol.md`` §2):
    payload = {
        "avatarId": "<uuid>",
        "agentId": "<plugin-config>",
        "exp": <unix-ts>,
        "scope": "agent" | "admin",
    }

Self-scoped enforcement: handlers extract avatarId from the token, never
from the request body. See ``boundary-contracts.md`` 不变量 5.

Keys live in ``$NURTURE_COMMUNITY_HOME/secrets/jwt-{private,public}.pem``.
Auto-generated on first run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


_DEFAULT_TOKEN_TTL = timedelta(days=30)
_ALG = "RS256"


@dataclass
class TokenPayload:
    """Decoded JWT contents."""
    avatar_id: str
    agent_id: str
    scope: str       # "agent" or "admin"
    expires_at: int  # unix epoch seconds


class JwtAuth:
    """Sign + verify Bearer tokens. Lazily generates a keypair on first
    use; persists to ``secrets_dir/jwt-private.pem`` + ``jwt-public.pem``.
    """

    def __init__(self, secrets_dir: Path) -> None:
        self._secrets_dir = Path(secrets_dir)
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        self._private_key_path = self._secrets_dir / "jwt-private.pem"
        self._public_key_path = self._secrets_dir / "jwt-public.pem"
        self._private_pem: Optional[bytes] = None
        self._public_pem: Optional[bytes] = None

    def _ensure_keys(self) -> None:
        if self._private_pem is not None and self._public_pem is not None:
            return

        if self._private_key_path.exists() and self._public_key_path.exists():
            self._private_pem = self._private_key_path.read_bytes()
            self._public_pem = self._public_key_path.read_bytes()
            return

        logger.info("nurture-community: generating new JWT RS256 keypair at %s", self._secrets_dir)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # Store with restrictive perms on the private key
        self._private_key_path.write_bytes(priv_pem)
        try:
            os.chmod(self._private_key_path, 0o600)
        except OSError:
            pass
        self._public_key_path.write_bytes(pub_pem)
        self._private_pem = priv_pem
        self._public_pem = pub_pem

    def issue(
        self,
        *,
        avatar_id: str,
        agent_id: str,
        scope: str = "agent",
        ttl: Optional[timedelta] = None,
    ) -> str:
        """Sign and return a Bearer token."""
        self._ensure_keys()
        now = datetime.now(timezone.utc)
        exp = now + (ttl or _DEFAULT_TOKEN_TTL)
        payload = {
            "avatarId": avatar_id,
            "agentId": agent_id,
            "scope": scope,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        return jwt.encode(payload, self._private_pem, algorithm=_ALG)

    def verify(self, token: str) -> TokenPayload:
        """Verify and decode a Bearer token. Raises ``jwt.InvalidTokenError``
        (or subclass) on any failure."""
        self._ensure_keys()
        decoded: Dict[str, Any] = jwt.decode(
            token,
            self._public_pem,
            algorithms=[_ALG],
        )
        avatar_id = str(decoded.get("avatarId") or "").strip()
        agent_id = str(decoded.get("agentId") or "").strip()
        scope = str(decoded.get("scope") or "agent").strip()
        exp = int(decoded.get("exp") or 0)
        if not avatar_id:
            raise jwt.InvalidTokenError("avatarId missing from payload")
        return TokenPayload(
            avatar_id=avatar_id, agent_id=agent_id, scope=scope, expires_at=exp,
        )

    def expiring_soon(self, payload: TokenPayload, *, within_days: int = 7) -> bool:
        """True when the token expires within *within_days*. Used by
        device side to trigger auto-refresh."""
        cutoff = datetime.now(timezone.utc) + timedelta(days=within_days)
        return payload.expires_at < int(cutoff.timestamp())
