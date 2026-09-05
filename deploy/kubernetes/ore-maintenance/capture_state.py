"""Capture private state only while the exact guarded writers remain stopped."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import time

import process_pause
from restore_state import MAX_ARCHIVE_BYTES, MAX_BYTES, validate_snapshot


def verify_quiescence(envelope):
    remaining = envelope['deadline'] - time.time()
    if not 10 <= remaining <= 120:
        raise ValueError('snapshot outside independent guard window')
    intent = envelope['writers']
    if process_pause.inspect() != intent:
        raise ValueError('snapshot writer identity changed')
    for expected in intent['processes']:
        actual = process_pause.process(expected['pid'])
        if not process_pause.same(expected, actual) or actual['state'] not in ('T', 't'):
            raise ValueError('snapshot writer is not stopped')


def capture(envelope, roots):
    verify_quiescence(envelope)
    output = io.BytesIO()
    total = 0
    records = []
    with tempfile.TemporaryDirectory(prefix='frigate-quiesced-') as temporary:
        backup = Path(temporary) / 'frigate.db'
        source = sqlite3.connect(f"file:{roots['data'] / 'frigate.db'}?mode=ro", uri=True)
        target = sqlite3.connect(backup)
        try:
            source.backup(target, pages=128,
                          progress=lambda *_: verify_quiescence(envelope))
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('snapshot SQLite integrity failed')
        finally:
            target.close()
            source.close()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for label, root in roots.items():
                for path in sorted(root.rglob('*')):
                    verify_quiescence(envelope)
                    if path.is_symlink():
                        raise ValueError('snapshot symlink rejected')
                    if path.is_dir():
                        continue
                    if not path.is_file():
                        raise ValueError('snapshot nonregular file rejected')
                    relative = path.relative_to(root).as_posix()
                    if label == 'data' and relative in ('frigate.db-wal', 'frigate.db-shm'):
                        continue
                    actual = backup if label == 'data' and relative == 'frigate.db' else path
                    if total + actual.stat().st_size > MAX_BYTES or len(records) >= 1024:
                        raise ValueError('snapshot expanded inventory exceeds bound')
                    raw = actual.read_bytes()
                    total += len(raw)
                    name = label + '/' + relative
                    records.append({'path': name, 'size': len(raw),
                                    'sha256': hashlib.sha256(raw).hexdigest()})
                    member = tarfile.TarInfo(name)
                    member.size = len(raw)
                    member.mode = 0o600
                    archive.addfile(member, io.BytesIO(raw))
                    if output.tell() > MAX_ARCHIVE_BYTES:
                        raise ValueError('snapshot exceeds private Secret bound')
            manifest = json.dumps({'schema': 'frigate.private-state-snapshot/v1',
                                   'sqlite_integrity': 'ok', 'writer_quiesced': True,
                                   'files': records}, sort_keys=True).encode()
            member = tarfile.TarInfo('manifest.json')
            member.size = len(manifest)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(manifest))
    raw = output.getvalue()
    validate_snapshot(raw)
    verify_quiescence(envelope)
    return raw


if __name__ == '__main__':
    envelope = json.load(sys.stdin)
    result = capture(envelope, {'config': Path('/config'), 'data': Path('/data'),
                                'media': Path('/media/frigate')})
    # Binary private output must be captured directly into operator-only custody.
    sys.stdout.buffer.write(result)
