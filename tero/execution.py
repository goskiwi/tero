"""A shared deadline, including auxiliary model requests and child work."""

import threading
import time


class ExecutionStopped(RuntimeError):
    pass


class Budget:
    def __init__(self, seconds, *, deadline=None, cancelled=None):
        self.deadline = deadline if deadline is not None else time.monotonic() + seconds
        self.cancelled = cancelled if cancelled is not None else threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise ExecutionStopped("cancelled")
        if time.monotonic() >= self.deadline:
            raise ExecutionStopped("time_limit")

    def remaining(self, limit=None):
        self.check()
        remaining = self.deadline - time.monotonic()
        return max(0.001, min(remaining, limit) if limit is not None else remaining)

    def child(self, seconds):
        return Budget(
            seconds,
            deadline=min(self.deadline, time.monotonic() + seconds),
            cancelled=self.cancelled,
        )
