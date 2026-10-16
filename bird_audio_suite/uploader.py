"""The uploader: its own service, apart from the live listener (`bird_audio_cli.py sync --watch`).

The live only writes each clip (WAV + JSON) into detections/. Everything heavy that follows,
MP3, spectrogram (seconds and 100+ MB for a long clip) and the upload, happens here, in another
process with low CPU and disk priority (systemd unit bird-audio-suite-sync): nothing of it can
slow the listening down, not even through Python's GIL.

It looks at the folder every few seconds, so a clip is on Supabase moments after it closes;
what failed (offline) is tried again every minute. Once an hour it also removes from Storage the
files of observations on iNaturalist for more than a week (Outbox.purge_uploaded_media). After each round it writes its state into
the runtime folder: the live table shows it, the health check warns when clips stay stuck.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from .health import UPLOADER_STATUS_FILE as STATUS_FILE
POLL_SECONDS = 3.0
RETRY_SECONDS = 60.0
NAMES_REFRESH_SECONDS = 3600.0
PURGE_EVERY_SECONDS = 3600.0
STALE_STATUS_SECONDS = 3 * 60  # the uploader writes at least every RETRY_SECONDS


def folder_signature(folder: Path) -> tuple[tuple[str, int], ...]:
    """What is waiting, cheaply: the clip data files and when they were written."""
    try:
        return tuple(sorted((entry.name, entry.stat().st_mtime_ns) for entry in Path(folder).glob("*.json")))
    except OSError:
        return ()


class UploaderStatus:
    def __init__(self, path: Path = STATUS_FILE) -> None:
        self.path = Path(path)
        self.durations: deque[float] = deque(maxlen=50)  # seconds per clip, last ones
        self.sent_total = 0
        self.last_sent_at: float | None = None

    def write(self, pending: list, report=None, seconds: float = 0.0, error: str = "") -> None:
        if report is not None and report.sent:
            self.sent_total += report.sent
            self.last_sent_at = time.time()
            self.durations.extend([seconds / report.sent] * report.sent)
        oldest = min((item.data.get("recorded_at", "") for item in pending), default="")
        payload = {
            "updated_at": time.time(),
            "pending": len(pending),
            "oldest_pending": oldest or None,  # ISO time of the oldest clip still waiting
            "sent_total": self.sent_total,
            "last_sent_at": self.last_sent_at,
            "seconds_per_clip": round(sum(self.durations) / len(self.durations), 2) if self.durations else None,
            "last_error": error or (report.errors[0] if report is not None and report.errors else None),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass


def read_status(path: Path = STATUS_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_line(status: dict | None, now: float | None = None) -> str:
    """One line for under the live table."""
    now = time.time() if now is None else now
    if not status or now - float(status.get("updated_at", 0)) > STALE_STATUS_SECONDS:
        return "Invio a Supabase: il servizio di invio non risponde (make install-sync, make status)"
    parts = [f"Invio a Supabase: {status['pending']} in attesa"]
    if status.get("last_sent_at"):
        parts.append(f"ultimo invio {datetime.fromtimestamp(status['last_sent_at']):%H:%M}")
    if status.get("seconds_per_clip") is not None:
        parts.append(f"{status['seconds_per_clip']:.1f} s/clip")
    if status["pending"] and status.get("last_error"):
        parts.append(f"errore: {str(status['last_error'])[:60]}")
    return " · ".join(parts)


def run_uploader(
    detections_dir: Path,
    make_outbox: Callable[[], object],
    *,
    poll_seconds: float = POLL_SECONDS,
    retry_seconds: float = RETRY_SECONDS,
    names_refresh_seconds: float = NAMES_REFRESH_SECONDS,
    purge_every_seconds: float = PURGE_EVERY_SECONDS,
    status: UploaderStatus | None = None,
    log: Callable[[str], None] = print,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Push whenever the folder changes, and every `retry_seconds` while something waits.
    `make_outbox()` builds the Outbox (again every `names_refresh_seconds`, for fresh names)."""
    from .cloud import outbox_items

    status = status or UploaderStatus()
    outbox, built_at = make_outbox(), clock()
    last_signature = None
    last_push = -retry_seconds
    last_purge = -purge_every_seconds
    while not should_stop():
        signature = folder_signature(detections_dir)
        # push when the folder changed, and every retry_seconds anyway (failed clips, fresh status)
        if signature != last_signature or clock() - last_push >= retry_seconds:
            if clock() - built_at >= names_refresh_seconds:
                outbox, built_at = make_outbox(), clock()
            started = clock()
            try:
                report, error = outbox.push(), ""
            except Exception as exc:  # e.g. Supabase unreachable while reading the Cestino list
                report, error = None, repr(exc)
            seconds = clock() - started
            last_push = clock()
            if report is None or not report.busy:  # busy: a manual sync is at it, look again soon
                # the folder as it was before this push: a clip written meanwhile counts as new
                last_signature = signature
                if report is not None and report.sent:
                    log(f"{datetime.now():%H:%M:%S} - su Supabase {report.sent} clip in {seconds:.1f} s")
                if error or (report is not None and report.errors):
                    log(f"{datetime.now():%H:%M:%S} - in attesa: {error or report.errors[0]}")
                status.write(outbox_items(detections_dir), report, seconds, error)
        if clock() - last_purge >= purge_every_seconds:
            last_purge = clock()
            try:
                purged, emptied, named = outbox.tidy()
                if purged:
                    log(f"{datetime.now():%H:%M:%S} - tolti da Supabase i file di {purged} clip gia' su iNaturalist")
                if emptied:
                    log(f"{datetime.now():%H:%M:%S} - eliminate {emptied} osservazioni rimaste senza clip")
                if named:
                    log(f"{datetime.now():%H:%M:%S} - nomi e link per {named} specie nuove (corrette sul sito)")
            except Exception as exc:  # offline: next hour
                log(f"{datetime.now():%H:%M:%S} - pulizia dei file gia' su iNaturalist rimandata: {exc!r}")
        sleep(poll_seconds)
