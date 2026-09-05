import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('restore_state', Path(__file__).with_name('restore_state.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def snapshot(quiesced=True, extra=None, bad_hash=False):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'data.db'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE retained(value TEXT)')
            c.execute("INSERT INTO retained VALUES ('owned-state')")
        files = {'data/frigate.db': db.read_bytes(), 'config/.jwt_secret': b'fixture-only-auth', 'media/retained.txt': b'retained-media'}
    if extra:
        files.update(extra)
    manifest = {'schema': 'frigate.private-state-snapshot/v1', 'sqlite_integrity': 'ok', 'writer_quiesced': quiesced,
                'files': [{'path': p, 'size': len(v), 'sha256': '0'*64 if bad_hash else hashlib.sha256(v).hexdigest()} for p, v in files.items()]}
    files['manifest.json'] = json.dumps(manifest).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as archive:
        for p, data in files.items():
            info = tarfile.TarInfo(p); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


class RestoreTests(unittest.TestCase):
    def roots(self, tmp):
        roots = {k: Path(tmp)/k for k in ['config', 'data', 'media']}
        for p in roots.values(): p.mkdir()
        return roots

    def test_exact_private_state_and_database_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self.roots(tmp)
            result = m.restore(snapshot(), roots)
            self.assertEqual(result['file_count'], 3)
            self.assertEqual((roots['config']/'.jwt_secret').read_bytes(), b'fixture-only-auth')
            self.assertEqual((roots['media']/'retained.txt').read_bytes(), b'retained-media')
            with sqlite3.connect(roots['data']/'frigate.db') as db:
                self.assertEqual(db.execute('SELECT value FROM retained').fetchone()[0], 'owned-state')
            self.assertEqual((roots['config']/'.jwt_secret').stat().st_mode & 0o777, 0o600)

    def test_nonquiesced_snapshot_refused_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self.roots(tmp)
            with self.assertRaisesRegex(ValueError, 'quiesced'): m.restore(snapshot(False), roots)
            self.assertTrue(all(not list(p.iterdir()) for p in roots.values()))

    def test_existing_state_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = self.roots(tmp); (roots['config']/'retained').write_bytes(b'original')
            with self.assertRaisesRegex(ValueError, 'empty'): m.restore(snapshot(), roots)
            self.assertEqual((roots['config']/'retained').read_bytes(), b'original')

    def test_tampered_archive_refused(self):
        with self.assertRaisesRegex(ValueError, 'fixity'): m.validate_snapshot(snapshot(bad_hash=True))

    def test_traversal_and_unexpected_paths_refused(self):
        for path in ['../outside', '/absolute', 'config/../outside', 'unexpected/file']:
            with self.subTest(path=path), self.assertRaises(ValueError): m.validate_snapshot(snapshot(extra={path:b'x'}))

    def test_uncheckpointed_wal_refused(self):
        with self.assertRaisesRegex(ValueError, 'consistent SQLite'): m.validate_snapshot(snapshot(extra={'data/frigate.db-wal':b'x'}))


if __name__ == '__main__': unittest.main()
