"""Author lineage tracking for trajectory provenance (Story 5.5, AR14).

Provides immutable append operations for the author_lineage metadata field.
Each entry uses the format ``{alias}@{fingerprint}`` where fingerprint is the
first 8 hex chars of the wallet address (no ``0x`` prefix) to avoid exposing
the full address.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

_FINGERPRINT_RE = re.compile(r"^[a-fA-F0-9]{8}$")
_ALIAS_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_ENTRY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}@[a-fA-F0-9]{8}$")


def wallet_fingerprint(address: str) -> str:
    """Extract the first 8 hex characters from a wallet address.

    Accepts addresses with or without ``0x`` prefix.
    """
    cleaned = address.removeprefix("0x").removeprefix("0X")
    if len(cleaned) < 8 or not _FINGERPRINT_RE.match(cleaned[:8]):
        raise ValueError(f"Invalid wallet address: cannot extract 8-char hex fingerprint")
    return cleaned[:8].lower()


def validate_alias(alias: str) -> str:
    """Trim and validate an author alias against the safe charset.

    The alias travels into immutable on-chain metadata, so it must not contain
    PII (email/phone) or shape-breaking characters like ``@`` / whitespace.
    """
    trimmed = (alias or "").strip()
    if not trimmed:
        raise ValueError("Author alias must not be empty")
    if not _ALIAS_RE.match(trimmed):
        raise ValueError(
            "Author alias must match [a-zA-Z0-9_-]{1,32} (no @, no spaces, no PII)",
        )
    return trimmed


def make_entry(alias: str, address: str) -> str:
    """Build an ``alias@fingerprint`` lineage entry."""
    safe_alias = validate_alias(alias)
    fp = wallet_fingerprint(address)
    return f"{safe_alias}@{fp}"


def append_author(
    trajectory: Dict[str, Any],
    alias: str,
    address: str,
) -> Dict[str, Any]:
    """Return a **new** trajectory with the author appended to lineage.

    - Deep-copies the input — the original is never mutated.
    - Initialises ``metadata.author_lineage`` to ``[]`` if missing.
    - Preserves existing lineage order.
    """
    new_traj = copy.deepcopy(trajectory)

    if "metadata" not in new_traj:
        new_traj["metadata"] = {}

    meta = new_traj["metadata"]
    lineage: List[str] = meta.get("author_lineage", [])
    if not isinstance(lineage, list):
        lineage = []

    entry = make_entry(alias, address)
    lineage.append(entry)
    meta["author_lineage"] = lineage

    return new_traj


def get_lineage(trajectory: Dict[str, Any]) -> List[str]:
    """Read ``metadata.author_lineage`` from a trajectory, defaulting to ``[]``."""
    meta = trajectory.get("metadata")
    if not isinstance(meta, dict):
        return []
    lineage = meta.get("author_lineage")
    if not isinstance(lineage, list):
        return []
    return list(lineage)
