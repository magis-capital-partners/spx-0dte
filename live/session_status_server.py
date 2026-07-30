"""Local session status API for the SPX 0DTE dashboard (loopback only).

Serves heartbeat + recent fills + supervisor stdout tail on 127.0.0.1 so the
dashboard can poll near-realtime when viewed from the trading PC.

  GET /status          -> JSON heartbeat + recent events (sanitized)
  GET /logs?tail=200   -> stdout log lines
  GET /healthz         -> ok

Also can write sanitized cloud status via --write-status.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"
SUPERVISOR_DIR = LIVE_DIR / "supervisor"
DOCS_STATUS = ROOT / "docs" / "data" / "live_status.json"
STATUS_ROLLOVER_EXIT_CODE = 75

sys.path.insert(0, str(ROOT / "live"))
from session_recovery import _pid_alive, load_fills_events  # noqa: E402


def _today() -> str:
    return date.today().isoformat()


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _tail_lines(path: Path, n: int) -> List[str]:
    if not path.is_file() or n <= 0:
        return []
    try:
        # Efficient-ish for multi-MB logs: read last ~512KB then take lines.
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 512_000))
            chunk = handle.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        if size > 512_000 and lines:
            lines = lines[1:]  # drop partial first line
        return lines[-n:]
    except Exception:
        return []


def _executor_console_path(today: str) -> Path:
    """Use today's executor output; the supervisor file is a legacy fallback."""
    session_log = LIVE_DIR / today / "executor-console.log"
    if session_log.is_file():
        return session_log
    legacy_log = SUPERVISOR_DIR / "executor-stdout.log"
    if legacy_log.is_file() and datetime.fromtimestamp(legacy_log.stat().st_mtime).date().isoformat() == today:
        return legacy_log
    return session_log


def _recent_events(today: str, limit: int = 12) -> List[dict]:
    events = load_fills_events(today)
    out: List[dict] = []
    interesting = {
        "session_start",
        "entry_submitted",
        "entry",
        "entry_ladder",
        "order_rejected",
        "entry_cancelled",
        "entry_fault",
        "halt_entries",
        "flatten",
        "error_flatten",
        "stop",
        "governor_recovered",
        "governor_clear",
        "account_guard_startup",
        "ib_disconnected",
        "ib_reconnect",
    }
    for ev in reversed(events):
        name = ev.get("event")
        if name not in interesting:
            continue
        row = {
            "ts": ev.get("ts"),
            "event": name,
        }
        for key in (
            "side",
            "short_strike",
            "long_strike",
            "contracts",
            "reason",
            "marked_pnl",
            "credit",
            "limit_credit",
            "error",
        ):
            if key in ev:
                row[key] = ev[key]
        out.append(row)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def build_status(*, today: Optional[str] = None) -> Dict[str, Any]:
    day = today or _today()
    day_dir = LIVE_DIR / day
    hb = _read_json(day_dir / "heartbeat.json") or {}
    lock = _read_json(day_dir / "executor.lock") or {}
    pid = int(hb.get("pid") or lock.get("pid") or 0)
    alive = _pid_alive(pid) if pid else False
    return {
        "schema": 1,
        "source": "local",
        "generated_at": datetime.now().isoformat(),
        "date": day,
        "pid": pid or None,
        "pid_alive": alive,
        "heartbeat_ts": hb.get("ts"),
        "entries_halted": bool(hb.get("entries_halted", False)),
        "flattened": bool(hb.get("flattened", False)),
        "open_count": int(hb.get("open_count") or 0),
        "marked_pnl": float(hb.get("marked_pnl") or 0.0),
        "recent_events": _recent_events(day),
        "stdout_path": str(_executor_console_path(day)),
    }


def build_sanitized_cloud_status(*, today: Optional[str] = None) -> Dict[str, Any]:
    """Public-safe subset — no stdout, no strike detail beyond counts."""
    full = build_status(today=today)
    last_ev = full["recent_events"][-1] if full["recent_events"] else None
    return {
        "schema": 1,
        "source": "cloud",
        "generated_at": full["generated_at"],
        "date": full["date"],
        "pid_alive": full["pid_alive"],
        "heartbeat_ts": full["heartbeat_ts"],
        "entries_halted": full["entries_halted"],
        "flattened": full["flattened"],
        "open_count": full["open_count"],
        "marked_pnl": round(full["marked_pnl"], 2),
        "last_event": (
            {"ts": last_ev.get("ts"), "event": last_ev.get("event")}
            if last_ev
            else None
        ),
        "note": "Sanitized status only. Full console is local to the trading host.",
    }


def write_cloud_status(
    *,
    today: Optional[str] = None,
    out_path: Path = DOCS_STATUS,
) -> Path:
    payload = build_sanitized_cloud_status(today=today)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Also keep a session copy for ops.
    day = payload["date"]
    session_copy = LIVE_DIR / day / "live_status.json"
    session_copy.parent.mkdir(parents=True, exist_ok=True)
    session_copy.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path


class _Handler(BaseHTTPRequestHandler):
    server_version = "SPXSessionStatus/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quiet default; supervisor log is enough.
        return

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Allow dashboard on github.io (HTTPS) and local http to call loopback.
        # Chrome Private Network Access requires Allow-Private-Network on preflight.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Preflight for Chromium Private Network Access from https://…github.io
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path in {"/", "/healthz"}:
                self._send(200, b'{"ok":true}\n')
                return
            if path == "/status":
                payload = build_status()
                self._send(200, (json.dumps(payload) + "\n").encode("utf-8"))
                return
            if path == "/logs":
                try:
                    tail = max(1, min(int((qs.get("tail") or ["200"])[0]), 2000))
                except ValueError:
                    tail = 200
                log_path = _executor_console_path(_today())
                lines = _tail_lines(log_path, tail)
                if not lines and not log_path.is_file():
                    lines = ["No console capture for this session yet. Restart the executor after the logging update to populate this panel."]
                payload = {
                    "path": str(log_path),
                    "tail": tail,
                    "lines": lines,
                    "generated_at": datetime.now().isoformat(),
                }
                self._send(200, (json.dumps(payload) + "\n").encode("utf-8"))
                return
            self._send(404, b'{"error":"not_found"}\n')
        except Exception as exc:
            self._send(500, (json.dumps({"error": repr(exc)}) + "\n").encode("utf-8"))


def _status_writer_loop(interval_sec: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            write_cloud_status()
        except Exception as exc:
            print(f"[session_status] write_cloud_status failed: {exc!r}", flush=True)
        stop.wait(interval_sec)


def _status_rollover_loop(
    started_day: str,
    stop: threading.Event,
    shutdown,
    *,
    poll_seconds: float = 30.0,
    today_fn=_today,
) -> None:
    """Request a clean service relaunch when the local calendar day changes."""
    while not stop.wait(poll_seconds):
        current_day = today_fn()
        if current_day == started_day:
            continue
        print(
            f"[{datetime.now().isoformat()}] session status date rollover "
            f"{started_day} -> {current_day}; restarting",
            flush=True,
        )
        shutdown()
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--write-status",
        action="store_true",
        help="Write sanitized docs/data/live_status.json once and exit",
    )
    parser.add_argument(
        "--write-interval",
        type=float,
        default=60.0,
        help="While serving, rewrite cloud status every N seconds (0=off)",
    )
    args = parser.parse_args()

    if args.write_status:
        path = write_cloud_status()
        print(f"wrote {path}")
        return 0

    stop = threading.Event()
    rollover_requested = threading.Event()
    writer: Optional[threading.Thread] = None
    if args.write_interval and args.write_interval > 0:
        write_cloud_status()
        writer = threading.Thread(
            target=_status_writer_loop,
            args=(args.write_interval, stop),
            daemon=True,
        )
        writer.start()

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    started_day = _today()

    def _request_rollover() -> None:
        rollover_requested.set()
        httpd.shutdown()

    rollover = threading.Thread(
        target=_status_rollover_loop,
        args=(started_day, stop, _request_rollover),
        daemon=True,
    )
    rollover.start()
    print(
        f"[{datetime.now().isoformat()}] session status on "
        f"http://{args.host}:{args.port}/status (logs=/logs)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return STATUS_ROLLOVER_EXIT_CODE if rollover_requested.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
