from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any


class AuthenticationError(ValueError):
    pass


class AuthorizationError(ValueError):
    pass


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    caller_agent_id: str
    allowed_source_systems: frozenset[str]
    can_manage_governance: bool = False
    can_read_audit_content: bool = False


class StaticCredentialStore:
    def __init__(self, credentials: list[dict[str, Any]]) -> None:
        self._credentials: dict[str, AuthContext] = {}
        for item in credentials:
            token = item.get("token")
            token_sha256 = item.get("token_sha256")
            if bool(token) == bool(token_sha256):
                raise ValueError("each credential requires exactly one of token or token_sha256")
            digest = token_sha256 or hashlib.sha256(token.encode()).hexdigest()
            sources = frozenset(item.get("allowed_source_systems") or [])
            manage_governance = item.get("can_manage_governance", False)
            if not isinstance(manage_governance, bool):
                raise ValueError("can_manage_governance must be a boolean")
            read_audit_content = item.get("can_read_audit_content", False)
            if not isinstance(read_audit_content, bool):
                raise ValueError("can_read_audit_content must be a boolean")
            if not sources and not manage_governance:
                raise ValueError("allowed_source_systems must not be empty")
            self._credentials[digest] = AuthContext(
                tenant_id=item["tenant_id"],
                caller_agent_id=item["caller_agent_id"],
                allowed_source_systems=sources,
                can_manage_governance=manage_governance,
                can_read_audit_content=read_audit_content,
            )

    @classmethod
    def from_json(cls, value: str) -> StaticCredentialStore:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("MEMORY_AUTH_CREDENTIALS_JSON must be a JSON array")
        return cls(parsed)

    def authenticate(self, authorization: str | None) -> AuthContext:
        scheme, separator, token = (authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise AuthenticationError("missing or invalid Bearer credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for expected, context in self._credentials.items():
            if secrets.compare_digest(digest, expected):
                return context
        raise AuthenticationError("invalid Bearer credential")

    @staticmethod
    def authorize_source(context: AuthContext, source_system: str) -> None:
        if source_system not in context.allowed_source_systems:
            raise AuthorizationError("credential cannot write this source_system")
