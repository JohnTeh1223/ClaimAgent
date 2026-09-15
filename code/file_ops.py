"""Copy local/cloud-synced files without publishing incomplete destinations."""
from __future__ import annotations

import errno
import os
import shutil
import tempfile
from pathlib import Path


def copy_file(source: Path, target: Path) -> None:
    source, target = Path(source), Path(target)
    fd, temporary = tempfile.mkstemp(prefix=".claim-copy-", dir=target.parent)
    os.close(fd)
    staged = Path(temporary)
    try:
        try:
            shutil.copy2(source, staged)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            # Some cloud filesystem providers reject optimized copying or
            # metadata operations. Retry ordinary reads/writes; never suppress
            # data I/O failures. Timestamps are optional for this fallback.
            with source.open("rb") as reader, staged.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
        if source.stat().st_size != staged.stat().st_size:
            raise OSError(f"复制前后大小不一致：{source} -> {target}")
        os.replace(staged, target)
    except OSError as exc:
        raise OSError(exc.errno, f"复制文件失败：{source} -> {target}；{exc}") from exc
    finally:
        staged.unlink(missing_ok=True)
