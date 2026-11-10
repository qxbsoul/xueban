"""Trajectory PII / secret scanner (Story 5.2, AR8).

Scans a trajectory dict recursively, stripping values that match known
PII / credential patterns before upload to 0G Storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class RemovedField:
    path: str
    pattern: str
    sample_masked: str


BLACKLIST_FIELD_NAMES = frozenset({
    "private_key", "api_key", "password", "authorization",
    "secret", "secret_key", "access_token", "refresh_token",
})

_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    )),
    ("phone_cn", re.compile(
        r"(?<!\d)1[3-9]\d{9}(?!\d)"
    )),
    ("phone_intl", re.compile(
        r"\+?[1-9]\d{7,14}"
    )),
    ("id_card_cn", re.compile(
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
    )),
    ("bank_card", re.compile(
        r"(?<!\d)\d{13,19}(?!\d)"
    )),
    ("api_key_sk", re.compile(
        r"sk-[A-Za-z0-9]{20,}"
    )),
    ("api_key_ghp", re.compile(
        r"ghp_[A-Za-z0-9]{36,}"
    )),
    ("api_key_google", re.compile(
        r"AIza[0-9A-Za-z\-_]{35}"
    )),
    ("jwt", re.compile(
        r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"
    )),
    ("eth_private_key", re.compile(
        r"0x[a-fA-F0-9]{64}"
    )),
    # Story 5-2 补丁 — 5 种新增凭证模式 (M1 修复)
    ("aws_access_key", re.compile(
        r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"
    )),
    ("slack_token", re.compile(
        r"xox[baprs]-[0-9A-Za-z\-]{10,}"
    )),
    ("github_fine_grained_pat", re.compile(
        r"github_pat_[A-Za-z0-9_]{80,110}"
    )),
    ("anthropic_api_key", re.compile(
        r"sk-ant-[A-Za-z0-9\-_]{20,}"
    )),
    ("bearer_token_header", re.compile(
        r"(?i)Bearer\s+[A-Za-z0-9\-_\.+/=]{20,}"
    )),
]


def _luhn_check(digits: str) -> bool:
    """Luhn algorithm — returns True if the digit string passes."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_sample(value: str, pattern_name: str) -> str:
    """Return a masked preview that does NOT reveal the original value."""
    if pattern_name == "email":
        parts = value.split("@")
        if len(parts) == 2:
            local, domain = parts
            domain_parts = domain.split(".")
            masked_local = local[0] + "***" if local else "***"
            masked_domain = domain_parts[0][0] + "***" if domain_parts[0] else "***"
            return f"{masked_local}@{masked_domain}.{domain_parts[-1]}" if len(domain_parts) > 1 else f"{masked_local}@{masked_domain}"
        return "***@***"
    if pattern_name in ("phone_cn", "phone_intl"):
        if len(value) >= 4:
            return value[:3] + "****" + value[-2:]
        return "***"
    if pattern_name == "id_card_cn":
        return value[:3] + "***" + value[-3:]
    if pattern_name == "bank_card":
        return value[:4] + " **** " + value[-4:]
    if pattern_name.startswith("api_key"):
        return value[:4] + "..." + value[-3:] if len(value) > 7 else value[:4] + "..."
    if pattern_name == "jwt":
        return "eyJ***...***"
    if pattern_name == "eth_private_key":
        return "0x" + value[2:6] + "..." + value[-4:]
    if pattern_name == "aws_access_key":
        return value[:4] + "****" + value[-4:]
    if pattern_name in ("slack_token", "github_fine_grained_pat", "anthropic_api_key"):
        return value[:8] + "..." + value[-3:] if len(value) > 11 else value[:8] + "..."
    if pattern_name == "bearer_token_header":
        parts = value.split(None, 1)
        if len(parts) == 2:
            tok = parts[1]
            return f"Bearer {tok[:6]}...{tok[-3:]}" if len(tok) > 9 else "Bearer ***"
        return "Bearer ***"
    return "***"


def _scan_string(value: str) -> List[Tuple[str, str, str]]:
    """Scan a single string value, return list of (pattern_name, matched, masked)."""
    hits: List[Tuple[str, str, str]] = []
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(value):
            matched = m.group()
            if name == "bank_card" and not _luhn_check(matched):
                continue
            if name == "phone_intl":
                if any(matched == h[1] for h in hits):
                    continue
            hits.append((name, matched, _mask_sample(matched, name)))
    return hits


_LINEAGE_ENTRY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}@[a-fA-F0-9]{8}$")


def _sanitize_author_lineage(value: Any, path: str, removed: List[RemovedField]) -> List[str]:
    """Filter ``metadata.author_lineage`` to entries matching ``alias@8hex``.

    Entries that fail the strict format are dropped (logged as removed).
    This enforces the contract instead of blanket-trusting the subtree.
    """
    if not isinstance(value, list):
        return []
    cleaned: List[str] = []
    for i, item in enumerate(value):
        if isinstance(item, str) and _LINEAGE_ENTRY_RE.match(item):
            cleaned.append(item)
        else:
            sample = item if isinstance(item, str) else type(item).__name__
            removed.append(RemovedField(
                path=f"{path}[{i}]",
                pattern="lineage_format",
                sample_masked=_mask_sample(str(sample)[:32], "lineage_format"),
            ))
    return cleaned


def _walk(
    obj: Any,
    path: str,
    removed: List[RemovedField],
) -> Any:
    """Recursively walk a dict/list, replacing sensitive values with '[REDACTED]'."""
    if path == "metadata.author_lineage":
        return _sanitize_author_lineage(obj, path, removed)
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for key, val in obj.items():
            key_lower = key.lower()
            if key_lower in BLACKLIST_FIELD_NAMES:
                removed.append(RemovedField(
                    path=f"{path}.{key}" if path else key,
                    pattern="blacklist_field",
                    sample_masked="[REDACTED]",
                ))
                cleaned[key] = "[REDACTED]"
                continue
            cleaned[key] = _walk(val, f"{path}.{key}" if path else key, removed)
        return cleaned
    if isinstance(obj, list):
        return [
            _walk(item, f"{path}[{i}]", removed)
            for i, item in enumerate(obj)
        ]
    if isinstance(obj, str):
        hits = _scan_string(obj)
        if hits:
            redacted = obj
            for name, matched, masked in hits:
                removed.append(RemovedField(
                    path=path,
                    pattern=name,
                    sample_masked=masked,
                ))
                redacted = redacted.replace(matched, "[REDACTED]")
            return redacted
        return obj
    return obj


def sanitize_trajectory(
    trajectory: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[RemovedField]]:
    """Scan *trajectory* for PII / secrets and return (cleaned_copy, removed_fields).

    Pure function — does not mutate the input.
    """
    removed: List[RemovedField] = []
    cleaned = _walk(trajectory, "", removed)
    return cleaned, removed
