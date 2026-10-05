"""The usable crop, rather than an out-of-bounds box, must meet min_area."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from frigate.data_processing.real_time.face import FaceRealTimeProcessor


class TestFaceCropArea(unittest.TestCase):
    def processor(self, manual):
        proc = FaceRealTimeProcessor.__new__(FaceRealTimeProcessor)
        proc.config = SimpleNamespace(
            cameras={
                "sample": SimpleNamespace(
                    face_recognition=SimpleNamespace(enabled=True, min_area=5000)
                )
            }
        )
        proc.face_config = SimpleNamespace(detection_threshold=0.7)
        proc.metrics = SimpleNamespace(face_rec_fps=SimpleNamespace(value=0))
        proc.faces_per_second = Mock()
        proc.faces_per_second.eps.return_value = 0
        proc.person_face_history = {}
        proc.requires_face_detection = manual
        proc.recognizer = Mock()
        proc.recognizer.classify.return_value = None
        proc._FaceRealTimeProcessor__detect_face = Mock(return_value=(0, 0, 100, 100))
        proc._FaceRealTimeProcessor__update_metrics = Mock()
        return proc

    def frame(self):
        return np.full((192, 128), 128, dtype=np.uint8)

    def object(self, box, attributes=None):
        return {
            "id": "event",
            "label": "person",
            "camera": "sample",
            "box": box,
            "current_attributes": attributes or [],
        }

    def test_manual_box_beyond_person_crop_is_rejected(self):
        proc = self.processor(True)
        proc.process_frame(self.object([0, 0, 40, 40]), self.frame())
        proc.recognizer.classify.assert_not_called()

    def test_large_manual_crop_reaches_recognition(self):
        proc = self.processor(True)
        proc.process_frame(self.object([0, 0, 120, 120]), self.frame())
        proc.recognizer.classify.assert_called_once()
        self.assertEqual(
            proc.recognizer.classify.call_args.args[0].shape[:2], (100, 100)
        )

    def test_attribute_box_beyond_frame_is_rejected(self):
        proc = self.processor(False)
        attrs = [{"label": "face", "score": 0.9, "box": [-80, 0, 20, 100]}]
        proc.process_frame(self.object([0, 0, 128, 128], attrs), self.frame())
        proc.recognizer.classify.assert_not_called()

    def test_clipped_attribute_crop_at_exact_floor_reaches_recognition(self):
        proc = self.processor(False)
        attrs = [{"label": "face", "score": 0.9, "box": [-50, 0, 50, 100]}]
        proc.process_frame(self.object([0, 0, 128, 128], attrs), self.frame())
        proc.recognizer.classify.assert_called_once()
        self.assertEqual(
            proc.recognizer.classify.call_args.args[0].shape[:2], (100, 50)
        )


if __name__ == "__main__":
    unittest.main()
