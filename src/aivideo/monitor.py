from __future__ import annotations

import subprocess
import threading
import time

from .common import AnalysisError


def gpu_memory() -> float:
    try:
        process = subprocess.run(["nvidia-smi", "--id=0", "--query-gpu=memory.used",
                                  "--format=csv,noheader,nounits"], capture_output=True,
                                 timeout=10, check=True)
        return float(process.stdout.decode().strip())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AnalysisError(f"Не удалось измерить память GPU: {exc}") from exc


class GPUMonitor:
    """Observed total GPU memory, including desktop/other apps, not process allocations."""

    def __enter__(self):
        self.baseline = gpu_memory()
        self.peak = self.baseline
        self.errors = []
        self.started = time.perf_counter()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)
        self.thread.start()
        return self

    def _poll(self):
        while not self.stop_event.wait(0.5):
            try:
                self.peak = max(self.peak, gpu_memory())
            except AnalysisError as exc:
                self.errors.append(str(exc))

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=12)
        try:
            self.peak = max(self.peak, gpu_memory())
        except AnalysisError as exc:
            self.errors.append(str(exc))
        self.elapsed = time.perf_counter() - self.started

    def result(self):
        return {"wall_time_s": round(self.elapsed, 3),
                "gpu_baseline_mib": self.baseline, "gpu_observed_peak_mib": self.peak,
                "gpu_measurement": "total device 0 memory, sampled every 0.5 s",
                "measurement_errors": self.errors}
