import threading
import time
import unittest

from kpi_crawler.acquisition_engine.concurrency import AdjustableGate


class AdjustableGateTests(unittest.TestCase):
    def test_never_exceeds_limit_under_real_concurrent_load(self):
        gate = AdjustableGate(3)
        concurrent_count = 0
        max_seen = 0
        lock = threading.Lock()

        def worker():
            nonlocal concurrent_count, max_seen
            gate.acquire()
            try:
                with lock:
                    concurrent_count += 1
                    max_seen = max(max_seen, concurrent_count)
                time.sleep(0.05)
            finally:
                with lock:
                    concurrent_count -= 1
                gate.release()

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertLessEqual(max_seen, 3)
        self.assertGreaterEqual(max_seen, 2)  # actually exercised concurrency, not accidentally serial

    def test_raising_limit_unblocks_waiting_threads(self):
        gate = AdjustableGate(1)
        gate.acquire()
        released = threading.Event()

        def waiter():
            gate.acquire()
            released.set()

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        self.assertFalse(released.is_set())
        gate.set_limit(2)
        t.join(timeout=2)
        self.assertTrue(released.is_set())

    def test_lowering_limit_blocks_new_acquires(self):
        gate = AdjustableGate(2)
        gate.acquire()
        gate.set_limit(1)
        acquired = threading.Event()

        def waiter():
            gate.acquire()
            acquired.set()

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        self.assertFalse(acquired.is_set())
        gate.release()
        t.join(timeout=2)
        self.assertTrue(acquired.is_set())


if __name__ == "__main__":
    unittest.main()
