# Selective 0DTE Short-Premium Overlay — Straddle + Iron Condor Test Plan (v3)

Date: 2026-07-16  
Supersedes: v2 (straddle-only)

Goal: find **when** selling a small short-premium overlay **on top of** production verticals (`p3_poststop_cooldown_120` + FOMC 13:30 + VIX≥20 put+25) has edge — using the four signal features — without overfitting.

Two structures share the same gate / split / promotion framework:

| Structure | Role | Loss profile |
|---|---|---|
| **Short ATM straddle** | Unbounded benchmark / upper-bound premium harvest | Unlimited (path stop only) |
| **Iron condor** | Primary candidate for live — **losses bounded** by wing width − credit | Max loss = `(width − credit) × contracts × 100` per side pair |

**IC low-vol / fee rule (hard default):** skip IC when `VIX open < 15` **or** net credit cannot clear `5×` round-trip fees (8 legs × $0.79). Low-vol ICs are fee-dominated; `B_always_IC_novix` is the control that turns this off.

Promotion preference: if IC and straddle both pass selection on the same gate family, **prefer IC** for holdout/live unless straddle sleeve Calmar is materially better *and* worst-day stays within promotion gates.

Reuse existing simulator condor plumbing where possible (`use_condor_sleeve`, `select_condor_entries` in `mbh_simulator.py`) — but **entry gates follow this plan’s Phase A bands**, not the older default condor z-floors alone.

## Status snapshot

| Phase | Status | Notes |
|---|---|---|
| W0 — `realized_vs_implied_z` | **DONE** | `simulator/rv_feature.py` + backfill; live path wired |
| A1 — straddle quintiles | **DONE** | `data/selective_straddle_overlay/phase_a/` (sel 872 / hold 635) |
| A1c — iron condor quintiles | **NEXT (with B)** | Same days/features; counterfactual IC PnL by quintile |
| A2–A4 — joints / FOMC / MFE | Deferred | Optional; only if Phase B is ambiguous |
| B — entry gates × structure | **NEXT** | Same gates for STRADDLE and IC |
| C — exits | Blocked on B winner | IC: soft manage inside hard max-loss bound |
| D — sizing / book interaction | Blocked on C | |
| E — sealed holdout promote | Final | Prefer IC if competitive |

---

## Anti-overfit protocol (mandatory)

| Split | Role | Dates |
|---|---|---|
| Rolling feature train | 40 eligible days for z-score baselines | walk-forward |
| **Selection** | Rank gates / pick winners **only here** | OOS ≤ `2023-12-29` |
| **Holdout (sealed)** | Promotion validation only | OOS ≥ `2024-01-02` |

Promotion vs production-only book on holdout:

- Overlay Calmar contribution ≥ 0 (book Calmar not worse than production-only − 0.05)
- Worst day not worse by > 0.5pp
- Overlay trade count ≥ 20 on selection (avoid rare-coin-flip gates)
- **Never** retune thresholds on holdout
- For IC: report **realized max loss / theoretical max loss** (should be ≤ 1.0 barring bad fills)

---

## Iron condor structure (bounded loss)

Default IC overlay (pre-register; small structure grid only):

| Param | Default | Grid (Phase B structure axis — max 3) |
|---|---|---|
| Short deltas | \|Δ\| ≈ 0.12 target | `{0.10, 0.12, 0.16}` (IC_d10 / IC_d12 / IC_d16) |
| Short Δ band | [0.08, 0.16] | widen only with d16: [0.12, 0.20] |
| Wing width | production wing helper / target-delta long | fixed-width alt: `{25, 50}` pts only if delta-wing fails liquidity |
| Sides | bull put + bear call **both required** | skip day if either leg missing |
| Size | **4 contracts** each short leg @ $13M (~⅛ morning vertical) | Phase D: 2 / 4 / 8 |
| Entry | first tranche ≥ 10:00 that passes gate | same as straddle |
| Theoretical max loss / lot | `width − net_credit` (points) | log per trade |

**Why IC is in this plan:** Phase A showed short-straddle edge is real but path-dependent (stop% ~19–30%). IC keeps the same “sell calm / mid-rich premium” thesis with a hard loss cap so a single blow-up day cannot dominate the sleeve.

Do **not** run straddle and IC on the same day in one book variant (capital/conflict). Variants are either `structure=STRADDLE` or `structure=IC_*`.

---

## Phase A findings (straddle — locked)

Source: `data/selective_straddle_overlay/phase_a/SUMMARY.md`  
Setup: counterfactual short ATM straddle @ first tranche ≥ 10:00; 1-lot points to EOD / 2× stop.

| Signal | Finding |
|---|---|
| `straddle_residual_z` | **Non-monotonic.** Peak at mid Q3 (+1.63); Q5 rich (−0.85) worse. Use **bands**, not uncapped rich. |
| `trend_score` | **Calm wins.** Q3 best; stable on holdout. |
| `term_ratio_z` | Unstable selection→holdout. Soft AND only. |
| `realized_vs_implied_z` | Avoid hot RV (Q5). Mild band better than deepest negative Q1. |

### Phase A → gate design rules (apply to **both** structures)

1. Prefer **residual bands** (floor + ceiling).
2. Prefer **|trend| ≤ 0.5** as primary co-filter.
3. Cap/skip when `realized_vs_implied_z` is hot (e.g. > 1.0).
4. Do **not** primary-gate on `term_ratio_z`.
5. Keep uncapped `residual ≥ 1.0` as a **negative control**.

### A1c — Iron condor diagnostics (run before or in parallel with B)

Same dates/features as A1. Counterfactual short IC (default IC_d12) to EOD; also mark debit stop at 2× credit **and** record theoretical max loss.

| ID | Output |
|---|---|
| A1c-1 | Mean/median IC PnL by quintile of each feature (selection) |
| A1c-2 | Win%, stop%, **avg loss / max-loss**, % of days hitting max loss |
| A1c-3 | Compare IC vs straddle quintile ranks — confirm same gate directions |

**Success for A1c:** IC shows same sign pattern as straddle on residual mid-band + calm trend (weaker magnitude OK). If IC flips directions vs straddle, freeze IC gates separately on selection only (still no holdout retune).

---

## Phase B — Entry gates × structure — NEXT

Substrate: production verticals always on.  
Overlay: **one** of STRADDLE or IC per variant.  
Default manage for B screen: hold to EOD (IC still bounded by wings even without a path stop).

### B0 — Controls (always run)

| ID | Structure | Rule |
|---|---|---|
| B0 | — | No overlay (production only) |
| B_always_S | STRADDLE | Every day at 10:00 |
| B_always_IC | IC_d12 | Every day at 10:00 |
| B_rich1_S | STRADDLE | residual ≥ 1.0 (negative control) |
| B_rich1_IC | IC_d12 | residual ≥ 1.0 (negative control) |

### B1 — Single-feature gates (shared; run on both structures)

| Feature | Gate family (pre-register) |
|---|---|
| `straddle_residual_z` | band ∈ {[−0.25, 1.0], [0.0, 1.0], [0.5, 1.5], [0.0, 1.5]}; floor-only controls ≥ {0.0, 0.5} |
| `trend_score` | \|trend\| ≤ {0.25, 0.5, 1.0} |
| `realized_vs_implied_z` | ≤ {0.5, 1.0} **or** ∈ [−1.0, 1.0] |
| `term_ratio_z` | **not primary** — soft AND in B2 only |

Structure axis for B1 winners only (avoid full cross): default **IC_d12** + **STRADDLE**; add IC_d10 / IC_d16 only for the top 2 gates after first pass.

### B2 — AND combinations (max 8 gates × {STRADDLE, IC_d12})

| ID | Gate |
|---|---|
| B2_1 | residual ∈ [−0.25, 1.0] **and** \|trend\| ≤ 0.5 |
| B2_2 | residual ∈ [0.5, 1.5] **and** \|trend\| ≤ 0.5 |
| B2_3 | residual ∈ [0.5, 1.5] **and** RV−IV ≤ 1.0 **and** \|trend\| ≤ 0.5 |
| B2_4 | residual ∈ [0.0, 1.0] **and** \|trend\| ≤ 0.5 **and** RV−IV ∈ [−1.0, 1.0] |
| B2_5 | Best of B2_1–B2_4 **and** not FOMC |
| B2_6 | Best of B2_1–B2_4 **and** VIX ∈ [15, 30] |
| B2_7 | Best of B2_1–B2_4 **and** \|term\| ≤ 1.0 |
| B2_8 | \|trend\| ≤ 0.5 only |

Rank on **selection** by: (1) combined-book Calmar, (2) overlay-sleeve Calmar, (3) overlay n ≥ 20, (4) for IC: worst overlay day and max-loss hit rate.  
Freeze **one straddle candidate** and **one IC candidate** (may share the same gate) before holdout. If only one structure clears floors, freeze that one.

---

## Phase C — Overlay management (on frozen gate + structure)

### C-S — Straddle exits

| ID | Exit |
|---|---|
| C0 | Hold to 16:00 |
| C1 | Stop if debit ≥ 2× entry credit |
| C2 | Stop at 1.5× |
| C3 | Take profit at 50% of credit |
| C4 | Flatten at 14:00 if not stopped |
| C5 | Best entry + C1 + C3 |

### C-IC — Iron condor exits (inside hard bound)

Wings already bound loss; path rules still matter for Calmar:

| ID | Exit |
|---|---|
| C0 | Hold to 16:00 (rely on wing bound) |
| C1 | Stop if IC mark debit ≥ 2× entry credit |
| C2 | Stop at 1.5× credit |
| C3 | Take profit at 50% of credit |
| C4 | Flatten at 14:00 if not stopped |
| C5 | Best entry + C1 + C3 |
| C6 | Stop if **either** short leg is ITM by ≥ 0.5× wing width (directional breach) |

Report for every IC variant: theoretical max loss, realized worst trade, % days stopped vs % days expired max-loss.

---

## Phase D — Sizing & interaction with vertical book

| ID | Size / interaction |
|---|---|
| D1 | 2 / 4 / 8 overlay contracts (straddle or IC shorts) |
| D2 | Size ∝ residual distance from band center (clip 2–8) |
| D3 | Skip overlay if vertical book already halted/flattened |
| D4 | Skip overlay if same-day vertical stop already fired |
| D5 | Overlay only when no vertical entry that tranche |
| D6 | IC only: skip if combined vertical+IC margin > budget (use `condor_margin_budget_pct` ≈ 3%) |

---

## Phase E — Sealed holdout + promotion

1. Rank on selection Calmar of **combined book** and **overlay sleeve alone**.  
2. Freeze promo candidate(s) before opening holdout — prefer **IC** when competitive.  
3. Promote only if holdout gates pass.  
4. If selection looks great and holdout fails → document as overfit.  
5. Live size-up only after holdout pass.  
6. Do **not** promote naked short straddle for live if IC holdout is within 0.05 Calmar of straddle — bounded loss wins ties.

---

## Optional Phase A follow-ups (only if B is flat)

| ID | Output | When |
|---|---|---|
| A2 | Joint heatmaps: residual × \|trend\|; residual × RV−IV | If B1 singles are weak |
| A3 | Conditional: FOMC vs not; VIX-5; TOD | If B2_5/B2_6 look promising |
| A4 | MFE/MAE path (straddle + IC) | Before Phase C if exit grid feels arbitrary |

---

## Implementation / run order

| Step | Work | Output |
|---|---|---|
| 1 (done) | W0 + Phase A1 straddle quintiles | `data/selective_straddle_overlay/phase_a/` |
| 2 **now** | A1c IC counterfactual diagnostics + selective overlay runner (STRADDLE + IC, gates B0–B2) | `.../phase_a_ic/`, runner script |
| 3 | Run B1 + B2 on selection for both structures; freeze winners | `.../phase_b/` |
| 4 | Phase C on frozen gate×structure | `.../phase_c/` |
| 5 | Phase D sizing + book interaction | `.../phase_d/` |
| 6 | Open holdout once; promotion memo (IC preferred on ties) | `summary_holdout.json`, `SUMMARY.md` |

Suggested commands (to implement in step 2):

```powershell
# IC diagnostics (mirror Phase A)
python scripts/run_phase_a_straddle_diagnostics.py --structure ic --resume

# Gate × structure suite
python scripts/run_selective_straddle_overlay.py --phase B --structures straddle,ic_d12 --shard 0 --shards 8 --resume
python scripts/merge_selective_straddle_shards.py --phase B --shards 8
python scripts/summarize_selective_straddle_overlay.py --phase B
```

Reuse:

- Straddle day PnL: `simulate_short_straddle_day` in `run_why_not_look_at_suite.py`
- IC legs: `select_condor_entries` / condor sleeve fields in `mbh_simulator.py` (override gates to this plan’s bands)
- Gate logic must support **residual bands**, not only `residual ≥ 1.0`

Outputs root: `data/selective_straddle_overlay/` with per-phase dirs; final `summary_selection.json`, `summary_holdout.json`, `report.json`, `SUMMARY.md` must break out **straddle vs IC**.

---

## Explicit non-goals

- Replacing verticals with straddles or condors  
- Full factorial of gates × deltas × wing widths  
- Using holdout to pick gates or structure  
- Primary gates on `term_ratio_z` after Phase A flip  
- Uncapped “sell when residual is highest”  
- Running straddle **and** IC overlays in the same book variant  
- Live size-up before holdout pass  
- Promoting naked short straddles when an IC variant is within promotion tolerance  
