"""How fast and how heavy the live service is, so we notice when it starts taking too long.

Over the last 10 minutes: how long BirdNET takes per window and how much of the available time
that is (a new window comes every half slice: above 100% the live falls behind), how far behind
the microphone the analysis is, memory (now and peak), CPU, what the PC-playback subtraction
costs and how long sending a clip to Supabase takes. Shown under the live table, written into
the heartbeat (the health check warns when the live falls behind or grows too big).
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

WINDOW_SECONDS = 10 * 60


def memory_mb() -> tuple[float, float]:
    """(resident now, peak) of this process, in MB."""
    values = {}
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(("VmRSS:", "VmHWM:")):
                    key, amount = line.split(":", 1)
                    values[key] = int(amount.split()[0]) / 1024
    except OSError:
        pass
    return values.get("VmRSS", 0.0), values.get("VmHWM", 0.0)


def cpu_seconds() -> float:
    times = os.times()
    return times.user + times.system


class LiveMetrics:
    def __init__(self, window_seconds: float = WINDOW_SECONDS, clock=time.monotonic) -> None:
        self.window_seconds = window_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.windows: deque[tuple[float, float, float, float]] = deque()  # (t, analysis, budget, lag)
        self.echo: deque[tuple[float, float, float]] = deque()  # (t, seconds spent, audio seconds)
        self.uploads: deque[tuple[float, float, int]] = deque()  # (t, seconds, clips)
        self.cpu: deque[tuple[float, float]] = deque([(clock(), cpu_seconds())])

    def _trim(self, now: float) -> None:
        for samples in (self.windows, self.echo, self.uploads):
            while samples and samples[0][0] < now - self.window_seconds:
                samples.popleft()
        while len(self.cpu) > 1 and self.cpu[1][0] < now - self.window_seconds:
            self.cpu.popleft()

    def record_window(self, analysis_seconds: float, budget_seconds: float, lag_seconds: float) -> None:
        now = self.clock()
        with self.lock:
            self.windows.append((now, analysis_seconds, budget_seconds, lag_seconds))
            self.cpu.append((now, cpu_seconds()))
            self._trim(now)

    def record_echo(self, seconds: float, audio_seconds: float) -> None:
        now = self.clock()
        with self.lock:
            # one entry per second of audio is plenty: merge into the last one
            if self.echo and now - self.echo[-1][0] < 1.0:
                t, spent, audio = self.echo[-1]
                self.echo[-1] = (t, spent + seconds, audio + audio_seconds)
            else:
                self.echo.append((now, seconds, audio_seconds))

    def record_upload(self, seconds: float, clips: int) -> None:
        if clips:
            with self.lock:
                self.uploads.append((self.clock(), seconds, clips))

    def snapshot(self) -> dict:
        """Numbers for the heartbeat; empty until the first window is analysed."""
        now = self.clock()
        with self.lock:
            self._trim(now)
            if not self.windows:
                return {}
            analysis = [entry[1] for entry in self.windows]
            budget = sum(entry[2] for entry in self.windows)
            (first_t, first_cpu), (last_t, last_cpu) = self.cpu[0], self.cpu[-1]
            echo_spent = sum(entry[1] for entry in self.echo)
            echo_audio = sum(entry[2] for entry in self.echo)
            uploaded = sum(entry[2] for entry in self.uploads)
            rss, peak = memory_mb()
            return {
                "windows": len(analysis),
                "analysis_avg_s": round(sum(analysis) / len(analysis), 3),
                "analysis_max_s": round(max(analysis), 3),
                "load_pct": round(100 * sum(analysis) / budget, 1) if budget else None,
                "lag_s": round(self.windows[-1][3], 2),
                "lag_max_s": round(max(entry[3] for entry in self.windows), 2),
                "rss_mb": round(rss),
                "peak_rss_mb": round(peak),
                "cpu_pct": round(100 * (last_cpu - first_cpu) / (last_t - first_t), 1) if last_t > first_t else None,
                "echo_pct": round(100 * echo_spent / echo_audio, 2) if echo_audio else None,
                "upload_s_per_clip": round(sum(entry[1] for entry in self.uploads) / uploaded, 2) if uploaded else None,
            }

    def summary_lines(self) -> list[str]:
        """Two short lines for under the live table (nothing before the first window)."""
        data = self.snapshot()
        if not data:
            return []
        timing = (f"Ultimi {self.window_seconds / 60:.0f} min: BirdNET {data['analysis_avg_s']:.2f} s/finestra "
                  f"(max {data['analysis_max_s']:.2f}, {data['load_pct']:.0f}% del tempo), "
                  f"ritardo {data['lag_s']:.1f} s")
        resources = [f"RAM {data['rss_mb']} MB (picco {data['peak_rss_mb']})"]
        if data["cpu_pct"] is not None:
            resources.append(f"CPU {data['cpu_pct']:.0f}%")
        if data["echo_pct"] is not None:
            resources.append(f"sottrazione PC {data['echo_pct']:.1f}% CPU")
        if data["upload_s_per_clip"] is not None:
            resources.append(f"invio {data['upload_s_per_clip']:.1f} s/clip")
        return [timing, " · ".join(resources)]
