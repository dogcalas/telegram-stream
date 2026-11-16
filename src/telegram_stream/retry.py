"""Capped exponential backoff with equal jitter."""

import random


class Backoff:
    def __init__(self, initial: float, maximum: float) -> None:
        self.initial = initial
        self.maximum = maximum
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    def next_delay(self) -> float:
        cap = min(self.maximum, self.initial * 2 ** min(self.attempt, 20))
        self.attempt += 1
        return random.uniform(cap / 2, cap)
