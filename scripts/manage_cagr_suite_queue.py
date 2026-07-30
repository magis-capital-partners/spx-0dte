"""Manage 32 suite×shard CAGR jobs with a 3-worker pool, checkpoint resume, and status."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_cagr_improvement_batches import (  # noqa: E402
    BATCH_SUITES,
    OUT,
    SEQUENTIAL_SUITES,
    TRAIN,
    checkpoint_path,
    load_checkpoint,
    resolve_eligible,
    shard_bounds,
    work_dir,
)

LOG_DIR = OUT / "logs"
STATE_PATH = OUT / "queue_state.json"
MANIFEST_PATH = OUT / "job_manifest.json"


@dataclass(frozen=True)
class Job:
    batch: int
    suite: str
    shard: int
    shards: int = 4

    @property
    def job_id(self) -> str:
        return f"b{self.batch}_{self.suite}_s{self.shard}"

    def log_path(self) -> Path:
        return LOG_DIR / f"{self.job_id}.log"

    def ckpt_path(self) -> Path:
        return checkpoint_path(work_dir(self.batch, self.suite, self.shard, self.shards))


def build_manifest(shards: int) -> List[Job]:
    jobs: List[Job] = []
    for batch, suites in BATCH_SUITES.items():
        for suite in suites:
            for shard in range(shards):
                jobs.append(Job(batch=batch, suite=suite, shard=shard, shards=shards))
    return jobs


def expected_shard_days(job: Job) -> int:
    eligible, _ = resolve_eligible()
    oos_total = len(eligible) - TRAIN
    start, end = shard_bounds(oos_total, job.shard, job.shards)
    return end - start


def is_complete(job: Job) -> bool:
    ckpt = load_checkpoint(job.ckpt_path())
    if not ckpt or not ckpt.get("complete"):
        return False
    return int(ckpt.get("oos_total", 0)) == expected_shard_days(job)


def can_start(job: Job) -> bool:
    if is_complete(job):
        return False
    if job.suite in SEQUENTIAL_SUITES and job.shard > 0:
        prev = Job(job.batch, job.suite, job.shard - 1, job.shards)
        if not is_complete(prev):
            return False
    return True


def trailing_from_prev_shard(job: Job) -> str:
    if job.suite not in SEQUENTIAL_SUITES or job.shard == 0:
        return ""
    prev = Job(job.batch, job.suite, job.shard - 1, job.shards)
    ckpt = load_checkpoint(prev.ckpt_path())
    if not ckpt:
        return ""
    return ",".join(str(x) for x in ckpt.get("trailing_stop", []))


def start_job(job: Job, resume: bool, checkpoint_every: int) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-u",
        str(SCRIPTS / "run_cagr_improvement_batches.py"),
        "--batch",
        str(job.batch),
        "--suite",
        job.suite,
        "--shard",
        str(job.shard),
        "--shards",
        str(job.shards),
        "--checkpoint-every",
        str(checkpoint_every),
    ]
    if resume:
        cmd.append("--resume")
    carry = trailing_from_prev_shard(job)
    if carry:
        cmd.extend(["--carry-trailing", carry])

    log_f = job.log_path().open("w", encoding="utf-8")
    log_f.write(f"# started {datetime.now(timezone.utc).isoformat()}\n# cmd: {' '.join(cmd)}\n\n")
    log_f.flush()
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000) if sys.platform == "win32" else 0,
    )
    proc._log_file = log_f  # type: ignore[attr-defined]
    return proc


def write_state(running: Dict[str, subprocess.Popen], pending: List[Job], done: int, total: int, jobs: List[Job]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    job_by_id = {j.job_id: j for j in jobs}
    running_jobs = []
    for jid, proc in running.items():
        job = job_by_id.get(jid)
        progress = ""
        if job:
            c = load_checkpoint(job.ckpt_path())
            if c:
                progress = f"{c.get('oos_done', 0)}/{c.get('oos_total', '?')}"
        running_jobs.append({"job_id": jid, "pid": proc.pid, "progress": progress})

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "done": done,
        "total": total,
        "running": running_jobs,
        "pending": len(pending),
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_status(jobs: List[Job]) -> None:
    done = sum(1 for j in jobs if is_complete(j))
    print(f"Progress: {done}/{len(jobs)} jobs complete")
    for j in jobs:
        ckpt = load_checkpoint(j.ckpt_path())
        if ckpt and ckpt.get("complete"):
            status = "DONE"
        elif ckpt:
            status = f"{ckpt.get('oos_done', 0)}/{ckpt.get('oos_total', '?')}"
        else:
            status = "pending"
        print(f"  {j.job_id:40s} {status}")


def run_queue(
    *,
    parallel: int,
    shards: int,
    resume: bool,
    checkpoint_every: int,
    merge: bool,
) -> None:
    jobs = build_manifest(shards)
    MANIFEST_PATH.write_text(
        json.dumps([{"batch": j.batch, "suite": j.suite, "shard": j.shard} for j in jobs], indent=2),
        encoding="utf-8",
    )

    pending = [j for j in jobs if not is_complete(j)]
    done_count = len(jobs) - len(pending)
    running: Dict[str, subprocess.Popen] = {}
    job_by_id = {j.job_id: j for j in jobs}

    print(f"Queue: {len(jobs)} jobs, {done_count} already complete, {len(pending)} remaining, {parallel} workers")

    while pending or running:
        # reap finished
        finished = []
        for jid, proc in running.items():
            rc = proc.poll()
            if rc is not None:
                log_f = getattr(proc, "_log_file", None)
                if log_f:
                    log_f.close()
                if rc != 0:
                    raise SystemExit(f"Job {jid} failed (exit {rc}). See {job_by_id[jid].log_path()}")
                finished.append(jid)
                done_count += 1
                print(f"  finished {jid} ({done_count}/{len(jobs)})", flush=True)
        for jid in finished:
            del running[jid]

        # start new jobs
        started = 0
        for job in list(pending):
            if len(running) >= parallel:
                break
            if not can_start(job):
                continue
            proc = start_job(job, resume=resume, checkpoint_every=checkpoint_every)
            running[job.job_id] = proc
            pending.remove(job)
            started += 1
            print(f"  started {job.job_id} pid={proc.pid} -> {job.log_path()}", flush=True)

        write_state(running, pending, done_count, len(jobs), jobs)

        if running:
            time.sleep(30)
        elif pending:
            # deadlock: sequential suite waiting — should resolve as shards complete
            time.sleep(5)

    print(f"\nAll {len(jobs)} jobs complete.")

    if merge:
        merge_script = SCRIPTS / "merge_cagr_batch_shards.py"
        for batch in sorted(BATCH_SUITES):
            print(f"Merging batch {batch}...", flush=True)
            subprocess.run(
                [sys.executable, str(merge_script), "--batch", str(batch), "--shards", str(shards), "--suite-mode"],
                cwd=ROOT,
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 4-shard × 3-suite CAGR job queue.")
    parser.add_argument("--parallel", type=int, default=3, help="Max concurrent workers")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="Pass --resume to each job")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--status", action="store_true", help="Print job status and exit")
    args = parser.parse_args()

    jobs = build_manifest(args.shards)
    if args.status:
        print_status(jobs)
        if STATE_PATH.is_file():
            print(f"\nLast queue state: {STATE_PATH}")
        return

    run_queue(
        parallel=args.parallel,
        shards=args.shards,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        merge=not args.no_merge,
    )


if __name__ == "__main__":
    main()
