import unittest

from kpi_crawler.acquisition_engine.contract import AcquisitionState, derive_run_status


class DeriveRunStatusTests(unittest.TestCase):
    def test_all_success_is_success(self):
        results = {"a": AcquisitionState.SUCCESS, "b": AcquisitionState.SUCCESS}
        self.assertEqual(derive_run_status(results), AcquisitionState.SUCCESS)

    def test_mixed_results_is_partial(self):
        results = {
            "a": AcquisitionState.SUCCESS,
            "b": AcquisitionState.RATE_LIMITED,
            "c": AcquisitionState.ACCESS_DENIED,
        }
        self.assertEqual(derive_run_status(results), AcquisitionState.PARTIAL)

    def test_all_failed_is_failed(self):
        results = {"a": AcquisitionState.NETWORK_ERROR, "b": AcquisitionState.TIMEOUT}
        self.assertEqual(derive_run_status(results), AcquisitionState.FAILED)

    def test_empty_is_failed(self):
        self.assertEqual(derive_run_status({}), AcquisitionState.FAILED)

    def test_single_success_is_success_not_partial(self):
        self.assertEqual(derive_run_status({"a": AcquisitionState.SUCCESS}), AcquisitionState.SUCCESS)


if __name__ == "__main__":
    unittest.main()
