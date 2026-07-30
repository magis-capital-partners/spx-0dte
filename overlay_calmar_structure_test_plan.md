# Overlay Calmar Structure Grid — IC Widths × Straddle Stops

Date: 2026-07-17  
Related: `selective_straddle_overlay_test_plan.md` (v3 — feature gates; mostly done / promo = always-IC)  
Does **not** replace v3; this plan answers a different question.

## Goal

Improve **combined-book Calmar** of current production verticals by tuning the short-premium overlay’s **risk shape**:

\[
\text{Calmar} = \frac{\text{CAGR}}{\text{MaxDD}}
\]

Secondary risk lenses (report always; use as promotion gates):

- Worst single-day return (account %)
- Max drawdown (account %)
- Overlay sleeve Calmar / worst overlay day
- For IC: realized loss / theoretical max loss

**Not the goal:** re-fish entry feature gates (residual / trend / RV). Production already runs permissive “always when structure allows” IC. This grid asks whether **wider/narrower wings** or a **stopped short straddle** beats that on Calmar.

---

## Locked substrate (do not retune)

| Knob | Value |
|---|---|
| Profile | `p3_poststop_cooldown_120` |
| Account / baseline | $13M / 31-lot vertical |
| Wings (vertical) | put 150 / call 75, skew 0.65, flatten −3.25% |
| Cooldown | 120 min same-side post-stop |
| FOMC | no new **vertical** entries after 13:30 |
| VIX put-widen | **off** |
| VIX sizing | skip session VIX>35; 1.25× in 25–35 |
| TOD sizing | production linear decay |

Overlay sits **on top** of this book. Variants differ only in overlay structure / stop / size. Never straddle **and** IC in the same variant.

---

## Anti-overfit (mandatory)

| Split | Role | Dates |
|---|---|---|
| Selection | Rank & freeze winners | OOS ≤ `2023-12-29` |
| Holdout (sealed) | Promotion only | OOS ≥ `2024-01-02` |

Rules:

1. Rank **only** on selection.
2. Freeze ≤ **2** promo candidates (best IC-family + best straddle-family) before opening holdout.
3. Never retune widths/stops on holdout.
4. Prefer IC when holdout Calmar within **0.05** of straddle (bounded loss wins ties).

### Primary ranking key (selection)

1. Combined-book Calmar  
2. Combined CAGR (tie-break; prefer higher)  
3. Combined worst-day % (prefer less negative)  
4. Overlay trade count ≥ 20 on selection  
5. For IC: max-loss hit rate (diagnostic; not primary)

### Promotion gates (holdout vs production-only)

Promote only if **all** hold:

| Gate | Threshold |
|---|---|
| Combined Calmar | ≥ prod-only Calmar − 0.05 |
| Combined CAGR | ≥ prod-only CAGR − 0.25pp |
| Worst day | not worse than prod-only by > **0.50pp** |
| MaxDD | not worse than prod-only by > **0.50pp** |
| Overlay n (selection) | ≥ 20 |

If selection looks great and holdout fails → document as overfit; do not ship.

---

## Phase 0 — Controls & plumbing

| ID | Overlay | Notes |
|---|---|---|
| `P0_prod` | none | Verticals only — Calmar baseline |
| `P0_ic50` | IC Δ0.12, **50pt** wings, 8/31 size, VIX≥15, 1×/day ≥10:00, EOD | **Current production overlay** |
| `P0_s_eod` | Straddle ATM, 8 ct, EOD (no stop) | Upper-bound premium / risk stress |
| `P0_s_2x` | Straddle ATM, 8 ct, stop @ 2× credit | Sanity vs old Phase C |

Reuse: `scripts/run_selective_straddle_overlay.py`, `selective_overlay_variants.py`, `select_condor_entries` / straddle path in runner.  
Extend `Structure` with explicit `wing_width` variants; extend straddle stop grid.

**Size policy (locked for Phase 1–2):** overlay contracts = `round(vertical_base × 8/31)` so IC and straddle are capital-comparable to production IC8. Fixed-8 at flat midday is OK if runner already uses fraction; document which.

**IC low-vol rule (locked):** skip when VIX open < 15 or credit < 5× round-trip fees (same as production / v3).

---

## Phase 1 — IC wing-width grid (primary Calmar lever for bounded sleeve)

Fix: Δ short ≈ **0.12**, band ±0.04, entry ≥10:00, 1×/day, hold to EOD (wings = hard bound). No path stop in P1 (isolate width).

| ID | Wing width (pts) | Thesis |
|---|---:|---|
| `IC_w25` | 25 | Tight wings → more credit, higher max-loss hit rate, fee-sensitive |
| `IC_w35` | 35 | Between tight and prod |
| `IC_w50` | 50 | Production control (= `P0_ic50`) |
| `IC_w75` | 75 | Wider → smaller credit, lower theoretical max loss / lot? Wait: max loss = width − credit; wider usually **higher** max $ loss, lower probability of hit |
| `IC_w100` | 100 | Far wings — cheap pin risk, larger notional max loss |
| `IC_w150` | 150 | Stress / near “naked short Δ” feel with distant hedges |

Report per variant:

- Combined Calmar / CAGR / MaxDD / worst day  
- Overlay PnL, win%, % days hitting wing max loss  
- Mean credit, mean theoretical max loss $, realized / theo max  
- Selection vs holdout (holdout only after freeze)

**Success:** any width with selection Calmar > `P0_prod` and ≥ `P0_ic50`, without blowing worst-day gate. If `IC_w50` wins → width is not the lever; go Phase 2 / 3.

### Phase 1b — Width × delta (only top 2 widths from P1)

| Short \|Δ\| | On winners of P1 only |
|---|---|
| 0.10 | farther shorts, less credit |
| 0.12 | default |
| 0.16 | closer shorts, more credit / breach risk |

Cap: **2 widths × 3 deltas = 6** (plus controls already run). No full factorial.

---

## Phase 2 — Short straddle + stop grid (unbounded sleeve, path risk)

Fix: ATM straddle (or nearest Δ~0.50/0.50), entry ≥10:00, 1×/day, size = same 8/31 policy.  
Stop = mark debit ≥ `k ×` entry credit (confirm with 1-bar mark unless noted). No take-profit in P2 (isolate stop).

| ID | Stop multiple \(k\) | Notes |
|---|---:|---|
| `S_eod` | — | Hold to 16:00 |
| `S_125` | 1.25 | Aggressive clip |
| `S_150` | 1.50 | Tight |
| `S_175` | 1.75 | |
| `S_200` | 2.00 | Classic 2× |
| `S_250` | 2.50 | |
| `S_300` | 3.00 | Loose (more premium, fatter tails) |

### Phase 2b — Stop confirmation (top 2 stops only)

| ID | Rule |
|---|---|
| `confirm_1` | Stop on first mark breach |
| `confirm_2` | Require 2 consecutive bars beyond stop (mirror vertical stop style) |

### Phase 2c — Optional soft exits (only if P2 Calmar still < best IC)

| ID | Rule |
|---|---|
| `flat_1400` | Flatten 14:00 if not stopped |
| `tp_50` | Take profit at 50% of credit |
| `stop_best + tp_50` | Combine |

**Success:** straddle family beats best IC on selection Calmar **and** passes worst-day gate — otherwise IC remains preferred for live even if straddle CAGR is higher.

---

## Phase 3 — Size & book interaction (frozen structure only)

Run only on frozen P1/P2 winners (max 2 structures).

| ID | Change |
|---|---|
| `n4` / `n8` / `n12` | Overlay contracts 4 / 8 / 12 @ flat $13M (or fraction 4/31, 8/31, 12/31) |
| `skip_vhalt` | Skip overlay if vertical book already flattened/halted |
| `skip_vstop` | Skip if same-day vertical stop already fired |
| `scale_vix` | Overlay size follows vertical VIX/TOD multiplier (prod IC already does) vs fixed 8 |

Rank again on selection Calmar; re-freeze size before holdout.

---

## Phase 4 — Sealed holdout + decision

1. Freeze from selection: **best IC** + **best straddle** (or one if other fails floors).  
2. Score holdout once.  
3. Decision matrix:

| Outcome | Action |
|---|---|
| Best IC clears promotion | Ship IC (width/delta/size from freeze); keep FOMC verts |
| Best straddle clears **and** Calmar ≥ best IC + 0.05 | Consider straddle live only with documented stop + max daily overlay loss kill-switch |
| Straddle wins Calmar but fails worst-day | Do **not** ship; keep IC or prod-only |
| Neither beats `P0_prod` on Calmar within tolerance | Keep current `IC_w50` or drop overlay |

Kill-switch for any live straddle promo (non-negotiable): halt new overlay entries for day if overlay MTM ≤ −X% account (pre-register X = 0.25% or 0.50% on selection MAE).

---

## Explicit non-goals

- Retuning vertical wings / flatten / FOMC in this suite  
- Full factorial: width × delta × stop × gate × size  
- Feature-gate re-search (use v3 winners only if Phase 1–2 are flat — optional Phase 5 below)  
- Straddle + IC same book  
- Promoting on selection alone  
- Optimizing overlay sleeve Calmar at the expense of combined-book Calmar  

---

## Optional Phase 5 — Entry filter salvage (only if P1–P2 flat)

If no width/stop beats `P0_ic50` Calmar by ≥ 0.05 on selection, attach the v3 calm gate to the best structure only:

- `|trend| ≤ 0.5` and residual ∈ `[0.5, 1.5]` (v3 B2_2)  
- Compare vs always-on on selection; freeze before holdout  

Do not expand gate search beyond that single salvage.

---

## Metrics & outputs

Root: `data/overlay_calmar_structure/`

| Artifact | Content |
|---|---|
| `phase0/…` `phase1/…` `phase2/…` | Per-variant daily PnL + summary |
| `summary_selection.json` | Ranked table: Calmar, CAGR, MaxDD, worst, overlay n |
| `summary_holdout.json` | Frozen candidates only |
| `pareto_selection.png` or CSV | CAGR vs MaxDD / vs worst-day scatter |
| `SUMMARY.md` | Winner, losers, promote / no-promote |

Every summary row must include:

`variant, structure, wing_or_stop, contracts, cagr, maxdd, calmar, worst_day, sharpe, overlay_pnl, overlay_n, max_loss_hit_pct (IC)`

---

## Implementation sketch

| Step | Work |
|---|---|
| 1 | Add wing-width Structures + straddle stop Variants to `selective_overlay_variants.py` (or new `overlay_calmar_variants.py`) |
| 2 | Ensure runner marks IC theo max loss and straddle stop exits (already partially in `run_selective_straddle_overlay.py`) |
| 3 | Parallel PS runner (mirror overnight / selective overlay shards) |
| 4 | Summarizer ranked by Calmar + Pareto CSV |
| 5 | One holdout pass on frozen set |

Estimated variant count (manageable):

- P0: 4  
- P1: 5 new widths (+ control) → ~6  
- P1b: ≤6  
- P2: 7  
- P2b: ≤4  
- P3: ≤8 on 1–2 winners  

≈ **30–35** full-history overlays — far smaller than v3 gate grid.

---

## Run order (checklist)

1. [ ] P0 controls (prod, IC50, S_eod, S_2x)  
2. [ ] P1 IC widths → freeze top 2 widths  
3. [ ] P1b delta cross on those widths → freeze best IC  
4. [ ] P2 straddle stops → freeze top 2 stops  
5. [ ] P2b confirm (optional P2c) → freeze best straddle  
6. [ ] P3 size/interaction on frozen IC + straddle  
7. [ ] Open holdout once; write promotion memo  
8. [ ] If ship: update `profiles.py` / live / dashboard; else keep IC50  

---

## Decision bias (pre-registered)

Calmar improvement that comes from **higher CAGR with equal/worse MaxDD** is acceptable only if worst-day gate still passes.  
Calmar improvement from **lower MaxDD with small CAGR give-up** is preferred for live.  
Bounded IC preferred over naked straddle at equal Calmar.
