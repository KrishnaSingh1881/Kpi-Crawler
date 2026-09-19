import unittest

from kpi_crawler.acquisition_engine.proxy import ProxyConfig, ProxyHealthState, ProxyPool


class ProxyPoolTests(unittest.TestCase):
    def test_empty_pool_returns_none(self):
        pool = ProxyPool()
        self.assertIsNone(pool.acquire())

    def test_rotates_through_configured_proxies(self):
        pool = ProxyPool([ProxyConfig("p1", "http://p1"), ProxyConfig("p2", "http://p2")])
        seen = {pool.acquire().proxy_id for _ in range(4)}
        self.assertEqual(seen, {"p1", "p2"})

    def test_failure_threshold_quarantines_proxy(self):
        pool = ProxyPool([ProxyConfig("p1", "http://p1")], failure_threshold=3)
        pool.report_failure("p1")
        self.assertEqual(pool.health_of("p1"), ProxyHealthState.UNHEALTHY)
        pool.report_failure("p1")
        self.assertEqual(pool.health_of("p1"), ProxyHealthState.UNHEALTHY)
        health = pool.report_failure("p1")
        self.assertEqual(health, ProxyHealthState.QUARANTINED)
        self.assertEqual(pool.health_of("p1"), ProxyHealthState.QUARANTINED)

    def test_quarantined_proxy_is_never_returned(self):
        pool = ProxyPool([ProxyConfig("p1", "http://p1")], failure_threshold=1)
        pool.report_failure("p1")
        self.assertEqual(pool.acquire(), None)

    def test_success_resets_failure_count_and_health(self):
        pool = ProxyPool([ProxyConfig("p1", "http://p1")], failure_threshold=3)
        pool.report_failure("p1")
        pool.report_failure("p1")
        pool.report_success("p1")
        self.assertEqual(pool.health_of("p1"), ProxyHealthState.HEALTHY)
        pool.report_failure("p1")
        pool.report_failure("p1")
        self.assertEqual(pool.health_of("p1"), ProxyHealthState.UNHEALTHY)  # not yet quarantined after reset

    def test_report_success_signals_recovery_only_on_the_transition(self):
        pool = ProxyPool([ProxyConfig("p1", "http://p1")], failure_threshold=3)
        self.assertFalse(pool.report_success("p1"), "already-healthy success is not a recovery")
        pool.report_failure("p1")
        self.assertTrue(pool.report_success("p1"), "the call that brings it back to healthy is a recovery")
        self.assertFalse(pool.report_success("p1"), "a second success right after is not a new recovery")

    def test_healthy_and_quarantined_counts(self):
        pool = ProxyPool(
            [ProxyConfig("p1", "http://p1"), ProxyConfig("p2", "http://p2")], failure_threshold=1
        )
        pool.report_failure("p1")
        self.assertEqual(pool.quarantined_count(), 1)
        self.assertEqual(pool.healthy_count(), 1)


if __name__ == "__main__":
    unittest.main()
