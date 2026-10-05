"""Recover preview frames without nesting directories or losing conflicting files."""

import filecmp
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)
LEGACY_DIRECTORIES = {"preview_frames", "preview_restart_cache"}


def recover_preview_frames(source: str, destination: str) -> None:
    """Merge frames, retaining source files if recovery cannot safely complete."""
    src, dst = Path(source), Path(destination)
    if not src.exists():
        return
    if src.resolve() == dst.resolve():
        return
    dst.mkdir(parents=True, exist_ok=True)
    # Frames with restored timestamps can reuse comparison-cache signatures.
    filecmp.clear_cache()

    for entry in list(src.iterdir()):
        if entry.is_symlink():
            logger.warning("Preserving unexpected preview-cache symlink: %s", entry)
            continue
        if entry.is_dir():
            if entry.name in LEGACY_DIRECTORIES:
                recover_preview_frames(str(entry), str(dst))
            else:
                logger.warning(
                    "Preserving unexpected preview-cache directory: %s", entry
                )
            continue
        if not entry.is_file():
            continue

        target = dst / entry.name
        if target.exists():
            if (
                not target.is_symlink()
                and target.is_file()
                and filecmp.cmp(entry, target, shallow=False)
            ):
                entry.unlink()
            else:
                logger.warning(
                    "Preserving conflicting preview frame at source: %s", entry
                )
            continue

        # Copy first: interrupted cross-filesystem recovery must leave the source
        # intact and must not expose a partial frame under its real filename.
        fd, staging = tempfile.mkstemp(prefix=".preview-recovery-", dir=dst)
        os.close(fd)
        try:
            shutil.copy2(entry, staging)
            os.replace(staging, target)
            entry.unlink()
        finally:
            Path(staging).unlink(missing_ok=True)

    if not any(src.iterdir()):
        src.rmdir()
