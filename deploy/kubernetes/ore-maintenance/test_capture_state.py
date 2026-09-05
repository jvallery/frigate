from contextlib import closing
import sqlite3
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

import capture_state
from restore_state import restore, validate_snapshot


class CaptureTests(unittest.TestCase):
    def test_real_sqlite_and_all_files_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = {name: Path(temporary) / name for name in ('config', 'data', 'media')}
            for root in roots.values():
                root.mkdir()
            (roots['config'] / 'config.yml').write_bytes(b'private-test-config')
            with closing(sqlite3.connect(roots['data'] / 'frigate.db')) as db:
                db.execute('CREATE TABLE marker (value TEXT)')
                db.execute("INSERT INTO marker VALUES ('retained')")
                db.commit()
            with patch.object(capture_state, 'verify_quiescence') as verify:
                raw = capture_state.capture({}, roots)
                self.assertGreater(verify.call_count, 2)
            files, manifest = validate_snapshot(raw)
            self.assertTrue(manifest['writer_quiesced'])
            self.assertEqual(files['config/config.yml'], b'private-test-config')
            destinations = {name: Path(temporary) / ('new-' + name) for name in roots}
            for root in destinations.values():
                root.mkdir()
            restore(raw, destinations)
            with closing(sqlite3.connect(destinations['data'] / 'frigate.db')) as db:
                self.assertEqual(db.execute('SELECT value FROM marker').fetchone(), ('retained',))

    def test_expired_guard_rejected_before_capture(self):
        with self.assertRaisesRegex(ValueError, 'guard window'):
            capture_state.capture({'deadline': time.time()}, {})

    def test_resumed_writer_rejected(self):
        row = {'pid': 2, 'start_ticks': 10, 'command_sha256': 'a'}
        intent = {'processes': [row]}
        with patch.object(capture_state.process_pause, 'inspect', return_value=intent), \
             patch.object(capture_state.process_pause, 'process', return_value={**row, 'state': 'S'}):
            with self.assertRaisesRegex(ValueError, 'not stopped'):
                capture_state.verify_quiescence({'deadline': time.time() + 60, 'writers': intent})

    def test_final_deadline_change_rejects_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with closing(sqlite3.connect(root / 'frigate.db')):
                pass
            with patch.object(capture_state, 'verify_quiescence', side_effect=[None, ValueError('guard expired')]):
                with self.assertRaisesRegex(ValueError, 'guard expired'):
                    capture_state.capture({}, {'data': root})


if __name__ == '__main__':
    unittest.main()
