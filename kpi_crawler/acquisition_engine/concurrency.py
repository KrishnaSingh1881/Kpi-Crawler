"""A concurrency gate whose limit can be raised or lowered while threads are
waiting on it — `threading.Semaphore` only supports releasing more permits,
not shrinking, so the adaptive policy (which does both) needs this instead.
"""

import threading


class AdjustableGate:
    def __init__(self, limit: int):
        self._limit = limit
        self._count = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self._count >= self._limit:
                self._cond.wait()
            self._count += 1

    def release(self) -> None:
        with self._cond:
            self._count -= 1
            self._cond.notify()

    def set_limit(self, new_limit: int) -> None:
        with self._cond:
            self._limit = new_limit
            self._cond.notify_all()
