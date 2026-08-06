"""Unit tests for stale_underlying halt arming, clearing, and recovery.

Regression cover for the 2026-08-06 incident: a 5.16s SPX spot gap at 09:27:47
— inside the pre-open market_data_lead_seconds warmup, when the cash index
publishes no prints by design — latched entries_halted for the whole session
with no operator path to resume.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, time as dt_time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

import ib_executor  # noqa: E402
from clear_stale_halt import (  # noqa: E402
    CLEARABLE_STALE_REASONS,
    filter_cleared_stale_reasons,
)
from live_config import LiveConfig  # noqa: E402
from session_recovery import recover_governor_state  # noqa: E402


class _FakeStream:
    """Minimal stand-in for the IB streaming provider's spot plumbing."""

    def __init__(self, spot: float, spot_age: float) -> None:
        self._spot = spot
        self._spot_age = spot_age

    def maybe_rebalance(self) -> None:
        pass

    def build_option_quotes(self, now):
        return []

    def spot(self) -> float:
        return self._spot

    def spot_age_seconds(self) -> float:
        return self._spot_age

    def spot_is_stale(self, max_age_seconds: float) -> bool:
        if max_age_seconds <= 0:
            return False
        return self._spot_age > max_age_seconds


def _provider(spot_age: float) -> ib_executor.IBSignalProvider:
    """An IBSignalProvider with its stream swapped for the fake."""
    provider = ib_executor.IBSignalProvider.__new__(ib_executor.IBSignalProvider)
    provider.live = LiveConfig(use_streaming_quotes=True, stale_spot_halt_seconds=5.0)
    provider._stream = _FakeStream(spot=7723.6, spot_age=spot_age)
    provider.baselines = None
    provider.session_vix = 15.83
    provider.last_signal_block_reason = ""
    provider.last_signal_diagnostics = {}
    return provider


class PreOpenStaleSpotTests(unittest.TestCase):
    """A stale spot before 09:30 must not arm the latching halt."""

    def test_pre_open_stale_spot_blocks_benignly(self) -> None:
        provider = _provider(spot_age=5.156)
        # 09:27:47 — the exact wall-clock of the incident, inside the 180s lead.
        now = datetime(2026, 8, 6, 9, 27, 47)

        _quotes, signal = provider.fetch(now)

        self.assertIsNone(signal)
        self.assertEqual(provider.last_signal_block_reason, "signal_warming")
        self.assertNotEqual(provider.last_signal_block_reason, "stale_underlying")
        self.assertEqual(
            provider.last_signal_diagnostics.get("sample_status"), "pre_open"
        )

    def test_stale_spot_after_open_still_halts(self) -> None:
        provider = _provider(spot_age=5.156)
        now = datetime(2026, 8, 6, 10, 2, 0)

        _quotes, signal = provider.fetch(now)

        self.assertIsNone(signal)
        self.assertEqual(provider.last_signal_block_reason, "stale_underlying")

    def test_healthy_spot_at_open_boundary_does_not_block(self) -> None:
        provider = _provider(spot_age=0.4)
        now = datetime(2026, 8, 6, 9, 30, 0)

        _quotes, _signal = provider.fetch(now)

        self.assertEqual(provider.last_signal_block_reason, "")

    def test_session_open_boundary_is_the_arming_line(self) -> None:
        self.assertEqual(ib_executor.SESSION_OPEN, dt_time(9, 30))
        provider = _provider(spot_age=99.0)

        provider.fetch(datetime(2026, 8, 6, 9, 29, 59))
        self.assertEqual(provider.last_signal_block_reason, "signal_warming")

        provider.fetch(datetime(2026, 8, 6, 9, 30, 0))
        self.assertEqual(provider.last_signal_block_reason, "stale_underlying")


class OperatorClearTests(unittest.TestCase):
    """CLEAR_STALE_HALT must reach stale_underlying, not just stale_quotes."""

    def test_stale_underlying_is_clearable(self) -> None:
        self.assertIn("stale_underlying", CLEARABLE_STALE_REASONS)
        self.assertIn("stale_quotes", CLEARABLE_STALE_REASONS)

    def test_filter_selects_only_stale_reasons(self) -> None:
        cleared = filter_cleared_stale_reasons(
            ["stale_underlying", "daily_loss", "kill_switch", "stale_quotes"]
        )
        self.assertEqual(cleared, ["stale_quotes", "stale_underlying"])

    def test_risk_halts_are_never_clearable(self) -> None:
        for reason in ("daily_loss", "account_guard", "kill_switch", "flatten",
                       "mark_unavailable", "partial_mark"):
            self.assertEqual(filter_cleared_stale_reasons([reason]), [])


class GovernorRecoveryTests(unittest.TestCase):
    """Restart-time recovery of stale_underlying halts and their clears."""

    def test_stale_underlying_halt_latches_without_a_clear(self) -> None:
        events = [
            {"event": "halt_entries", "reason": "stale_underlying"},
        ]
        gov = recover_governor_state(events, now=datetime(2026, 8, 6, 10, 0))
        self.assertTrue(gov.entries_halted)
        self.assertEqual(gov.halt_reasons, ["stale_underlying"])

    def test_operator_clear_releases_stale_underlying(self) -> None:
        events = [
            {"event": "halt_entries", "reason": "stale_underlying"},
            {
                "event": "governor_clear",
                "reason": "operator_clear_stale_quotes",
                "cleared_reasons": ["stale_underlying"],
            },
        ]
        gov = recover_governor_state(events, now=datetime(2026, 8, 6, 10, 0))
        self.assertFalse(gov.entries_halted)
        self.assertEqual(gov.halt_reasons, [])

    def test_auto_resume_event_replays_as_cleared(self) -> None:
        events = [
            {"event": "halt_entries", "reason": "stale_underlying"},
            {
                "event": "governor_clear",
                "reason": "stale_underlying_recovered",
                "cleared_reasons": ["stale_underlying"],
            },
        ]
        gov = recover_governor_state(events, now=datetime(2026, 8, 6, 10, 0))
        self.assertFalse(gov.entries_halted)

    def test_operator_clear_keeps_unrelated_risk_halt(self) -> None:
        events = [
            {"event": "halt_entries", "reason": "stale_underlying"},
            {"event": "halt_entries", "reason": "daily_loss"},
            {
                "event": "governor_clear",
                "reason": "operator_clear_stale_quotes",
                "cleared_reasons": ["stale_underlying"],
            },
        ]
        gov = recover_governor_state(events, now=datetime(2026, 8, 6, 10, 0))
        self.assertTrue(gov.entries_halted)
        self.assertEqual(gov.halt_reasons, ["daily_loss"])

    def test_operator_clear_cannot_release_a_flatten(self) -> None:
        events = [
            {"event": "halt_entries", "reason": "stale_underlying"},
            {"event": "kill_switch", "reason": "operator"},
            {
                "event": "governor_clear",
                "reason": "operator_clear_stale_quotes",
                "cleared_reasons": ["stale_underlying", "kill_switch"],
            },
        ]
        gov = recover_governor_state(events, now=datetime(2026, 8, 6, 10, 0))
        self.assertTrue(gov.flattened)
        self.assertTrue(gov.entries_halted)
        self.assertIn("kill_switch", gov.halt_reasons)


class AutoResumeGateTests(unittest.TestCase):
    """The in-loop auto-resume predicate, exercised as pure logic.

    Mirrors the condition in ib_executor's run loop: resume only when
    stale_underlying is the *sole* live halt reason and the feed has been
    healthy for the full confirmation window.
    """

    @staticmethod
    def _may_resume(
        *,
        resume_after: float,
        entries_halted: bool,
        flattened: bool,
        disconnect_halt: bool,
        upstream_halt: bool,
        halt_reasons: set,
        healthy_for: float,
    ) -> bool:
        return bool(
            resume_after > 0
            and entries_halted
            and not flattened
            and not disconnect_halt
            and not upstream_halt
            and halt_reasons == {"stale_underlying"}
            and healthy_for >= resume_after
        )

    def _base(self, **over):
        kwargs = dict(
            resume_after=30.0,
            entries_halted=True,
            flattened=False,
            disconnect_halt=False,
            upstream_halt=False,
            halt_reasons={"stale_underlying"},
            healthy_for=30.0,
        )
        kwargs.update(over)
        return self._may_resume(**kwargs)

    def test_resumes_after_sustained_healthy_feed(self) -> None:
        self.assertTrue(self._base())

    def test_holds_until_confirmation_window_elapses(self) -> None:
        self.assertFalse(self._base(healthy_for=29.9))

    def test_disabled_by_zero_threshold(self) -> None:
        self.assertFalse(self._base(resume_after=0.0))

    def test_never_resumes_a_flattened_session(self) -> None:
        self.assertFalse(self._base(flattened=True))

    def test_never_resumes_while_disconnected(self) -> None:
        self.assertFalse(self._base(disconnect_halt=True))
        self.assertFalse(self._base(upstream_halt=True))

    def test_never_resumes_past_another_risk_halt(self) -> None:
        for extra in ("daily_loss", "account_guard", "mark_unavailable",
                      "partial_mark", "stale_quotes", "kill_switch"):
            with self.subTest(extra=extra):
                self.assertFalse(
                    self._base(halt_reasons={"stale_underlying", extra})
                )

    def test_unknown_reason_fails_closed(self) -> None:
        # A halt recorded with no reason must never auto-clear.
        self.assertFalse(self._base(halt_reasons=set()))
        self.assertFalse(self._base(halt_reasons={"unspecified"}))


class ConfigDefaultTests(unittest.TestCase):
    def test_resume_window_defaults_on_and_exceeds_halt_threshold(self) -> None:
        live = LiveConfig()
        self.assertGreater(live.stale_underlying_resume_seconds, 0.0)
        self.assertGreaterEqual(
            live.stale_underlying_resume_seconds, live.stale_spot_halt_seconds
        )


if __name__ == "__main__":
    unittest.main()
