# Stop Calibration Test Plan — 2026-06-30

Narrowed follow-up after Test 1 (unconditional baseline) and Test 2 (stop + structure
matrix). **Stops are required** — no-stop variants are out of scope. Goal: calibrate
stop mechanics to what MBH described on the investor call and in the DDQ, using wide
asymmetric wings as the fixed structural substrate (Test 2 showed tight wings + any
stop config underperforms).

**Note:** The raw call transcript is not in this repo (referenced in
`execution_tracker.md` as reviewed offline). Stop rules below are synthesized from
`strategy_reconstruction_spec.md`, `README.md`, `position_snapshot_analysis.md` (DDQ
extract), and `diligence_reconciliation.md`.

---

## What MBH said about stops (call + DDQ)

### From the investor call (via reconstruction spec)

| Topic | Disclosed behavior |
|---|---|
| **Which leg** | Stop on the **short leg only** |
| **Long wing** | **Not sold** when the short is stopped; kept until EOD settlement |
| **Long wing payoff** | Can become profitable on large moves (retained convexity) |
| **Trigger** | Short-leg price reaches a **multiple of entry premium** |
| **Example** | ~$4 short-leg credit → stop around **$8 or $12** (i.e. **2×–3×** entry) |
| **Fill assumption (live)** | Broker **stop order on the short leg** (`live/ib_executor.py`) |
| **Structure** | Risk-defined **vertical spreads**, 15–25Δ shorts |

### From the DDQ (via `position_snapshot_analysis.md`)

| Topic | Disclosed behavior |
|---|---|
| **Trade win rate** | ~**65%** per individual trade |
| **Portfolio tail** | Max single-day loss if **all positions stop out with slippage**: ~**-2.25%** of capital |
| **Worst-case frequency** | Roughly **once per quarter** |
| **Daily loss framework** | Hard-stop worst case is a function of **total premium collected** that day |
| **Book evolution** | 11:00 snapshot balanced long/short; 15:00 snapshot **net long** — consistent with **stopped shorts leaving retained long wings**, not buying extra OTM longs every tranche |

### Still unknown (explicit proof request in diligence)

- Stop based on **bid vs ask vs mark vs midpoint** vs spread mark
- Whether stop is **immediate on touch** or **confirmed**
- Daily loss behavior: **halt new entries only** vs **flatten entire book**
- Exact multiple: fixed 2×, fixed 3×, or **regime-dependent** ($8 *or* $12 on $4 credit)

### What our tests already proved

| Finding | Implication for stop plan |
|---|---|
| 2.0× stop → 39.6% stop rate, 61.5% win, **8.9% CAGR** | **Too tight** vs ~65% win target |
| 3.0× stop → 25.3% stop rate, 74.3% win, **13.3% CAGR** | Directionally right; still below MBH returns |
| Stopped trades (2.0×) → **- $8,683/trade**; held to close → **+ $6,033/trade** | Stop **fill path** dominates P&L, not strike delta |
| Wide wings (200/75) + no stop → **27.8% CAGR** but **-25% worst day** | Wings fix payoff shape; **portfolio governor** needed alongside stops |
| Wide wings + 2.0× stop → **11.1% CAGR**, -6.6% worst day | Wings help, but 2.0× still over-stops |
| Mechanical net-long overlay every tranche → **negative CAGR** | **Not** how MBH achieves net-long book; use **retained wings after stop** only |

---

## Fixed substrate for all stop tests

Hold constant across every variant unless noted:

- **Wings:** put 200pt / call 75pt (MBH snapshot shape)
- **Short delta:** 15–25Δ (20Δ target) — already validated
- **Cadence:** unconditional for Test 3A–3B (isolate stop mechanics); selective entry added in 3D
- **Sizing:** 31 contracts/tranche, $13M equity (research default)
- **Retained long wing after stop:** ON (call requirement)
- **Fills:** sell short at bid, buy wing at ask; stop fill at short **ask** (conservative default)

---

## Test 3A — Stop multiple on wide wings (priority 1)

**Question:** Where on the call’s 2×–3× range does wide-wing + stop land?

| ID | Stop multiple | Notes |
|---|---|---|
| 3A-1 | 2.0× | Current baseline on wide wings (Test 2 partial: 11.1% CAGR) |
| 3A-2 | 2.5× | Midpoint of call example |
| 3A-3 | 3.0× | Upper call example ($12 on $4) |
| 3A-4 | 3.5× | Extension if 3.0× still over-stops |

**Metrics:** CAGR, Sharpe, worst day, stop rate, win rate, E[trade], avg loss on stopped vs non-stopped.

**Acceptance (call-aligned):**
- Win rate → **60–68%**
- Stop rate → **25–40%** (consistent with ~65% win)
- Worst day → **< -7%** (interim; DDQ portfolio cap is -2.25% with governor)
- CAGR → beat 3A-1 by ≥ 3pp

**Expected winner:** 3A-3 or 3A-4.

---

## Test 3B — Stop trigger and fill mechanics (priority 2)

Take the best multiple from 3A (likely 3.0×) and vary **how** the stop fires. These
map directly to the open diligence question (“bid, ask, mark, or broker stop?”).

| ID | Trigger rule | Fill rule |
|---|---|---|
| 3B-1 | Short **ask** ≥ N × entry **bid** | Fill at ask (current simulator) |
| 3B-2 | Short **ask** ≥ N × entry **bid** | Fill at ask + 1 tick slippage |
| 3B-3 | Short **mid** ≥ N × entry **mid** | Fill at ask |
| 3B-4 | **Spread mark** loss ≥ 1.5× entry credit | Close spread (buy short ask, sell long bid) |
| 3B-5 | Spread mark loss ≥ 2.0× entry credit | Same, wider |
| 3B-6 | Ask ≥ stop for **2 consecutive 1-min bars** | Fill at ask (anti-whipsaw) |

**Acceptance:**
- Reduce avg loss per stopped trade vs 3B-1 without increasing worst day by > 2pp
- Stop rate should **not** drop below ~20% (otherwise stop is cosmetic)

**Out of scope:** Midpoint-only triggers without ask fill — not conservative enough for live.

---

## Test 3C — Post-stop behavior (priority 3)

MBH’s net-long book is largely **retained wings**, not fresh overlay buys. Test
**behavior after a stop**, not extra long purchases.

| ID | Rule | Rationale |
|---|---|---|
| 3C-1 | Retain wing only (baseline) | Call + DDQ |
| 3C-2 | + **Same-side re-entry cooldown** 120 min | Iteration plan; reduces stop clustering |
| 3C-3 | + **Max 2 stops per side per day** | Already in simulator; verify on wide wings |
| 3C-4 | + **No same-strike re-entry** after stop | Prevent repeated loss at one strike |
| 3C-5 | 3C-2 + 3C-4 combined | Best post-stop discipline |

**Acceptance:**
- Improve worst-day and stop-cluster days (e.g. tariff shock, reversal days)
- Win rate stays in 60–68% band

---

## Test 3D — Portfolio-level stop stack (priority 4)

DDQ: worst case if **all** positions stop with slippage ≈ **-2.25%**. Call + prior
research: need a **daily loss governor**, not just per-trade stops.

Apply to **best config from 3A + 3B + 3C**:

| ID | New-entry halt | Flatten governor |
|---|---|---|
| 3D-1 | -2.25% marked P&L | None |
| 3D-2 | -2.25% | Flatten all at **-2.25%** |
| 3D-3 | -2.25% | Flatten at **-3.5%** (prior `best` profile) |

**Acceptance (DDQ-aligned):**
- Worst day → **-4% to -5%** (MBH claimed bad day)
- CAGR sacrifice vs no-governor ≤ 5pp
- Sharpe improves vs 3A winner

This is the most likely path to **MBH-like risk shape with stops enabled**.

---

## Test 3E — Microstructure validation (priority 5)

Use existing `stop_microstructure_classifier.py` + 10s ThetaData windows on the **top
2 configs** from 3D.

**Classify stopped trades:**
- Real directional failure → stop was correct; keep rule
- Quote spike / whipsaw → consider 3B-6 confirmation
- Late-day reversal → time-of-day stop widening or entry block

**Acceptance:** ≥ 70% of stops on finalist config are “real failure” or “ordinary path”
(not quote artifacts). If artifact rate > 30%, promote 3B-6 before live.

---

## Test 3F — Selective entry × calibrated stop (priority 6, after 3D)

Only after stop + governor are calibrated on unconditional wide-wing substrate:

| ID | Entry policy | Stop config |
|---|---|---|
| 3F-1 | Green-day refit gates + wide wings | 3D winner |
| 3F-2 | cheap_premium ablation + wide wings | 3D winner |
| 3F-3 | Event/time controls + wide wings | 3D winner |

**Acceptance:** Active days > 30%, CAGR ≥ 3D winner, worst day still < -5%.

---

## Explicitly excluded from this plan

| Excluded | Reason |
|---|---|
| No per-trade stop | User requirement; Test 2 showed unacceptable tail without wings anyway |
| Mechanical net-long overlay every tranche | Test 2: -0.8% CAGR; not call/DDQ behavior |
| Tight ~25pt wings as stop-test substrate | Dominated by stop noise; wrong MBH structure |
| Stop-only tuning on gated 6.8% cadence | Confounds stop calibration with selection artifact |
| New sleeve types (condor, 1DTE, trend debit) | Premature until core stop + wing stack works |

---

## Implementation checklist

1. **Run 3A-3 and 3A-4 immediately** — `wide_wings_stop_3.0` / `_3.5` missing from Test 2 matrix
2. Add simulator support for 3B variants (spread-value stop, confirmation bars, slippage) — small `StrategyConfig` extensions
3. Extend `stop_structure_matrix.py` → `stop_calibration_matrix.py` with 3A–3D only
4. Wire 3D flatten governor (already exists; combine with wide-wing defaults)
5. Score against DDQ targets in report: win rate, stop rate, worst day, retained-wing count vs snapshots

---

## Success definition (stop-calibrated MBH candidate)

A configuration passes Test 3 when **all** are true on 391 OOS days:

1. **Per-trade win rate** 60–68%
2. **Stop rate** 25–40%
3. **Positive E[trade]** on spread sleeve after costs
4. **CAGR** ≥ 20% (interim; full MBH ~30–40% needs selective entry + sizing from 3F)
5. **Worst day** ≥ -5% of equity with 3D governor
6. **Retained-wing book** grows on stop-heavy days (holdings recon vs DDQ 11:00 → 15:00 pattern)

---

## Recommended execution order

```
3A (multiples on wide wings)
  → 3B (trigger/fill on best multiple)
    → 3C (post-stop rules)
      → 3D (daily loss governor)
        → 3E (microstructure on finalists)
          → 3F (add selective entry)
```

**First command to run:** Test 3A with variants `wide_wings_stop_{2.0,2.5,3.0,3.5}` on full 391-day OOS.
