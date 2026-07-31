"""IB NetLiquidation / BuyingPower overlay checks.

PnL halt/flatten dollars still use configured ``account_equity``. These guards
only assert the brokerage account has enough NetLiq and buying power to run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class AccountSnapshot:
    net_liquidation: Optional[float]
    buying_power: Optional[float]
    source: str = "ib"
    account: Optional[str] = None


@dataclass(frozen=True)
class AccountGuardResult:
    ok: bool
    reason: str
    snapshot: AccountSnapshot
    halt_entries: bool = False
    flatten: bool = False


def _parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def snapshot_from_account_values(values: Mapping[str, object]) -> AccountSnapshot:
    """Build a snapshot from a tag→value map (NetLiquidation, BuyingPower, …)."""
    net = _parse_float(values.get("NetLiquidation"))
    bp = _parse_float(values.get("BuyingPower"))
    if bp is None:
        bp = _parse_float(values.get("AvailableFunds"))
    return AccountSnapshot(net_liquidation=net, buying_power=bp, source="map")


def _read_account_tags(rows: Any, tags: dict, *, account: Optional[str] = None) -> None:
    for row in list(rows or []):
        row_account = str(getattr(row, "account", "") or "")
        if account and row_account != account:
            continue
        tag = str(getattr(row, "tag", "") or "")
        if tag in tags and tags[tag] is None:
            tags[tag] = getattr(row, "value", None)


def fetch_account_snapshot(
    ib: Any, *, timeout_sec: float = 3.0, account: Optional[str] = None
) -> AccountSnapshot:
    """Pull NetLiquidation and BuyingPower from a connected IB client.

    Prefer ``accountValues`` / cached ``accountSummary()`` — never call
    ``reqAccountSummary()`` on each poll (IB error 322: max summary requests).
    ``ib.accountSummary()`` only requests once when the wrapper cache is empty.
    """
    del timeout_sec  # retained for call-site compatibility; summary is cached.
    tags = {
        "NetLiquidation": None,
        "BuyingPower": None,
        "AvailableFunds": None,
    }
    try:
        values_fn = getattr(ib, "accountValues", None)
        if callable(values_fn):
            _read_account_tags(values_fn(), tags, account=account)

        if tags["NetLiquidation"] is None or (
            tags["BuyingPower"] is None and tags["AvailableFunds"] is None
        ):
            summary_fn = getattr(ib, "accountSummary", None)
            if callable(summary_fn):
                _read_account_tags(summary_fn(), tags, account=account)
    except Exception:
        pass
    snap = snapshot_from_account_values(tags)
    return AccountSnapshot(
        net_liquidation=snap.net_liquidation,
        buying_power=snap.buying_power,
        source="ib",
        account=account,
    )


def check_startup_account_guard(
    snapshot: AccountSnapshot,
    *,
    account_equity: float,
    netliq_min_ratio: float,
    buying_power_min_ratio: float,
    require_values: bool = True,
) -> AccountGuardResult:
    """Fail session start if NetLiq / BP are missing or below configured floors."""
    if snapshot.net_liquidation is None or snapshot.buying_power is None:
        if require_values:
            return AccountGuardResult(
                ok=False,
                reason="account_values_missing",
                snapshot=snapshot,
            )
        return AccountGuardResult(ok=True, reason="", snapshot=snapshot)

    min_net = account_equity * netliq_min_ratio
    min_bp = account_equity * buying_power_min_ratio
    if snapshot.net_liquidation < min_net:
        return AccountGuardResult(
            ok=False,
            reason=(
                f"netliq_below_min ({snapshot.net_liquidation:,.0f} "
                f"< {min_net:,.0f})"
            ),
            snapshot=snapshot,
        )
    if snapshot.buying_power < min_bp:
        return AccountGuardResult(
            ok=False,
            reason=(
                f"buying_power_below_min ({snapshot.buying_power:,.0f} "
                f"< {min_bp:,.0f})"
            ),
            snapshot=snapshot,
        )
    return AccountGuardResult(ok=True, reason="", snapshot=snapshot)


def check_loop_account_guard(
    snapshot: AccountSnapshot,
    *,
    account_equity: float,
    netliq_halt_ratio: float,
    netliq_flatten_ratio: float = 0.0,
    flatten_on_netliq_breach: bool = False,
) -> AccountGuardResult:
    """In-loop overlay: halt (and optionally flatten) when NetLiq erodes."""
    if snapshot.net_liquidation is None:
        return AccountGuardResult(
            ok=True,
            reason="account_values_unavailable",
            snapshot=snapshot,
            halt_entries=False,
        )
    halt_floor = account_equity * netliq_halt_ratio
    if snapshot.net_liquidation < halt_floor:
        flatten = False
        if (
            flatten_on_netliq_breach
            and netliq_flatten_ratio > 0
            and snapshot.net_liquidation < account_equity * netliq_flatten_ratio
        ):
            flatten = True
        return AccountGuardResult(
            ok=False,
            reason=(
                f"netliq_halt ({snapshot.net_liquidation:,.0f} "
                f"< {halt_floor:,.0f})"
            ),
            snapshot=snapshot,
            halt_entries=True,
            flatten=flatten,
        )
    return AccountGuardResult(ok=True, reason="", snapshot=snapshot)
