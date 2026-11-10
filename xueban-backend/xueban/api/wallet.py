"""0G Wallet API — read-only status + activity feed.

Endpoints:
  GET /api/wallet/status     — connection state + masked address + network
  GET /api/wallet/activity   — recent on-chain operations (publish/fork/update)
  GET /api/wallet/skin-pack  — current Skin Pack 7 slot values

Backing store
-------------
A single JSON file at ``$XUEBAN_DATA_DIR/wallet.json`` (defaults to
``.xueban/wallet.json``). When the file is missing we return the design's
hard-coded sample data so the Hi-Fi v2 Settings page can render its Wallet
section out-of-the-box, while leaving room for the user to swap in a real 0G
integration later.

This is intentionally read-only — there are no PUT/POST routes. The file
schema mirrors the tuples shown in ``hf-pages.jsx`` -> ``HfSettings`` and the
4-stat grid + activity rows + 7-slot Skin Pack used by the design.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("XUEBAN_DATA_DIR", ".xueban"))
WALLET_FILE = DATA_DIR / "wallet.json"


# ---------------------------------------------------------------------------
# Default payload — mirrors HfSettings hard-coded values in hf-pages.jsx
# ---------------------------------------------------------------------------

_DEFAULT_WALLET: Dict[str, Any] = {
    "connected": True,
    "network": "0G Galileo Testnet",
    "address": "0x3f7a4d12ab98bc91e2d5c4f8a6b0e1d3c5f7a9b1",
    "stats": {
        "teams_published": 3,
        "cids_held": 12,
        "gas_budget_og": 0.42,
        "citations": 7,
    },
    "activity": [
        {
            "timestamp": "09:14",
            "op": "team.publish",
            "label": "论文深读小队",
            "cid": "cid://Qm…3bx2a",
            "status": "ok",
        },
        {
            "timestamp": "昨日",
            "op": "team.fork",
            "label": "from Newsroom",
            "cid": "cid://Qm…f0d12",
            "status": "ok",
        },
        {
            "timestamp": "昨日",
            "op": "team.update",
            "label": "Rebuttal 起草",
            "cid": "cid://Qm…99cda",
            "status": "warn",
        },
    ],
    "skin_pack": [
        {"slot": "bg",     "value": "#0A0A0A"},
        {"slot": "panel",  "value": "#0F0F12"},
        {"slot": "fg",     "value": "#FAFAFA"},
        {"slot": "muted",  "value": "#A1A1AA"},
        {"slot": "accent", "value": "#A855F7"},
        {"slot": "ink",    "value": "#0A0A0A"},
        {"slot": "border", "value": "#27272A"},
    ],
}


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class WalletStats(BaseModel):
    teams_published: int
    cids_held: int
    gas_budget_og: float
    citations: int


class WalletStatusResponse(BaseModel):
    connected: bool
    network: str
    address_masked: str
    address_full: str  # full hex — UI uses this for "复制" button
    stats: WalletStats


class WalletActivityItem(BaseModel):
    timestamp: str
    op: str
    label: str
    cid: str
    status: str  # "ok" | "warn" | "err"


class WalletActivityResponse(BaseModel):
    items: List[WalletActivityItem]


class SkinPackSlot(BaseModel):
    slot: str
    value: str


class SkinPackResponse(BaseModel):
    slots: List[SkinPackSlot]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_wallet() -> Dict[str, Any]:
    """Read wallet payload from disk; fall back to design defaults."""
    try:
        data = json.loads(WALLET_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("wallet.json must be a JSON object")
        # Shallow merge so partial files still render.
        merged = {**_DEFAULT_WALLET, **data}
        # Ensure nested keys are present.
        merged["stats"] = {**_DEFAULT_WALLET["stats"], **(merged.get("stats") or {})}
        if not isinstance(merged.get("activity"), list):
            merged["activity"] = _DEFAULT_WALLET["activity"]
        if not isinstance(merged.get("skin_pack"), list):
            merged["skin_pack"] = _DEFAULT_WALLET["skin_pack"]
        return merged
    except FileNotFoundError:
        return _DEFAULT_WALLET
    except Exception as exc:  # JSON decode error, etc.
        logger.warning("wallet.json unreadable, using defaults: %s", exc)
        return _DEFAULT_WALLET


def _mask_address(address: str) -> str:
    """Render ``0x3f7a4d12ab98bc91…`` as ``0x3f7a · 4d12 · ab98 · bc91``.

    Matches the visual pattern in hf-pages.jsx (4 dot-separated 4-char chunks
    drawn from the leading 18 hex chars of the address).
    """
    if not isinstance(address, str) or not address:
        return ""
    addr = address.lower()
    if addr.startswith("0x"):
        body = addr[2:]
        prefix = "0x"
    else:
        body = addr
        prefix = ""
    chunks: List[str] = []
    for i in range(0, min(16, len(body)), 4):
        chunks.append(body[i : i + 4])
    if not chunks:
        return prefix
    head = chunks[0]
    rest = chunks[1:]
    return f"{prefix}{head}" + ("" if not rest else " · " + " · ".join(rest))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", response_model=WalletStatusResponse)
async def get_wallet_status() -> WalletStatusResponse:
    """Return wallet connection state + masked + full address + headline stats."""
    data = _read_wallet()
    stats_in = data.get("stats", {})
    return WalletStatusResponse(
        connected=bool(data.get("connected", False)),
        network=str(data.get("network", "0G Galileo Testnet")),
        address_masked=_mask_address(str(data.get("address", ""))),
        address_full=str(data.get("address", "")),
        stats=WalletStats(
            teams_published=int(stats_in.get("teams_published", 0)),
            cids_held=int(stats_in.get("cids_held", 0)),
            gas_budget_og=float(stats_in.get("gas_budget_og", 0.0)),
            citations=int(stats_in.get("citations", 0)),
        ),
    )


@router.get("/activity", response_model=WalletActivityResponse)
async def get_wallet_activity() -> WalletActivityResponse:
    """Return the recent on-chain activity feed (publish / fork / update)."""
    data = _read_wallet()
    items_in = data.get("activity", [])
    items: List[WalletActivityItem] = []
    for raw in items_in:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(
                WalletActivityItem(
                    timestamp=str(raw.get("timestamp", "")),
                    op=str(raw.get("op", "")),
                    label=str(raw.get("label", "")),
                    cid=str(raw.get("cid", "")),
                    status=str(raw.get("status", "ok")),
                )
            )
        except Exception:
            # Skip malformed entries rather than 500.
            continue
    return WalletActivityResponse(items=items)


@router.get("/skin-pack", response_model=SkinPackResponse)
async def get_wallet_skin_pack() -> SkinPackResponse:
    """Return the active Skin Pack — 7 named color slots (bg/panel/fg/...)."""
    data = _read_wallet()
    slots_in = data.get("skin_pack", [])
    slots: List[SkinPackSlot] = []
    for raw in slots_in:
        if not isinstance(raw, dict):
            continue
        slot = str(raw.get("slot", "")).strip()
        value = str(raw.get("value", "")).strip()
        if slot and value:
            slots.append(SkinPackSlot(slot=slot, value=value))
    return SkinPackResponse(slots=slots)
