"""An optional ArcFace guard abstains on competing sample-level matches."""

import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from pydantic import ValidationError

import frigate.embeddings  # noqa: F401 - initialize embeddings before its processors
from frigate.config.classification import FaceRecognitionConfig
from frigate.data_processing.common.face.model import (
    ArcFaceRecognizer,
    nearest_sample_margin,
    normalize_face_samples,
)
from frigate.data_processing.real_time.face import FaceRealTimeProcessor


class TestFaceSampleMargin(unittest.TestCase):
    def recognizer(self, margin=0.05, ambiguous=True):
        rec = ArcFaceRecognizer.__new__(ArcFaceRecognizer)
        rec.config = SimpleNamespace(
            face_recognition=FaceRecognitionConfig(min_sample_margin=margin)
        )
        rec.landmark_detector = Mock()
        rec.mean_embs = {"alpha": np.array([1.0, 0.0]), "beta": np.array([0.0, 1.0])}
        rec.sample_embs = {
            "alpha": normalize_face_samples([np.array([1.0, 0.0])]),
            "beta": normalize_face_samples(
                [np.array([1.0, 0.0]), np.array([-1.0, 0.0]), np.array([0.0, 1.0])]
                if ambiguous
                else [np.array([0.0, 1.0])]
            ),
        }
        rec.face_embedder = Mock(return_value=[np.array([[1.0, 0.0]])])
        rec.align_face = Mock(side_effect=lambda image, width, height: image)
        rec.get_blur_confidence_reduction = Mock(return_value=0.0)
        rec.model_builder_failed = False
        rec.model_builder_queue = None
        return rec

    def image(self):
        return np.full((100, 100, 3), 128, dtype=np.uint8)

    def test_default_and_explicit_zero_keep_existing_class_mean_result(self):
        self.assertEqual(FaceRecognitionConfig().min_sample_margin, 0.0)
        rec = self.recognizer(margin=0)
        name, score = rec.classify(self.image())
        self.assertEqual(name, "alpha")
        self.assertGreater(score, 0.99)

    def test_competing_samples_reject_even_a_near_one_centroid_confidence(self):
        rec = self.recognizer()
        self.assertEqual(rec.classify(self.image()), ("unknown", 0.0))

    def test_separated_samples_keep_the_existing_winner_and_confidence(self):
        rec = self.recognizer(ambiguous=False)
        guarded = rec.classify(self.image())
        rec.config.face_recognition.min_sample_margin = 0
        self.assertEqual(guarded, rec.classify(self.image()))
        self.assertEqual(guarded[0], "alpha")

    def test_exact_margin_boundary_is_accepted(self):
        rec = self.recognizer(margin=1.0, ambiguous=False)
        self.assertEqual(rec.classify(self.image())[0], "alpha")

    def test_runtime_margin_changes_apply_without_rebuilding_samples(self):
        rec = self.recognizer(margin=0)
        self.assertEqual(rec.classify(self.image())[0], "alpha")
        rec.config.face_recognition = FaceRecognitionConfig(min_sample_margin=0.05)
        self.assertEqual(rec.classify(self.image()), ("unknown", 0.0))
        rec.config.face_recognition = FaceRecognitionConfig(min_sample_margin=0)
        self.assertEqual(rec.classify(self.image())[0], "alpha")

    def test_clear_discards_both_centroids_and_samples(self):
        rec = self.recognizer()
        rec.clear()
        self.assertEqual(rec.mean_embs, {})
        self.assertEqual(rec.sample_embs, {})

    def test_build_replaces_both_maps_and_skips_empty_identity_folders(self):
        rec = self.recognizer()
        rec.model_builder_queue = queue.Queue()
        rec.model_builder_queue.put({"gamma": [np.array([3.0, 4.0])], "empty": []})
        rec.build()
        self.assertEqual(set(rec.mean_embs), {"gamma"})
        self.assertEqual(set(rec.sample_embs), {"gamma"})
        np.testing.assert_allclose(rec.sample_embs["gamma"], [[0.6, 0.8]])

    def test_normalization_excludes_invalid_or_zero_samples(self):
        samples = normalize_face_samples(
            [np.array([3.0, 4.0]), np.zeros(2), np.array([np.nan, 1.0])]
        )
        np.testing.assert_allclose(samples, [[0.6, 0.8]])

    def test_margin_handles_unusable_queries_missing_references_and_one_identity(self):
        rec = self.recognizer()
        self.assertEqual(
            nearest_sample_margin(np.zeros(2), rec.sample_embs, "alpha"), 0
        )
        self.assertEqual(
            nearest_sample_margin(np.array([np.nan, 1]), rec.sample_embs, "alpha"), 0
        )
        self.assertEqual(nearest_sample_margin(np.array([1.0, 0]), {}, "alpha"), 0)
        self.assertEqual(
            nearest_sample_margin(
                np.array([1.0, 0]), {"alpha": rec.sample_embs["alpha"]}, "alpha"
            ),
            float("inf"),
        )

    def test_competitor_winning_samples_rejects_the_centroid_winner(self):
        rec = self.recognizer(ambiguous=False)
        rec.sample_embs["alpha"] = normalize_face_samples([np.array([0.6, 0.8])])
        rec.sample_embs["beta"] = normalize_face_samples([np.array([1.0, 0.0])])
        self.assertLess(
            nearest_sample_margin(np.array([1.0, 0]), rec.sample_embs, "alpha"), 0
        )
        self.assertEqual(rec.classify(self.image()), ("unknown", 0.0))

    def test_tied_losing_identities_do_not_reject_a_clear_selected_winner(self):
        rec = self.recognizer(ambiguous=False)
        rec.mean_embs["gamma"] = np.array([-1.0, 0.0])
        rec.sample_embs["gamma"] = normalize_face_samples([np.array([0.0, -1.0])])
        self.assertEqual(rec.classify(self.image())[0], "alpha")

    def test_missing_selected_identity_references_abstains(self):
        rec = self.recognizer(ambiguous=False)
        del rec.sample_embs["alpha"]
        self.assertEqual(rec.classify(self.image()), ("unknown", 0.0))

    def test_config_rejects_out_of_range_margins(self):
        for margin in [-0.01, 2.01]:
            with self.assertRaises(ValidationError):
                FaceRecognitionConfig(min_sample_margin=margin)

    def processor(self, recognizer):
        proc = FaceRealTimeProcessor.__new__(FaceRealTimeProcessor)
        proc.config = SimpleNamespace(
            cameras={
                "sample": SimpleNamespace(
                    face_recognition=SimpleNamespace(enabled=True, min_area=5000)
                )
            }
        )
        proc.face_config = recognizer.config.face_recognition
        proc.metrics = SimpleNamespace(face_rec_fps=SimpleNamespace(value=0))
        proc.faces_per_second = Mock()
        proc.faces_per_second.eps.return_value = 0
        proc.person_face_history = {}
        proc.camera_current_people = {}
        proc.requires_face_detection = False
        proc.recognizer = recognizer
        proc.requestor = Mock()
        proc.sub_label_publisher = Mock()
        proc.write_face_attempt = Mock()
        proc._FaceRealTimeProcessor__update_metrics = Mock()
        return proc

    def test_ambiguous_attempt_is_retained_but_does_not_publish_a_person_label(self):
        proc = self.processor(self.recognizer())
        proc.process_frame(
            {
                "id": "event",
                "camera": "sample",
                "label": "person",
                "current_attributes": [{"label": "face", "box": [0, 0, 100, 100]}],
            },
            np.full((192, 128), 128, dtype=np.uint8),
        )
        self.assertEqual(proc.person_face_history["event"], [("unknown", 0.0, 10000)])
        self.assertEqual(proc.write_face_attempt.call_args.args[3:], ("unknown", 0.0))
        proc.sub_label_publisher.publish.assert_not_called()
        proc.requestor.send_data.assert_called_once()

    def test_separated_attempt_still_publishes_the_recognized_label(self):
        proc = self.processor(self.recognizer(ambiguous=False))
        proc.process_frame(
            {
                "id": "event",
                "camera": "sample",
                "label": "person",
                "current_attributes": [{"label": "face", "box": [0, 0, 100, 100]}],
            },
            np.full((192, 128), 128, dtype=np.uint8),
        )
        self.assertEqual(proc.sub_label_publisher.publish.call_args.args[0][1], "alpha")


if __name__ == "__main__":
    unittest.main()
