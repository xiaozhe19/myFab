"""Terminal progress for the collector and a separate read-only status monitor."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import sys
import time


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def bar(fraction, width=20):
    fraction = min(1.0, max(0.0, fraction))
    full = int(width * fraction)
    return "[" + "#" * full + "-" * (width - full) + "]"


def job_progress(directory, job):
    """Progress is advisory; a missing/rotating file must never abort DES."""
    if job["status"] == "complete":
        return {"fraction": 1.0, "phase": "complete", "snapshots": job["result"]["snapshots"]}
    path = (Path(directory) / job["database"]).with_suffix(".progress.json")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if info.get("job_id") != job["id"]:
            return {}
        fraction = float(info.get("fraction", 0))
        if not math.isfinite(fraction):
            return {}
        info["fraction"] = min(1.0, max(0.0, fraction))
        return info
    except (OSError, ValueError, TypeError):
        return {}


def render_progress(manifest, jobs=None):
    jobs = manifest["jobs"] if jobs is None else jobs
    counts = Counter(job["status"] for job in jobs)
    directory = manifest["plan"]["output_dir"]
    active = [(job, job_progress(directory, job)) for job in jobs if job["status"] == "running"]
    fraction = (counts["complete"] + sum(info.get("fraction", 0) for _, info in active)) / max(1, len(jobs))
    lines = [f"TOTAL {bar(fraction)} {fraction:6.1%} | OK {counts['complete']}/{len(jobs)} "
             f"| RUN {counts['running']} | WAIT {counts['pending']} "
             f"| FAIL {counts['failed']} | CANCEL {counts['cancelled']}"]
    controller = manifest.get("controller", {})
    if controller:
        clock = datetime.now(timezone.utc)
        started = datetime.fromisoformat(controller["started_at"])
        heartbeat = datetime.fromisoformat(controller["heartbeat_at"])
        lines.append(f"Controller {controller['status']} | PID {controller['pid']} "
                     f"| elapsed {duration((clock-started).total_seconds())} "
                     f"| heartbeat {max(0, int((clock-heartbeat).total_seconds()))}s ago")
    total_days = (manifest["plan"]["expected_simulation"]["end_time"]
                  - manifest["plan"]["expected_simulation"]["start_time"]) / 1440
    expected_snapshots = manifest["contract"]["snapshots"]
    for job, info in active:
        fraction = info.get("fraction", 0)
        phase = info.get("phase", "starting")
        lines.append(f"{job['id']:28} {bar(fraction)} {fraction:6.1%} "
                     f"| day {info.get('elapsed_days', 0):6.1f}/{total_days:g} "
                     f"| tokens {info.get('snapshots', 0):3}/{expected_snapshots} | {phase}")
    if counts["failed"]:
        latest = next(job for job in reversed(jobs) if job["status"] == "failed")
        lines.append(f"Last failure: {latest['id']}: {latest.get('error', 'see job log')}")
    return lines


class ProgressDisplay:
    def __init__(self, mode="auto", stream=None):
        self.stream = stream or sys.stdout
        self.mode = ("live" if self.stream.isatty() else "plain") if mode == "auto" else mode
        self.interval = 1.0 if self.mode == "live" else 30.0
        self._last_draw = -math.inf
        self._lines = 0

    def _clear(self):
        if self.mode == "live" and self._lines:
            self.stream.write(f"\x1b[{self._lines}A\r\x1b[J")
            self._lines = 0

    def event(self, message):
        self._clear()
        print(message, file=self.stream, flush=True)

    def update(self, manifest, jobs=None, *, force=False):
        now = time.monotonic()
        if self.mode == "off" or (not force and now-self._last_draw < self.interval):
            return
        self._clear()
        lines = render_progress(manifest, jobs)
        if self.mode == "live":
            # Avoid wrapped lines breaking cursor positioning in narrower terminals.
            width = max(20, shutil.get_terminal_size(fallback=(120, 24)).columns - 1)
            lines = [line[:width] for line in lines]
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
        self._lines = len(lines) if self.mode == "live" else 0
        self._last_draw = now


def watch_progress(path, *, mode="auto", interval=1.0):
    display = ProgressDisplay(mode)
    path = Path(path)
    while True:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not path.exists():
                raise FileNotFoundError(path)
            time.sleep(interval)
            continue
        display.update(manifest, force=display.mode == "live")
        active = any(job["status"] == "running" for job in manifest["jobs"])
        if not active and manifest.get("controller", {}).get("status") != "running":
            display.update(manifest, force=True)
            return
        time.sleep(interval)
