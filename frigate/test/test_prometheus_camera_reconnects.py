"""Tests for public per-camera reconnect metrics."""

import unittest

from frigate.stats.prometheus import CustomCollector


class TestPrometheusCameraReconnects(unittest.TestCase):
    """Prove the rolling reconnect stat is exported without camera topology."""

    def test_exports_reconnects_last_hour_for_each_camera(self) -> None:
        collector = CustomCollector(None)
        collector.complete_stats = {
            "cameras": {
                "camera_a": {"reconnects_last_hour": 3},
                "camera_b": {"reconnects_last_hour": 0},
            }
        }

        family = next(
            metric
            for metric in collector.collect()
            if metric.name == "frigate_camera_reconnects_last_hour"
        )

        self.assertEqual(
            {sample.labels["camera_name"]: sample.value for sample in family.samples},
            {"camera_a": 3.0, "camera_b": 0.0},
        )

    def test_omits_reconnect_metric_when_stat_is_unavailable(self) -> None:
        collector = CustomCollector(None)
        collector.complete_stats = {
            "cameras": {
                "camera_a": {},
                "camera_b": {"reconnects_last_hour": "unavailable"},
            }
        }

        family = next(
            metric
            for metric in collector.collect()
            if metric.name == "frigate_camera_reconnects_last_hour"
        )

        self.assertEqual([], family.samples)


if __name__ == "__main__":
    unittest.main()
