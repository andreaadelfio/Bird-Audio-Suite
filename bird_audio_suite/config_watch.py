"""config.py read again while the services run.

The live listener and the uploader look at config.py's modification time every few seconds (a
single stat(), nothing is read until it changes) and then run it again: the thresholds apply from
the next listening window, without a restart. Values that shape the audio pipeline (sample rate,
frame length, devices, ...) are read only at start: for those the log says a restart is needed.
A file saved halfway or with a typo is not taken: the values in use stay.
"""

from __future__ import annotations

import importlib
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config

CHECK_EVERY_SECONDS = 3.0


def _values() -> dict:
    return {name: getattr(config, name) for name in dir(config) if name.isupper()}


class ConfigWatcher:
    def __init__(self, applied: set[str], *, report_others: bool = True, every: float = CHECK_EVERY_SECONDS,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                 path: Path | None = None) -> None:
        """applied: the names this process takes while running; report_others: also log the
        other changes, as needing a restart (the live does, the uploader leaves that to it)."""
        self.applied, self.report_others = set(applied), report_others
        self.path = Path(path or config.__file__)
        self.every, self.log, self.clock = every, log, clock
        self.mtime = self._mtime()
        self.checked = clock()

    def _mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def poll(self) -> dict[str, tuple]:
        """{name: (old, new)} of what changed in config.py since the last look; {} almost always."""
        now = self.clock()
        if now - self.checked < self.every:
            return {}
        self.checked = now
        mtime = self._mtime()
        if mtime is None or mtime == self.mtime:
            return {}
        self.mtime = mtime
        before = _values()
        try:
            importlib.reload(config)
        except Exception as exc:
            for name, value in before.items():
                setattr(config, name, value)
            self.log(f"{datetime.now():%H:%M:%S} - config.py non letto ({exc!r}): restano i valori di prima")
            return {}
        changes = {name: (before.get(name), value) for name, value in _values().items() if before.get(name) != value}
        for name, (old, new) in changes.items():
            if name in self.applied:
                self.log(f"{datetime.now():%H:%M:%S} - config.py: {name} {old} -> {new}")
            elif self.report_others:
                self.log(f"{datetime.now():%H:%M:%S} - config.py: {name} {old} -> {new} (vale dal prossimo riavvio)")
        return changes
