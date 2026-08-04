"""Upstream IB connectivity health from TWS system events (codes 1100-1102).

The socket-level disconnect breaker (``ib_connection.ib_is_connected``) only
fires when the local API socket drops. On 2026-08-04 TWS lost its *upstream*
connection to IB servers seven times (error 1100) while the local socket stayed
"connected": quotes froze, stop-confirmation timers kept advancing on stale
marks, and entries stayed armed. This monitor turns those system events into an
explicit trading-health state the executor can act on immediately.

IB system event semantics:
  1100  Connectivity between IB and TWS has been lost.
  1101  Connectivity restored -- data lost. Market data subscriptions must be
        re-established.
  1102  Connectivity restored -- data maintained.

Related market-data farm messages (2103/2105 broken, 2104/2106/2158 OK) are
tracked for diagnostics but do not by themselves trip the breaker: farm
messages routinely flap at session open while quotes remain valid.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import List, Optional

UPSTREAM_LOST_CODE = 1100
UPSTREAM_RESTORED_DATA_LOST_CODE = 1101
UPSTREAM_RESTORED_DATA_OK_CODE = 1102

_FARM_BROKEN_CODES = frozenset({2103, 2105})
_FARM_OK_CODES = frozenset({2104, 2106, 2158})


@dataclass(frozen=True)
class HealthTransition:
    """One upstream state change, consumed by the executor loop."""

    kind: str  # "upstream_lost" | "upstream_restored"
    code: int
    at_monotonic: float
    resubscribe_required: bool = False


@dataclass
class ConnectionHealthMonitor:
    """Tracks upstream TWS<->IB connectivity from the IB error event stream.

    Thread-safety note: ib_insync dispatches events on the same asyncio loop
    the executor runs on, so plain attributes are sufficient.
    """

    upstream_down: bool = False
    resubscribe_required: bool = False
    lost_at_monotonic: Optional[float] = None
    lost_count: int = 0
    restored_count: int = 0
    farm_broken_count: int = 0
    _pending: List[HealthTransition] = field(default_factory=list)

    def on_ib_error(self, error_code: int) -> None:
        now = _time.monotonic()
        code = int(error_code)
        if code == UPSTREAM_LOST_CODE:
            self.lost_count += 1
            if not self.upstream_down:
                self.upstream_down = True
                self.lost_at_monotonic = now
                self._pending.append(
                    HealthTransition("upstream_lost", code, now)
                )
        elif code in (
            UPSTREAM_RESTORED_DATA_LOST_CODE,
            UPSTREAM_RESTORED_DATA_OK_CODE,
        ):
            data_lost = code == UPSTREAM_RESTORED_DATA_LOST_CODE
            if data_lost:
                # Subscriptions are gone even if we never saw the 1100.
                self.resubscribe_required = True
            if self.upstream_down or data_lost:
                self.restored_count += 1
                self.upstream_down = False
                self._pending.append(
                    HealthTransition(
                        "upstream_restored", code, now,
                        resubscribe_required=data_lost,
                    )
                )
        elif code in _FARM_BROKEN_CODES:
            self.farm_broken_count += 1

    def consume_transitions(self) -> List[HealthTransition]:
        """Return and clear transitions since the last loop iteration.

        Consecutive lost/restored pairs are preserved in order so the loop can
        both log the outage and run the restore path once.
        """
        out = list(self._pending)
        self._pending.clear()
        return out

    def mark_resubscribed(self) -> None:
        self.resubscribe_required = False

    def outage_seconds(self) -> float:
        if not self.upstream_down or self.lost_at_monotonic is None:
            return 0.0
        return max(0.0, _time.monotonic() - self.lost_at_monotonic)

    def snapshot(self) -> dict:
        """Compact state for heartbeats and telemetry."""
        return {
            "upstream_down": self.upstream_down,
            "resubscribe_required": self.resubscribe_required,
            "outage_seconds": round(self.outage_seconds(), 1),
            "lost_count": self.lost_count,
            "restored_count": self.restored_count,
            "farm_broken_count": self.farm_broken_count,
        }
