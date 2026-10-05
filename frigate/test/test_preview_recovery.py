"""Preview recovery must preserve frames across restarts and failed copies."""

import filecmp
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frigate.output.preview_recovery import recover_preview_frames


class TestPreviewRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "holdover"
        self.dst = self.root / "cache"
        self.src.mkdir()
        self.dst.mkdir()

    def recover(self):
        recover_preview_frames(str(self.src), str(self.dst))

    def test_existing_destination_receives_flat_frames(self):
        (self.src / "camera-123.jpg").write_bytes(b"frame")
        self.recover()
        self.assertEqual((self.dst / "camera-123.jpg").read_bytes(), b"frame")
        self.assertFalse((self.dst / "holdover").exists())
        self.assertFalse(self.src.exists())
        self.recover()  # A completed recovery is safe to repeat.

    def test_recovers_both_legacy_directory_names(self):
        nested = self.src / "preview_frames" / "preview_restart_cache"
        nested.mkdir(parents=True)
        (nested / "camera-123.jpg").write_bytes(b"nested frame")
        self.recover()
        self.assertEqual((self.dst / "camera-123.jpg").read_bytes(), b"nested frame")
        self.assertFalse(self.src.exists())

    def test_identical_duplicate_is_removed_after_byte_comparison(self):
        for root in (self.src, self.dst):
            (root / "camera-123.jpg").write_bytes(b"same")
        self.recover()
        self.assertEqual((self.dst / "camera-123.jpg").read_bytes(), b"same")
        self.assertFalse(self.src.exists())

    def test_different_same_size_contents_are_preserved(self):
        (self.src / "camera-123.jpg").write_bytes(b"new")
        (self.dst / "camera-123.jpg").write_bytes(b"old")
        self.recover()
        self.assertEqual((self.src / "camera-123.jpg").read_bytes(), b"new")
        self.assertEqual((self.dst / "camera-123.jpg").read_bytes(), b"old")

    def test_failed_copy_keeps_source_and_hides_partial_destination(self):
        frame = self.src / "camera-123.jpg"
        frame.write_bytes(b"complete")

        def fail_copy(source, staging):
            Path(staging).write_bytes(b"partial")
            raise OSError("copy interrupted")

        with patch(
            "frigate.output.preview_recovery.shutil.copy2", side_effect=fail_copy
        ):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(frame.read_bytes(), b"complete")
        self.assertEqual(list(self.dst.iterdir()), [])
        self.recover()
        self.assertEqual((self.dst / frame.name).read_bytes(), b"complete")

    def test_unexpected_directory_and_symlink_are_preserved(self):
        (self.src / "other").mkdir()
        (self.src / "link").symlink_to(self.dst, target_is_directory=True)
        self.recover()
        self.assertTrue((self.src / "other").is_dir())
        self.assertTrue((self.src / "link").is_symlink())

    def test_duplicate_comparison_does_not_reuse_stale_timestamp_cache(self):
        frame = self.src / "camera-123.jpg"
        target = self.dst / frame.name
        frame.write_bytes(b"old")
        target.write_bytes(b"old")
        stamp = frame.stat().st_mtime_ns
        self.assertTrue(filecmp.cmp(frame, target, shallow=False))
        frame.write_bytes(b"new")
        os.utime(frame, ns=(stamp, stamp))
        self.recover()
        self.assertEqual(frame.read_bytes(), b"new")
        self.assertEqual(target.read_bytes(), b"old")

    def test_failed_publish_keeps_source_without_visible_partial_frame(self):
        frame = self.src / "camera-123.jpg"
        frame.write_bytes(b"complete")
        with patch("frigate.output.preview_recovery.os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.recover()
        self.assertEqual(frame.read_bytes(), b"complete")
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_destination_symlink_is_not_followed_or_replaced(self):
        frame = self.src / "camera-123.jpg"
        frame.write_bytes(b"frame")
        outside = self.root / "outside"
        outside.write_bytes(b"frame")
        (self.dst / frame.name).symlink_to(outside)
        self.recover()
        self.assertEqual(frame.read_bytes(), b"frame")
        self.assertTrue((self.dst / frame.name).is_symlink())
