import unittest

from frigate.embeddings.embeddings import get_jina_v1_devices


class TestJinaV1Device(unittest.TestCase):
    def test_large_uses_cpu_for_text_and_gpu_for_vision(self) -> None:
        self.assertEqual(get_jina_v1_devices("large", None), ("CPU", "GPU"))

    def test_small_uses_cpu_for_text_and_vision(self) -> None:
        self.assertEqual(get_jina_v1_devices("small", None), ("CPU", "CPU"))

    def test_explicit_device_only_controls_vision(self) -> None:
        self.assertEqual(get_jina_v1_devices("large", "CPU"), ("CPU", "CPU"))
        self.assertEqual(get_jina_v1_devices("small", "GPU"), ("CPU", "GPU"))
