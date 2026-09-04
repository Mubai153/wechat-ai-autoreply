from __future__ import annotations

import time
from pathlib import Path


def cleanup_media_cache(
    root: Path,
    *,
    retention_days: int,
    max_bytes: int,
) -> tuple[int, int]:
    """删除图片缓存中的过期文件，并按最旧优先控制总大小。

    只处理 root 目录下的普通文件，不触碰聊天数据库、画像或微信原始缓存。
    返回 (删除文件数, 释放字节数)。
    """
    if not root.exists() or not root.is_dir():
        return 0, 0
    files = [p for p in root.rglob("*") if p.is_file()]
    now = time.time()
    cutoff = now - max(0, retention_days) * 86400
    removed = 0
    freed = 0
    survivors: list[tuple[Path, int, float]] = []
    for path in files:
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                path.unlink()
                removed += 1
                freed += size
            else:
                survivors.append((path, size, mtime))
        except OSError:
            continue

    if max_bytes > 0:
        total = sum(size for _, size, _ in survivors)
        for path, size, _ in sorted(survivors, key=lambda item: item[2]):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
                freed += size
            except OSError:
                continue

    # 只移除缓存目录内部的空子目录，不会移除 root 本身。
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed, freed
