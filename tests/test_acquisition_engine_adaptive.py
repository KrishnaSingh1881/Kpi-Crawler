import unittest

from kpi_crawler.acquisition_engine.adaptive import AdaptivePolicy


class AdaptivePolicyTests(unittest.TestCase):
    def test_rate_limit_halves_concurrency(self):
        policy = AdaptivePolicy(initial_concurrency=8, min_concurrency=1)
        signal = policy.on_rate_limited("example.edu")
        self.assertEqual(signal.decision_type, "concurrency_reduced")
        self.assertEqual(signal.before_value, 8)
        self.assertEqual(signal.after_value, 4)
        self.assertEqual(policy.current_concurrency("example.edu"), 4)

    def test_concurrency_never_drops_below_minimum(self):
        policy = AdaptivePolicy(initial_concurrency=1, min_concurrency=1)
        signal = policy.on_rate_limited("example.edu")
        self.assertEqual(signal.after_value, 1)
        self.assertEqual(signal.decision_type, "backoff")  # no-op concurrency change, still backs off

    def test_backoff_grows_exponentially_and_caps(self):
        policy = AdaptivePolicy(base_backoff_seconds=1.0, max_backoff_seconds=10.0, min_concurrency=1, initial_concurrency=1)
        backoffs = [policy.on_rate_limited("example.edu").backoff_seconds for _ in range(6)]
        self.assertEqual(backoffs, [1.0, 2.0, 4.0, 8.0, 10.0, 10.0])

    def test_success_restores_concurrency_after_threshold(self):
        policy = AdaptivePolicy(initial_concurrency=8, max_concurrency=8, restore_after_successes=3)
        policy.on_rate_limited("example.edu")  # drop to 4
        self.assertIsNone(policy.on_success("example.edu"))
        self.assertIsNone(policy.on_success("example.edu"))
        signal = policy.on_success("example.edu")
        self.assertEqual(signal.decision_type, "concurrency_restored")
        self.assertEqual(signal.before_value, 4)
        self.assertEqual(signal.after_value, 5)

    def test_success_never_exceeds_max_concurrency(self):
        policy = AdaptivePolicy(initial_concurrency=8, max_concurrency=8, restore_after_successes=1)
        self.assertIsNone(policy.on_success("example.edu"))

    def test_domains_are_tracked_independently(self):
        policy = AdaptivePolicy(initial_concurrency=8)
        policy.on_rate_limited("a.edu")
        self.assertEqual(policy.current_concurrency("a.edu"), 4)
        self.assertEqual(policy.current_concurrency("b.edu"), 8)

    def test_rate_limit_resets_consecutive_success_streak(self):
        policy = AdaptivePolicy(initial_concurrency=8, restore_after_successes=2)
        policy.on_rate_limited("example.edu")
        self.assertIsNone(policy.on_success("example.edu"))  # 1 of 2
        policy.on_rate_limited("example.edu")  # resets success streak
        self.assertIsNone(policy.on_success("example.edu"))  # 1 of 2 again, not 2 of 2


if __name__ == "__main__":
    unittest.main()
