import unittest

from kpi_crawler.acquisition_engine.sessions import SessionHealth, SessionManager


class SessionManagerTests(unittest.TestCase):
    def test_create_returns_unique_healthy_sessions(self):
        manager = SessionManager()
        a = manager.create()
        b = manager.create()
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(a.health, SessionHealth.HEALTHY)

    def test_repeated_failures_become_unhealthy(self):
        manager = SessionManager(unhealthy_after_failures=2)
        session = manager.create()
        self.assertEqual(manager.report_failure(session.session_id), SessionHealth.HEALTHY)
        self.assertEqual(manager.report_failure(session.session_id), SessionHealth.UNHEALTHY)

    def test_success_recovers_from_unhealthy(self):
        manager = SessionManager(unhealthy_after_failures=1)
        session = manager.create()
        manager.report_failure(session.session_id)
        self.assertEqual(session.health, SessionHealth.UNHEALTHY)
        manager.report_success(session.session_id)
        self.assertEqual(session.health, SessionHealth.HEALTHY)
        self.assertEqual(session.consecutive_failures, 0)

    def test_retire_marks_session_retired_and_excludes_from_active_count(self):
        manager = SessionManager()
        session = manager.create()
        manager.create()
        self.assertEqual(manager.active_count(), 2)
        manager.retire(session.session_id)
        self.assertEqual(manager.active_count(), 1)
        self.assertEqual(manager.get(session.session_id).health, SessionHealth.RETIRED)

    def test_unknown_session_id_is_handled_gracefully(self):
        manager = SessionManager()
        self.assertEqual(manager.report_failure("nonexistent"), SessionHealth.RETIRED)
        manager.report_success("nonexistent")  # must not raise
        manager.retire("nonexistent")  # must not raise


if __name__ == "__main__":
    unittest.main()
