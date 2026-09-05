import os
import time
from pathlib import Path

from media_cleanup import cleanup_media_cache


def test_cleanup_removes_expired_files_only(tmp_path: Path):
    old = tmp_path / "old.jpg"
    fresh = tmp_path / "fresh.jpg"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))

    removed, freed = cleanup_media_cache(
        tmp_path, retention_days=1, max_bytes=0
    )
    assert removed == 1
    assert freed == 3
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_enforces_size_limit_oldest_first(tmp_path: Path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"12345")
    second.write_bytes(b"67890")
    now = time.time()
    os.utime(first, (now - 2, now - 2))
    os.utime(second, (now - 1, now - 1))

    removed, freed = cleanup_media_cache(
        tmp_path, retention_days=30, max_bytes=5
    )
    assert removed == 1
    assert freed == 5
    assert not first.exists()
    assert second.exists()


def test_cleanup_continues_with_files_found_before_walk_error(tmp_path: Path, monkeypatch):
    cached = tmp_path / "cached.bin"
    cached.write_bytes(b"123")

    def interrupted_walk(*_args, **_kwargs):
        yield str(tmp_path), [], [cached.name]
        raise OSError("目录在扫描中消失")

    monkeypatch.setattr("media_cleanup.os.walk", interrupted_walk)

    removed, freed = cleanup_media_cache(tmp_path, retention_days=0, max_bytes=0)

    assert (removed, freed) == (1, 3)
    assert not cached.exists()
