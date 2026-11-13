"""StickerCollector：去重、落盘、清理与自动分类。"""
from __future__ import annotations

import io
import os

import pytest
from PIL import Image

import sticker_collector
from sticker_collector import StickerCollector


def _png(color=(200, 40, 40), size=(64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _striped_png(size=(64, 64)) -> bytes:
    img = Image.new("RGB", size, (0, 0, 0))
    px = img.load()
    for x in range(size[0]):
        if x % 2 == 0:
            for y in range(size[1]):
                px[x, y] = (255, 255, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeDB:
    def __init__(self):
        self.stickers = []
        self.categories = []
        self.actions = []

    def insert_sticker(self, fname, file_hash, file_size, group_id, user_id):
        self.stickers.append((fname, file_hash, file_size, group_id, user_id))

    def update_sticker_category(self, fname, category, description, emotion):
        self.categories.append((fname, category, description, emotion))

    def execute_action(self, sql, params):
        self.actions.append((sql, params))


class FakeExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


@pytest.fixture()
def sticker_dir(tmp_path, monkeypatch):
    path = tmp_path / "stickers"
    path.mkdir()
    monkeypatch.setattr(sticker_collector, "STICKER_DIR", str(path))
    return path


@pytest.fixture()
def collector(sticker_dir):
    return StickerCollector()


class FakeHead:
    def __init__(self, content_length):
        self.headers = {"content-length": str(content_length)}


class FakeGet:
    def __init__(self, chunks, status_error=None):
        self.chunks = chunks
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def iter_content(self, chunk_size):
        return iter(self.chunks)


# ── 基础工具 ─────────────────────────────────────────────────────

def test_md5_helpers(tmp_path, collector):
    path = tmp_path / "x.bin"
    path.write_bytes(b"hello")
    assert collector._md5_data(b"hello") == collector._md5_file(str(path))
    assert collector._md5_file(str(tmp_path / "missing")) is None


def test_phash_helpers(sticker_dir, collector, tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(_png())
    assert collector._phash_file(str(path)) is not None
    assert collector._phash_data(_png()) is not None
    assert collector._phash_file(str(tmp_path / "bad.png")) is None
    assert collector._phash_data(b"not an image") is None


def test_indexes_are_lazy_and_cached(sticker_dir, collector):
    (sticker_dir / "a.png").write_bytes(_png())
    assert len(collector.hashes) == 1
    assert collector.hashes is collector.hashes
    assert len(collector.phashes) == 1
    assert collector.phashes is collector.phashes


def test_visual_duplicate_detection(sticker_dir, collector):
    data = _png()
    assert collector._is_visual_duplicate(data) is False

    (sticker_dir / "same.png").write_bytes(data)
    collector._phashes = None
    assert collector._is_visual_duplicate(data) is True


def test_set_executor(collector):
    executor = FakeExecutor()
    collector.set_executor(executor)
    assert collector._executor is executor


# ── find_duplicates / cleanup ────────────────────────────────────

def test_find_duplicates_groups_identical_images(sticker_dir, collector):
    data = _png()
    for name in ("a.png", "b.png", "c.png"):
        (sticker_dir / name).write_bytes(data)
    (sticker_dir / "different.png").write_bytes(_striped_png())

    groups = collector.find_duplicates()

    assert groups
    assert max(len(g) for g in groups) >= 3
    assert all(len(g) >= 2 for g in groups)


def test_find_duplicates_empty_or_missing(sticker_dir, collector, monkeypatch):
    assert collector.find_duplicates() == []
    monkeypatch.setattr(sticker_collector, "STICKER_DIR", str(sticker_dir / "nope"))
    assert collector.find_duplicates() == []


def test_cleanup_duplicates_dry_run_keeps_files(sticker_dir, collector):
    data = _png()
    (sticker_dir / "small.png").write_bytes(data)
    (sticker_dir / "big.png").write_bytes(data + b"\x00" * 200)

    result = collector.cleanup_duplicates(dry_run=True)

    assert result["dry_run"] is True
    if result["groups_cleaned"]:
        assert result["files_removed"] >= 1
    assert (sticker_dir / "small.png").exists()


def test_cleanup_duplicates_executes_and_updates_db(sticker_dir):
    data = _png()
    (sticker_dir / "small.png").write_bytes(data)
    (sticker_dir / "big.png").write_bytes(data + b"\x00" * 200)
    db = FakeDB()
    collector = StickerCollector(db=db)

    result = collector.cleanup_duplicates(dry_run=False)

    assert result["files_removed"] >= 1
    assert not (sticker_dir / "small.png").exists()
    assert (sticker_dir / "big.png").exists()
    assert db.actions and "DELETE FROM stickers" in db.actions[0][0]


# ── collect ──────────────────────────────────────────────────────

def test_collect_ignores_non_list(collector):
    assert collector.collect({}) == (0, [])
    assert collector.collect({"message": "nope"}) == (0, [])


def test_collect_skips_non_image_and_non_sticker(collector, monkeypatch):
    monkeypatch.setattr(collector, "_download", lambda url: "should-not-be-called.png")
    saved, names = collector.collect({"message": [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "image", "data": {"subType": 2, "url": "http://x/a.png"}},
    ]})
    assert (saved, names) == (0, [])


def test_collect_downloads_and_records(sticker_dir, monkeypatch):
    db = FakeDB()
    executor = FakeExecutor()
    collector = StickerCollector(db=db)
    collector.set_executor(executor)
    monkeypatch.setattr(collector, "_download", lambda url: "sticker_abc.png")
    (sticker_dir / "sticker_abc.png").write_bytes(_png())

    saved, names = collector.collect({"group_id": 2, "user_id": 3, "message": [
        {"type": "image", "data": {"url": "http://x/a.png"}},
    ]})

    assert (saved, names) == (1, ["sticker_abc.png"])
    assert db.stickers and db.stickers[0][0] == "sticker_abc.png"
    assert executor.submitted and executor.submitted[0][0] == collector._auto_categorize


def test_collect_uses_local_file_when_no_url(collector, monkeypatch, tmp_path):
    local = tmp_path / "local.png"
    local.write_bytes(_png())
    monkeypatch.setattr(collector, "_copy_local", lambda p: "sticker_local.png")

    saved, names = collector.collect({"message": [
        {"type": "image", "data": {"file": str(local)}},
    ]})
    assert (saved, names) == (1, ["sticker_local.png"])


# ── _download ────────────────────────────────────────────────────

def test_download_rejects_large_by_head(sticker_dir, collector, monkeypatch):
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(20 * 1024 * 1024))
    assert collector._download("http://x/a.png") is None


def test_download_rejects_oversize_stream(sticker_dir, collector, monkeypatch):
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(100))
    monkeypatch.setattr(
        sticker_collector.requests, "get",
        lambda *a, **k: FakeGet([b"x" * 600, b"y" * 600]),
    )
    monkeypatch.setattr(sticker_collector, "MAX_SIZE", 1000)
    assert collector._download("http://x/a.png") is None


def test_download_rejects_tiny_payload(sticker_dir, collector, monkeypatch):
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(10))
    monkeypatch.setattr(sticker_collector.requests, "get", lambda *a, **k: FakeGet([b"tiny"]))
    assert collector._download("http://x/a.png") is None


def test_download_skips_exact_duplicate(sticker_dir, collector, monkeypatch):
    data = _png()
    (sticker_dir / "existing.png").write_bytes(data)
    collector._hashes = None
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(len(data)))
    monkeypatch.setattr(sticker_collector.requests, "get", lambda *a, **k: FakeGet([data]))
    assert collector._download("http://x/a.png") is None


def test_download_saves_new_sticker(sticker_dir, collector, monkeypatch):
    data = _png(color=(10, 200, 10))
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(len(data)))
    monkeypatch.setattr(sticker_collector.requests, "get", lambda *a, **k: FakeGet([data]))

    name = collector._download("http://x/pic.png?token=1")

    assert name and name.endswith(".png")
    assert os.path.isfile(sticker_dir / name)
    assert collector._md5_data(data) in collector.hashes


def test_download_returns_existing_name_on_race(sticker_dir, collector, monkeypatch):
    data = _png(color=(1, 2, 3))
    full_hash = collector._md5_data(data)
    name = f"sticker_{full_hash[:12]}.png"
    (sticker_dir / name).write_bytes(data)
    collector._hashes = set()  # 让去重检查先放过
    collector._phashes = {}
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: FakeHead(len(data)))
    monkeypatch.setattr(sticker_collector.requests, "get", lambda *a, **k: FakeGet([data]))

    assert collector._download("http://x/pic.png") == name


def test_download_swallows_errors(sticker_dir, collector, monkeypatch):
    monkeypatch.setattr(sticker_collector.requests, "head", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sticker_collector.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert collector._download("http://x/a.png") is None


# ── _copy_local ──────────────────────────────────────────────────

def test_copy_local_rejects_bad_size_and_extension(sticker_dir, collector, tmp_path, monkeypatch):
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(b"x")
    assert collector._copy_local(str(tiny)) is None

    huge = tmp_path / "huge.png"
    huge.write_bytes(b"x" * 200)
    monkeypatch.setattr(sticker_collector, "MAX_SIZE", 100)
    assert collector._copy_local(str(huge)) is None

    bad_ext = tmp_path / "file.txt"
    bad_ext.write_bytes(b"x" * 200)
    assert collector._copy_local(str(bad_ext)) is None


def test_copy_local_saves_sticker(sticker_dir, collector, tmp_path):
    path = tmp_path / "local.png"
    path.write_bytes(_png(color=(5, 5, 200)))

    name = collector._copy_local(str(path))

    assert name and name.startswith("sticker_") and name.endswith(".png")
    assert os.path.isfile(sticker_dir / name)


def test_copy_local_skips_duplicate(sticker_dir, collector, tmp_path):
    data = _png(color=(9, 9, 9))
    path = tmp_path / "local.png"
    path.write_bytes(data)
    (sticker_dir / "existing.png").write_bytes(data)
    collector._hashes = None

    assert collector._copy_local(str(path)) is None


def test_copy_local_returns_existing_name(sticker_dir, collector, tmp_path):
    data = _png(color=(11, 12, 13))
    path = tmp_path / "local.png"
    path.write_bytes(data)
    name = f"sticker_{collector._md5_data(data)[:12]}.png"
    (sticker_dir / name).write_bytes(data)
    collector._hashes = set()
    collector._phashes = {}

    assert collector._copy_local(str(path)) == name


def test_copy_local_swallows_errors(sticker_dir, collector):
    assert collector._copy_local("/definitely/missing.png") is None


# ── _auto_categorize ─────────────────────────────────────────────

def test_auto_categorize_without_db_is_noop(sticker_dir, collector, monkeypatch):
    calls = []
    monkeypatch.setattr("ai_server.AiServer.vision_analyze_with_category", staticmethod(lambda p: calls.append(p)))
    collector._auto_categorize("x.png", "http://x")
    assert calls == []


def test_auto_categorize_updates_db(sticker_dir, monkeypatch):
    db = FakeDB()
    collector = StickerCollector(db=db)
    monkeypatch.setattr(
        "ai_server.AiServer.vision_analyze_with_category",
        staticmethod(lambda p: {"description": "d", "emotion": "e", "category": "可爱"}),
    )

    collector._auto_categorize("x.png", "http://x")

    assert db.categories == [("x.png", "可爱", "d", "e")]


def test_auto_categorize_marks_uncategorized_without_vision(sticker_dir, monkeypatch):
    db = FakeDB()
    collector = StickerCollector(db=db)
    monkeypatch.setattr("ai_server.AiServer.vision_analyze_with_category", staticmethod(lambda p: None))

    collector._auto_categorize("x.png", "http://x")

    assert db.categories[0][:2] == ("x.png", "未分类")


def test_auto_categorize_falls_back_to_other_category(sticker_dir, monkeypatch):
    db = FakeDB()
    collector = StickerCollector(db=db)
    monkeypatch.setattr(
        "ai_server.AiServer.vision_analyze_with_category",
        staticmethod(lambda p: {"description": "", "emotion": "", "category": "不存在"}),
    )

    collector._auto_categorize("x.png", "http://x")

    assert db.categories[0][1] == "其他"


def test_auto_categorize_swallows_exceptions(sticker_dir, monkeypatch):
    db = FakeDB()
    collector = StickerCollector(db=db)
    monkeypatch.setattr(
        "ai_server.AiServer.vision_analyze_with_category",
        staticmethod(lambda p: (_ for _ in ()).throw(RuntimeError("boom"))),
    )

    collector._auto_categorize("x.png", "http://x")  # 不抛
    assert db.categories == []
