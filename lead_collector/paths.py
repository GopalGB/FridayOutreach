"""Pure path safety predicates shared by local input and output boundaries."""

from __future__ import annotations

from pathlib import PurePath


def is_protected_component(component: str) -> bool:
    lower = component.casefold()
    return lower in {".git", ".ssh", ".aws", ".codex", ".agents"} or lower.startswith((".env", "id_rsa")) or lower.endswith((".key", ".pem")) or any(token in lower for token in ("secret", "credential", "token"))


def is_protected_path(path: str) -> bool:
    return any(is_protected_component(part) for part in PurePath(path).parts)
