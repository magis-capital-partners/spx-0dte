"""Run the full selective overlay plan: A1c -> B -> CD -> holdout promote.

  python scripts/run_selective_overlay_full_plan.py
  python scripts/run_selective_overlay_full_plan.py --shards 8
  python scripts/run_selective_overlay_full_plan.py --max-oos-days 40  # smoke
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(cmd: list[str]) -> None:
    print("\n>>>", " ".join(cmd), flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"Command failed ({proc.returncode}): {' '.join(cmd)}")
    print(f"<<< done in {(time.time() - t0) / 60:.1f} min", flush=True)


def run_phase_parallel(phase: str, shards: int, checkpoint_every: int, max_oos: int, winners: str | None = None, promote: bool = False) -> None:
    # Launch shards sequentially via subprocess pool using PowerShell script when max_oos==0,
    # else single-process for smoke tests.
    if max_oos > 0 or shards <= 1:
        cmd = [
            PY,
            str(ROOT / "scripts" / "run_selective_straddle_overlay.py"),
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
            str(ROOT / "scripts" / "merge_selective_straddle_shards.py"),
            "--phase",
            phase,
            "--shards",
            "1",
        ]
        if winners:
            merge += ["--winners-json", winners]
        # single-process wrote to phase/ not phase/shard_0 — merge expects shard dirs when shards>1
        # For shards=1, work_dir is phase root itself.
        run(merge)
    else:
        # parallel via Start-Process
        ps = ROOT / "scripts" / "run_selective_straddle_overlay_parallel.ps1"
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
        if promote:
            cmd += ["-Promote"]
        run(cmd)
        return  # ps1 already merges + summarizes

    sum_cmd = [PY, str(ROOT / "scripts" / "summarize_selective_straddle_overlay.py"), "--phase", phase]
    if promote:
        sum_cmd.append("--promote")
    run(sum_cmd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--skip-a1c", action="store_true")
    parser.add_argument("--skip-b", action="store_true")
    parser.add_argument("--skip-cd", action="store_true")
    args = parser.parse_args()

    t_all = time.time()

    if not args.skip_a1c:
        print("=== Phase A1c: IC diagnostics ===", flush=True)
        run_phase_parallel("A1c", args.shards, args.checkpoint_every, args.max_oos_days, promote=False)

    winners_path = ROOT / "data" / "selective_straddle_overlay" / "b" / "winners.json"

    if not args.skip_b:
        print("=== Phase B: entry gates × structure ===", flush=True)
        run_phase_parallel("B", args.shards, args.checkpoint_every, args.max_oos_days, promote=False)

    if not args.skip_cd:
        if not winners_path.is_file():
            raise SystemExit(f"Missing winners at {winners_path}; Phase B must complete first")
        print("=== Phase CD: exits + sizing on frozen winners ===", flush=True)
        run_phase_parallel(
            "CD",
            args.shards,
            args.checkpoint_every,
            args.max_oos_days,
            winners=str(winners_path),
            promote=True,
        )

    # Final promotion memo at suite root
    b_summary = ROOT / "data" / "selective_straddle_overlay" / "b" / "SUMMARY.md"
    cd_summary = ROOT / "data" / "selective_straddle_overlay" / "cd" / "SUMMARY.md"
    cd_report = ROOT / "data" / "selective_straddle_overlay" / "cd" / "report.json"
    final = ROOT / "data" / "selective_straddle_overlay" / "FINAL_REPORT.md"
    parts = ["# Selective Straddle + Iron Condor Overlay — Final Report", ""]
    if b_summary.is_file():
        parts += ["## Phase B", "", b_summary.read_text(encoding="utf-8"), ""]
    if cd_summary.is_file():
        parts += ["## Phase C/D + Holdout", "", cd_summary.read_text(encoding="utf-8"), ""]
    if cd_report.is_file():
        parts += ["## Raw promotion JSON", "", "See `cd/report.json`.", ""]
    a1c = ROOT / "data" / "selective_straddle_overlay" / "a1c" / "SUMMARY.md"
    if a1c.is_file():
        parts = parts[:2] + ["## Phase A1c", "", a1c.read_text(encoding="utf-8"), ""] + parts[2:]
    final.write_text("\n".join(parts), encoding="utf-8")
    print(f"\nALL DONE in {(time.time() - t_all) / 60:.1f} min", flush=True)
    print(f"Final report: {final}", flush=True)


if __name__ == "__main__":
    main()
