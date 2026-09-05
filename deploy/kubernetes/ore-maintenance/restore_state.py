"""Restore an operator-custodied snapshot into fresh maintenance emptyDir volumes."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile

MAX_BYTES = 64 * 1024 * 1024


def validate_snapshot(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("snapshot exceeds bound")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 1025 or sum(m.size for m in members) > MAX_BYTES:
            raise ValueError("expanded snapshot exceeds bound")
        for member in members:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or member.name in files or str(path) != member.name):
                raise ValueError("unsafe snapshot member")
            if member.name != "manifest.json" and (len(path.parts) < 2 or path.parts[0] not in {"config", "data", "media"}):
                raise ValueError("unexpected snapshot path")
            files[member.name] = archive.extractfile(member).read()
    manifest = json.loads(files.pop("manifest.json"))
    if manifest.get("schema") != "frigate.private-state-snapshot/v1" or manifest.get("sqlite_integrity") != "ok":
        raise ValueError("snapshot contract differs")
    records = manifest["files"]
    if len({r["path"] for r in records}) != len(records) or {r["path"] for r in records} != set(files):
        raise ValueError("snapshot inventory differs")
    for record in records:
        data = files[record["path"]]
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError("snapshot fixity differs")
    if "data/frigate.db" not in files or any(p in files for p in ["data/frigate.db-wal", "data/frigate.db-shm"]):
        raise ValueError("consistent SQLite snapshot required")
    return files, manifest


def restore(raw, roots):
    files, manifest = validate_snapshot(raw)
    if manifest.get("writer_quiesced") is not True:
        raise ValueError("final quiesced handoff required")
    if any(any(root.iterdir()) for root in roots.values()):
        raise ValueError("restore destination must be empty")
    with tempfile.TemporaryDirectory(dir=roots["data"]) as tmp:
        db = Path(tmp) / "verify.db"
        db.write_bytes(files["data/frigate.db"])
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("restored SQLite integrity failed")
    for name, data in files.items():
        path = PurePosixPath(name)
        target = roots[path.parts[0]].joinpath(*path.parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            target.chmod(0o600)
            stream.write(data)
    return {"schema": "frigate.private-state-restored/v1", "file_count": len(files),
            "archive_sha256": hashlib.sha256(raw).hexdigest(), "sqlite_integrity": "ok"}


if __name__ == "__main__":
    result = restore(Path("/snapshot/state.tar.gz").read_bytes(),
                     {"config": Path("/config"), "data": Path("/data"), "media": Path("/media/frigate")})
    print(json.dumps(result, sort_keys=True))
