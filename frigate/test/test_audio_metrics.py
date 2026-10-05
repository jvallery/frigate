"""Audio counters retain shared handles for each maintainer's lifetime."""

import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from frigate.events.audio import AudioEventMaintainer, AudioProcessor


class CountingMetrics(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.lookups = 0

    def __getitem__(self, key):
        self.lookups += 1
        return super().__getitem__(key)


def metrics():
    return SimpleNamespace(
        audio_rms=SimpleNamespace(value=0.0),
        audio_dBFS=SimpleNamespace(value=0.0),
    )


class TestAudioMetrics(unittest.TestCase):
    def make_maintainer(self, shared, enabled=True):
        camera = SimpleNamespace(
            name="sample",
            enabled=True,
            ffmpeg=None,
            audio=SimpleNamespace(
                enabled=enabled,
                num_threads=2,
                min_volume=400,
                listen=["speech"],
                filters=None,
            ),
            audio_transcription=SimpleNamespace(enabled=False),
        )
        with ExitStack() as stack:
            for name in (
                "AudioTfl",
                "LogPipe",
                "InterProcessRequestor",
                "CameraConfigUpdateSubscriber",
                "DetectionPublisher",
            ):
                stack.enter_context(patch(f"frigate.events.audio.{name}"))
            stack.enter_context(
                patch("frigate.events.audio.get_ffmpeg_command", return_value=[])
            )
            maintainer = AudioEventMaintainer(
                camera, SimpleNamespace(), shared, None, threading.Event()
            )
        maintainer.detector = Mock()
        maintainer.detector.detect.return_value = []
        return maintainer

    def test_repeated_chunks_write_the_shared_counters_without_reloading_bundle(self):
        counters = metrics()
        shared = CountingMetrics(sample=counters)
        maintainer = self.make_maintainer(shared)
        for amplitude in (500, 1000, 1500):
            audio = np.full(16000, amplitude, dtype=np.int16)
            maintainer.detect_audio(audio)
            self.assertAlmostEqual(counters.audio_rms.value, amplitude, delta=0.001)
            expected = 20 * np.log10(amplitude / 32768.0)
            self.assertAlmostEqual(counters.audio_dBFS.value, expected, delta=0.00001)
        self.assertEqual(shared.lookups, 1)
        self.assertEqual(maintainer.requestor.send_data.call_count, 9)

    def test_replacement_maintainer_binds_the_replacement_counters(self):
        old = metrics()
        shared = CountingMetrics(sample=old)
        first = self.make_maintainer(shared)
        first.stop()
        replacement = metrics()
        shared["sample"] = replacement
        second = self.make_maintainer(shared)
        second.detect_audio(np.full(16000, 1000, dtype=np.int16))
        self.assertEqual(old.audio_rms.value, 0)
        self.assertEqual(replacement.audio_rms.value, 1000)
        self.assertEqual(shared.lookups, 2)

    def test_runtime_add_retries_until_metrics_are_available(self):
        shared = {}
        camera = SimpleNamespace(
            name="sample",
            enabled=True,
            enabled_in_config=True,
            audio=SimpleNamespace(enabled=True),
            audio_transcription=SimpleNamespace(enabled=False),
            ffmpeg=SimpleNamespace(inputs=[SimpleNamespace(roles=["audio"])]),
        )
        proc = AudioProcessor.__new__(AudioProcessor)
        proc._closed = False
        proc._popen = None
        proc.config = SimpleNamespace(logger={}, cameras={"sample": camera})
        proc.camera_metrics = shared
        proc.pre_run_setup = Mock()
        proc.logger = Mock()
        thread = Mock()
        thread.is_alive.return_value = False
        polls = 0

        def wait(timeout):
            nonlocal polls
            polls += 1
            if polls == 1:
                shared["sample"] = metrics()
                return False
            return True

        proc.stop_event = SimpleNamespace(wait=wait)

        def spawn(*args):
            shared[camera.name]  # raises on the initial, out-of-order add
            return thread

        with (
            patch("frigate.events.audio.CameraConfigUpdateSubscriber"),
            patch(
                "frigate.events.audio.AudioEventMaintainer", side_effect=spawn
            ) as factory,
        ):
            proc.run()
        self.assertEqual(factory.call_count, 2)
        thread.start.assert_called_once()
        self.assertEqual(proc.audio_threads, {"sample": thread})

    def test_disabled_audio_does_not_write_or_publish(self):
        counters = metrics()
        maintainer = self.make_maintainer(CountingMetrics(sample=counters), False)
        maintainer.detect_audio(np.full(16000, 1000, dtype=np.int16))
        self.assertEqual(counters.audio_rms.value, 0)
        maintainer.requestor.send_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
