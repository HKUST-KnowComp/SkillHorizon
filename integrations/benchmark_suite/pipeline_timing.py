"""Payload-free, exclusive wall-time spans for construction diagnostics."""

import re
import time
from pathlib import Path

from skillhorizon.util import write_json


class PhaseRecorder:
    def __init__(self, path, *, clock=time.monotonic, progress=None, notes=None):
        self.path = Path(path)
        self.clock = clock
        self.progress = progress or (lambda name: None)
        self.started = clock()
        self.active = None
        self.spans = []
        self.notes = list(notes or [])
        self.state = "running"
        self.error_class = None
        self._exception = None

    def __enter__(self):
        self._write()
        return self

    def _close(self, now, status):
        if self.active is not None:
            self.spans.append({**self.active, "seconds": now - self.active["start_seconds"],
                               "status": status})
            self.active = None

    def stage(self, name):
        if self.state != "running" or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError("Invalid timing transition")
        now = self.clock() - self.started
        self._close(now, "completed")
        self.active = {"name": name, "start_seconds": now}
        self.progress(name)
        self._write()

    def unavailable(self):
        if self.active is None:
            raise ValueError("No active phase")
        now = self.clock() - self.started
        self._close(now, "unavailable")
        self._write()

    def failed(self, error):
        self._exception = error
        self._close(self.clock() - self.started, "failed")
        self._write()

    def _write(self):
        write_json(self.path, {
            "revision": "construction_timing_v1", "state": self.state,
            "spans": self.spans, "active_phase": self.active,
            "recorded_seconds": sum(s["seconds"] for s in self.spans),
            "error_class": self.error_class, "notes": self.notes,
            "scope": "exclusive_diagnostic_spans_not_business_or_model_evidence",
        })

    def __exit__(self, kind, value, traceback):
        try:
            status = "failed" if kind and value is not self._exception else "completed"
            self._close(self.clock() - self.started, status)
            self.state = "failed" if kind else "completed"
            self.error_class = kind.__name__ if kind else None
            self._write()
        except Exception as diagnostic_error:
            if kind is None:
                raise
            value.add_note("Timing diagnostics failed: " + type(diagnostic_error).__name__)
        return False
