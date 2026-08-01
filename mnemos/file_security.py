"""Small, cross-platform helpers for private and atomic local files."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def secure_directory(path: str | Path, *, force: bool = False) -> Path:
    """Create a directory and make it private when Mnemos owns its mode."""

    directory = Path(path).expanduser()
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" and (force or not existed):
        directory.chmod(PRIVATE_DIR_MODE)
    return directory


def secure_file(path: str | Path) -> Path:
    """Restrict an existing file to its owner on POSIX systems."""

    file_path = Path(path).expanduser()
    if os.name != "nt" and file_path.exists():
        file_path.chmod(PRIVATE_FILE_MODE)
    return file_path


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    backup_existing: bool = False,
) -> Path:
    """Write a private text file and replace the destination atomically."""

    destination = Path(path).expanduser()
    parent = secure_directory(destination.parent)
    if backup_existing and destination.exists():
        backup = destination.with_suffix(destination.suffix + ".bak")
        shutil.copy2(destination, backup)
        secure_file(backup)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        secure_file(destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination


def atomic_write_bytes(
    path: str | Path, content: bytes, *, backup_existing: bool = False
) -> Path:
    """Write private bytes atomically, with an optional recoverable backup."""
    destination = Path(path).expanduser()
    parent = secure_directory(destination.parent)
    if backup_existing and destination.exists():
        backup = destination.with_suffix(destination.suffix + ".bak")
        shutil.copy2(destination, backup)
        secure_file(backup)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        secure_file(destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination
