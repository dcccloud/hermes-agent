"""AvatarRegistry — manages community avatar identities, capabilities,
and lifecycle.

Each device agent has an "avatar" in the community — its identity and
capability profile. Tracks persistent avatar identity, capability
indexing, and APP-level routing.

In Avatar-Hermes the connection layer is HTTP+MCP (not WebSocket node),
so ``currentNodeId`` here represents the agent's most-recent connection
session id (or None if offline).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Literal, Optional

from .stores._helpers import now_ms


Platform = Literal["android", "ios", "harmonyos"]


@dataclass
class AvatarCapability:
    app: str
    operations: List[str] = field(default_factory=list)
    hasRecipe: bool = False


@dataclass
class AvatarRecord:
    avatarId: str
    deviceModel: str
    displayName: str
    platform: Platform
    capabilities: List[AvatarCapability] = field(default_factory=list)
    registeredAt: int = 0
    lastSeenAt: int = 0
    currentNodeId: Optional[str] = None  # session id or None when offline

    def to_dict(self) -> Dict[str, object]:
        return {
            "avatarId": self.avatarId,
            "deviceModel": self.deviceModel,
            "displayName": self.displayName,
            "platform": self.platform,
            "capabilities": [asdict(c) for c in self.capabilities],
            "registeredAt": self.registeredAt,
            "lastSeenAt": self.lastSeenAt,
            "currentNodeId": self.currentNodeId,
        }


class AvatarRegistry:
    """In-memory avatar registry. Does NOT persist to disk by design —
    the source of truth is recipe/trace ingest. The registry is rebuilt
    on community server restart from incoming traffic.
    """

    def __init__(self) -> None:
        self._avatars: Dict[str, AvatarRecord] = {}

    def register(
        self,
        *,
        avatar_id: str,
        device_model: str,
        display_name: str,
        platform: Platform,
        capabilities: Optional[List[AvatarCapability]] = None,
        node_id: Optional[str] = None,
    ) -> AvatarRecord:
        """Register or update an avatar."""
        existing = self._avatars.get(avatar_id)
        now = now_ms()

        if existing is not None:
            existing.deviceModel = device_model
            existing.displayName = display_name
            existing.lastSeenAt = now
            if capabilities is not None:
                existing.capabilities = list(capabilities)
            if node_id is not None:
                existing.currentNodeId = node_id
            return existing

        record = AvatarRecord(
            avatarId=avatar_id,
            deviceModel=device_model,
            displayName=display_name,
            platform=platform,
            capabilities=list(capabilities) if capabilities is not None else [],
            registeredAt=now,
            lastSeenAt=now,
            currentNodeId=node_id,
        )
        self._avatars[avatar_id] = record
        return record

    def unregister(self, avatar_id: str) -> None:
        """Mark avatar as offline (does NOT delete record)."""
        record = self._avatars.get(avatar_id)
        if record is not None:
            record.currentNodeId = None

    def get(self, avatar_id: str) -> Optional[AvatarRecord]:
        return self._avatars.get(avatar_id)

    def list(self) -> List[AvatarRecord]:
        return list(self._avatars.values())

    def list_online(self) -> List[AvatarRecord]:
        return [a for a in self.list() if a.currentNodeId is not None]

    def get_by_app(self, app: str) -> List[AvatarRecord]:
        return [
            a for a in self.list()
            if any(c.app == app for c in a.capabilities)
        ]

    def update_capabilities(
        self, avatar_id: str, capabilities: List[AvatarCapability]
    ) -> None:
        record = self._avatars.get(avatar_id)
        if record is not None:
            record.capabilities = list(capabilities)
            record.lastSeenAt = now_ms()

    def heartbeat(self, avatar_id: str) -> None:
        record = self._avatars.get(avatar_id)
        if record is not None:
            record.lastSeenAt = now_ms()

    @property
    def size(self) -> int:
        return len(self._avatars)
