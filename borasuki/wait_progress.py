"""Measured phase progress; unknown work never gets a synthetic percentage."""

import json
import logging
import math
import statistics
import time
from pathlib import Path

from borasuki.storage import atomic_json

logger = logging.getLogger(__name__)


class WaitProgress:
    def __init__(self, update, history=None):
        self.update = update
        self.started = self.phase_started = time.monotonic()
        self.stage = None
        self.completed = self.total = None
        self.baseline = None
        self.durations = {}
        self.history_path = history
        self.history = {}
        self.last_poll = 0
        if history and Path(history).exists():
            try:
                saved = json.loads(Path(history).read_text(encoding='utf-8'))
                self.history = {key: [float(x) for x in values[-5:] if 0 < float(x) < 1800]
                                for key, values in saved.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                logger.warning('Preparation duration history is invalid: %s', history, exc_info=True)

    def set(self, stage, completed=None, total=None):
        now = time.monotonic()
        if stage != self.stage:
            if self.stage:
                self.durations[self.stage] = self.durations.get(self.stage, 0) + now - self.phase_started
            self.stage, self.phase_started, self.baseline = stage, now, None
        self.completed, self.total = completed, total
        if completed and self.baseline is None:
            self.baseline = (now, completed)
        self.publish()

    def publish(self):
        now = time.monotonic()
        phase_elapsed = now - self.phase_started
        eta = None
        basis = None
        if self.total and self.baseline and self.completed < self.total:
            duration, count = now - self.baseline[0], self.completed - self.baseline[1]
            if count >= 2 and duration >= 1:
                eta = math.ceil((self.total - self.completed) * duration / count)
                basis = 'samples'
        previous = self.history.get(self.stage, [])
        if eta is None and previous and not self.total:
            expected = statistics.median(previous)
            if phase_elapsed < expected:
                eta, basis = math.ceil(expected - phase_elapsed), 'history'
        self.update(progress={'stage': self.stage, 'completed': self.completed, 'total': self.total,
                              'elapsed': now - self.started, 'eta': eta, 'eta_basis': basis,
                              'updated_at': time.time()})

    def poll(self, path, force=False):
        now = time.monotonic()
        if not force and now - self.last_poll < 0.5:
            return
        self.last_poll = now
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            self.publish()
            return
        self.set(value['stage'], value.get('completed'), value.get('total'))

    def finish(self):
        if self.stage:
            self.durations[self.stage] = self.durations.get(self.stage, 0) + time.monotonic() - self.phase_started
        if self.history_path:
            for stage, duration in self.durations.items():
                self.history[stage] = (self.history.get(stage, []) + [duration])[-5:]
            try:
                atomic_json(self.history_path, self.history)
            except OSError:
                logger.warning('Preparation duration history could not be saved', exc_info=True)
