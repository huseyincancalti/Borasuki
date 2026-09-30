"""Rolling output throughput; cold engine build time is not frame time."""

from collections import deque
import math


class RenderEstimate:
    def __init__(self):
        self.samples = deque()
        self.startups = deque(maxlen=4)
        self.attempts = 0
        self.speed = None
        self.started = None
        self.cycles = deque(maxlen=4)

    def complete(self, frames, now):
        if frames <= 0 or self.started is None or now <= self.started:
            raise ValueError('Invalid completed segment measurement')
        self.cycles.append((frames, now - self.started))
        self.speed = sum(item[0] for item in self.cycles) / sum(item[1] for item in self.cycles)

    def begin(self, now):
        self.samples.clear()
        self.started = now
        self.attempts += 1

    def observe(self, frame, now, remaining, future_segments=0):
        if frame <= 0:
            return self.speed, None
        if not self.samples:
            # The first startup can include minutes of one-time TensorRT compilation.
            if self.attempts > 1:
                self.startups.append(now - self.started)
            self.samples.append((now, frame))
            return self.speed, self.remaining(remaining, future_segments)
        if frame < self.samples[-1][1]:
            raise ValueError('Frame counter must be monotonic within an attempt')
        if now <= self.samples[-1][0]:
            return self.speed, self.remaining(remaining, future_segments)
        self.samples.append((now, frame))
        while len(self.samples) > 2 and self.samples[1][0] <= now - 12:
            self.samples.popleft()
        elapsed = now - self.samples[0][0]
        if elapsed >= 1 and not self.cycles:
            self.speed = (frame - self.samples[0][1]) / elapsed
        elif self.cycles:
            # Whole verified segments include startup and encoder drain, not flush bursts.
            self.speed = sum(item[0] for item in self.cycles) / sum(item[1] for item in self.cycles)
        return self.speed, self.remaining(remaining, future_segments)

    def remaining(self, frames, future_segments):
        if not self.speed or self.speed <= 0:
            return None
        startup = sum(self.startups) / len(self.startups) if self.startups and not self.cycles else 0
        return math.ceil(max(0, frames) / self.speed + future_segments * startup)
