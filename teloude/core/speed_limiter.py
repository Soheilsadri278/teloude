# teloude/core/speed_limiter.py
"""Cooperative transfer rate limiting (token bucket).

The limiter throttles actual byte flow: transfer loops call consume(n) after
moving n bytes. Unlimited mode is a no-op. Clock and sleeper are injectable
so tests are deterministic (no real sleeping).
"""
import time
from typing import Callable, Optional


class SpeedLimiter:
    def __init__(
        self,
        bytes_per_sec: Optional[int],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if bytes_per_sec is not None and bytes_per_sec <= 0:
            raise ValueError("bytes_per_sec must be positive or None (unlimited).")
        self._rate = bytes_per_sec
        self._clock = clock
        self._sleeper = sleeper
        self._allowance = float(bytes_per_sec) if bytes_per_sec else 0.0
        self._updated = clock()

    @property
    def bytes_per_sec(self) -> Optional[int]:
        return self._rate

    @property
    def is_unlimited(self) -> bool:
        return self._rate is None

    def consume(self, nbytes: int) -> None:
        """Blocks just long enough to keep the average rate at the limit."""
        if self._rate is None or nbytes <= 0:
            return
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._allowance = min(float(self._rate), self._allowance + elapsed * self._rate)
        if self._allowance < nbytes:
            deficit = nbytes - self._allowance
            self._sleeper(deficit / self._rate)
            self._updated = self._clock()
            self._allowance = 0.0
        else:
            self._allowance -= nbytes


PRESET_LIMITS_MBPS = (None, 10.0, 5.0, 2.0)  # None == Unlimited


def limit_from_mbps(mbps: Optional[float]) -> Optional[int]:
    """Converts a UI speed choice (MB/s) to bytes/sec for SpeedLimiter."""
    if mbps is None:
        return None
    if mbps <= 0:
        raise ValueError("Speed limit must be positive.")
    return int(mbps * 1024 * 1024)
