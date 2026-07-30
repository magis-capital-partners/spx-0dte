"""Run full overlay Calmar structure plan: GRID -> P3 -> P5 -> FINAL_REPORT.

  python scripts/run_overlay_calmar_full_plan.py
  python scripts/run_overlay_calmar_full_plan.py --shards 8
  python scripts/run_overlay_calmar_full_plan.py --max-oos-days 20  # smoke
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
OUT = ROOT / "data" / "overlay_calmar_structure"
WINNERS = OUT / "winners.json"


def run(cmd: list[str]) -> None:
    print("\n>>>", " ".join(cmd), flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"Command failed ({proc.returncode}): {' '.join(cmd)}")
    print(f"<<< done in {(time.time() - t0) / 60:.1f} min", flush=True)


def run_phase(phase: str, shards: int, checkpoint_every: int, max_oos: int, winners: str | None = None) -> None:
    if max_oos > 0 or shards <= 1:
        cmd = [
            PY,
            str(ROOT / "scripts" / "run_overlay_calmar_structure.py"),
            "--phase",
            phase,
            "--resume",
            "--checkpoint-every",
            str(checkpoint_every),
        ]
        if max_oos > 0:
            cmd += ["--max-oos-days", str(max_oos)]
        if winners:
            cmd += ["--winners-json", winners]
        run(cmd)
        merge = [
            PY,
            str(ROOT / "scripts" / "merge_overlay_calmar_shards.py"),
            "--phase",
            phase,
            "--shards",
            "1",
        ]
        if winners:
            merge += ["--winners-json", winners]
        run(merge)
        run([PY, str(ROOT / "scripts" / "summarize_overlay_calmar_structure.py"), "--phase", phase])
        return

    ps = ROOT / "scripts" / "run_overlay_calmar_parallel.ps1"
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps),
        "-Phase",
        phase,
        "-Shards",
        str(shards),
        "-CheckpointEvery",
        str(checkpoint_every),
    ]
    if winners:
        cmd += ["-WinnersJson", winners]
    run(cmd)


def write_final() -> None:
    parts = [
        "# Overlay Calmar Structure — Final Report",
        "",
        "Plan: `overlay_calmar_structure_test_plan.md`",
        "Substrate: production verticals (FOMC 13:30, no put-widen, IC sleeve off) + overlay variants.",
        "",
    ]
    for phase in ("grid", "p3", "p5"):
        sm = OUT / phase / "SUMMARY.md"
        if sm.is_file():
            parts += [f"## Phase {phase.upper()}", "", sm.read_text(encoding="utf-8"), ""]
    if WINNERS.is_file():
        w = json.loads(WINNERS.read_text(encoding="utf-8"))
        parts += [
            "## Frozen winners payload",
            "",
            f"- best_ic: `{w.get('best_ic')}`",
            f"- best_straddle: `{w.get('best_straddle')}`",
            f"- freeze_ic: {w.get('freeze_ic_variants')}",
            f"- freeze_straddle: {w.get('freeze_straddle_variants')}",
            f"- p1_top_widths: {w.get('p1_top_widths')}",
            f"- p2_top_stops: {w.get('p2_top_stops')}",
            "",
        ]
    # Decision from latest report
    for phase in ("p5", "p3", "grid"):
        rep = OUT / phase / "report.json"
        if rep.is_file():
            r = json.loads(rep.read_text(encoding="utf-8"))
            parts += ["## Promotion decision", "", f"**{r.get('decision')}**", ""]
            for p in r.get("promotion") or []:
                parts.append(
                    f"- `{p['variant']}`: {'PROMOTE' if p['promote'] else 'NO'} — {'; '.join(p['reasons'])}"
                )
            parts.append("")
            break
    final = OUT / "FINAL_REPORT.md"
    final.write_text("\n".join(parts), encoding="utf-8")
    print(f"Final report: {final}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--skip-grid", action="store_true")
    parser.add_argument("--skip-p3", action="store_true")
    parser.add_argument("--skip-p5", action="store_true")
    args = parser.parse_args()

    t_all = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    if not args.skip_grid:
        print("=== GRID: P0 + P1 + P1b + P2 + P2b + P2c ===", flush=True)
        run_phase("GRID", args.shards, args.checkpoint_every, args.max_oos_days)

    if not args.skip_p3:
        if not WINNERS.is_file():
            raise SystemExit(f"Missing {WINNERS}; GRID must complete first")
        print("=== P3: size + book interaction on frozen winners ===", flush=True)
        run_phase("P3", args.shards, args.checkpoint_every, args.max_oos_days, winners=str(WINNERS))

    if not args.skip_p5:
        if not WINNERS.is_file():
            raise SystemExit(f"Missing {WINNERS}")
        print("=== P5: calm-gate salvage on best IC + straddle ===", flush=True)
        run_phase("P5", args.shards, args.checkpoint_every, args.max_oos_days, winners=str(WINNERS))

    write_final()
    print(f"\nALL DONE in {(time.time() - t_all) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
