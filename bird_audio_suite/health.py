"""Health check for the live service, run by a systemd timer: ``python -m bird_audio_suite.health``.

Live mode writes a heartbeat after every analyzed window. This check sends a desktop
notification when a problem persists for two consecutive runs (so a resume from
suspend, which briefly looks like a stale heartbeat, does not alert), and another one
when it is resolved. It deliberately imports nothing heavy (no BirdNET).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

SERVICE_NAME = "bird-audio-suite.service"
STALE_AFTER_SECONDS = 5 * 60
SILENCE_WINDOW_SECONDS = 10 * 60
# Digital silence: muted/unplugged inputs read ~0.00004, a quiet room 0.0003+.
SILENCE_RMS = 0.0001
CONSECUTIVE_CHECKS_TO_ALERT = 2
# the analysis finishes this far behind the microphone: BirdNET is slower than real time
SLOW_LAG_SECONDS = 60
MEMORY_LIMIT_MB = 1200


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "bird-audio-suite"


HEARTBEAT_FILE = runtime_dir() / "heartbeat.json"
STATE_FILE = runtime_dir() / "health_state.json"


class HeartbeatWriter:
    """Record, after each analyzed window, when it happened and how loud the input was."""

    def __init__(self, path: Path = HEARTBEAT_FILE, window_seconds: float = SILENCE_WINDOW_SECONDS) -> None:
        self.path = Path(path)
        self.window_seconds = window_seconds
        self.started_at = time.time()
        self.recent: deque[tuple[float, float]] = deque()

    def beat(self, rms: float, **extra) -> None:
        now = time.time()
        self.recent.append((now, float(rms)))
        while self.recent and self.recent[0][0] < now - self.window_seconds:
            self.recent.popleft()
        payload = {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": now,
            "last_rms": float(rms),
            "max_rms_recent": max(value for _, value in self.recent),
            "recent_seconds": now - self.recent[0][0],
            **extra,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass  # never let monitoring break detection


def find_problems(now: float, service_state: str, heartbeat: dict | None) -> dict[str, str]:
    if service_state != "active":
        return {"service": f"Il servizio non è attivo (stato: {service_state})."}
    if not heartbeat:
        return {"stale": "Il servizio è attivo ma non ha ancora analizzato audio."}
    age = now - float(heartbeat.get("updated_at", 0))
    if age > STALE_AFTER_SECONDS:
        last = datetime.fromtimestamp(float(heartbeat["updated_at"])).strftime("%H:%M")
        return {"stale": f"Nessun audio analizzato dalle {last} ({age / 60:.0f} min)."}
    problems = {}
    if (
        float(heartbeat.get("recent_seconds", 0)) >= SILENCE_WINDOW_SECONDS - 60
        and float(heartbeat.get("max_rms_recent", 1.0)) < SILENCE_RMS
    ):
        device = heartbeat.get("device", "?")
        problems["silent"] = f"Solo silenzio digitale da 10 minuti dal microfono ({device}): è muto o scollegato?"
    # written by metrics.LiveMetrics
    lag = float(heartbeat.get("lag_s") or 0)
    if lag > SLOW_LAG_SECONDS:
        problems["slow"] = (f"Il live è indietro di {lag:.0f} s sul microfono: BirdNET ci mette "
                            f"{float(heartbeat.get('analysis_avg_s') or 0):.1f} s per finestra "
                            f"({float(heartbeat.get('load_pct') or 0):.0f}% del tempo disponibile).")
    rss = float(heartbeat.get("rss_mb") or 0)
    if rss > MEMORY_LIMIT_MB:
        problems["memory"] = f"Il live usa {rss:.0f} MB di RAM (limite di guardia {MEMORY_LIMIT_MB} MB)."
    return problems


def service_state() -> str:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def notify(title: str, message: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "--app-name=Bird Audio Suite", f"--urgency={urgency}", "--icon=audio-input-microphone", title, message],
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"{title}: {message}")


def update_state(state: dict, problems: dict[str, str]) -> tuple[dict, list[tuple[str, str]], list[str]]:
    """Return the new state, the problems to announce and the announced problems now resolved."""
    counts = {key: int(state.get("counts", {}).get(key, 0)) + 1 for key in problems}
    notified = set(state.get("notified", []))
    to_alert = [
        (key, message)
        for key, message in problems.items()
        if counts[key] >= CONSECUTIVE_CHECKS_TO_ALERT and key not in notified
    ]
    resolved = sorted(key for key in notified if key not in problems)
    notified = (notified - set(resolved)) | {key for key, _ in to_alert}
    return {"counts": counts, "notified": sorted(notified)}, to_alert, resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bird Audio Suite health check.")
    parser.add_argument("--test-notification", action="store_true", help="Send a test notification and exit.")
    args = parser.parse_args(argv)
    if args.test_notification:
        notify("Bird Audio Suite", "Le notifiche funzionano.")
        return 0

    problems = find_problems(time.time(), service_state(), read_json(HEARTBEAT_FILE))
    state, to_alert, resolved = update_state(read_json(STATE_FILE) or {}, problems)
    for _key, message in to_alert:
        notify("Bird Audio Suite: problema", message, urgency="critical")
    if resolved and not problems:
        notify("Bird Audio Suite", "Il servizio ha ripreso ad ascoltare normalmente.")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
