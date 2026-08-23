import unittest

from frigate.embeddings.embeddings import get_jina_v1_device


class TestJinaV1Device(unittest.TestCase):
    def test_large_uses_gpu_for_text_and_vision(self) -> None:
        self.assertEqual(get_jina_v1_device("large", None), "GPU")

    def test_small_remains_on_cpu(self) -> None:
        self.assertEqual(get_jina_v1_device("small", None), "CPU")

    def test_explicit_device_wins(self) -> None:
        self.assertEqual(get_jina_v1_device("large", "CPU"), "CPU")
        self.assertEqual(get_jina_v1_device("small", "GPU"), "GPU")
