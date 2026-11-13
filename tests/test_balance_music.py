"""BalanceService / MusicService 的响应解析、错误分支与下载兜底。"""
from __future__ import annotations

import os

import pytest
import requests

import music_service
from balance_service import BalanceService
from config import Config
from music_service import MusicService


class FakeResponse:
    def __init__(self, data=None, *, ok=True, status_code=200, headers=None, content=b"", chunks=None):
        self.data = data
        self.ok = ok
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self._chunks = chunks or []
        self.raw = FakeRaw(content)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(response=self)

    def json(self):
        return self.data

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def close(self):
        pass


class FakeRaw:
    def __init__(self, content):
        self._content = content

    def read(self, size=-1):
        return self._content[:size] if size else self._content


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.response


# ── BalanceService ───────────────────────────────────────────────

def _use_balance_session(monkeypatch, session):
    monkeypatch.setattr(BalanceService, "_create_session", staticmethod(lambda: session))


def test_balance_session_has_retries():
    session = BalanceService._create_session()
    adapter = session.get_adapter("https://api.deepseek.com")
    assert adapter.max_retries.total == 2
    assert "GET" in adapter.max_retries.allowed_methods


def test_balance_success(monkeypatch):
    data = {"balance_infos": [{"currency": "CNY", "total_balance": "12.5"}]}
    _use_balance_session(monkeypatch, FakeSession(FakeResponse(data)))

    result = BalanceService().get_balance()

    assert result == {"ok": True, "data": data}


def test_balance_timeout(monkeypatch):
    _use_balance_session(monkeypatch, FakeSession(exc=requests.exceptions.Timeout()))
    assert BalanceService().get_balance()["error"] == "查询超时，请稍后再试"


@pytest.mark.parametrize("status,message", [
    (401, "API密钥无效"),
    (403, "无权限访问"),
    (500, "HTTP 500"),
])
def test_balance_http_errors(monkeypatch, status, message):
    _use_balance_session(monkeypatch, FakeSession(FakeResponse(ok=False, status_code=status)))
    assert BalanceService().get_balance() == {"ok": False, "error": message}


def test_balance_generic_error(monkeypatch):
    _use_balance_session(monkeypatch, FakeSession(exc=RuntimeError("boom")))
    assert BalanceService().get_balance()["error"] == "查询失败，网络或服务异常"


def test_format_balance_failure(monkeypatch):
    monkeypatch.setattr(BalanceService, "get_balance", lambda self: {"ok": False, "error": "超时"})
    assert "超时" in BalanceService().format_balance()


def test_format_balance_without_infos(monkeypatch):
    monkeypatch.setattr(BalanceService, "get_balance", lambda self: {"ok": True, "data": {}})
    assert "暂无数据" in BalanceService().format_balance()


def test_format_balance_renders_numbers(monkeypatch):
    data = {"balance_infos": [{
        "currency": "CNY", "total_balance": "10.0",
        "granted_balance": "1.0", "topped_up_balance": "9.0",
    }]}
    monkeypatch.setattr(BalanceService, "get_balance", lambda self: {"ok": True, "data": data})

    text = BalanceService().format_balance()

    assert "10.0 CNY" in text and "9.0 CNY" in text and "1.0 CNY" in text


def test_format_balance_uses_defaults(monkeypatch):
    monkeypatch.setattr(BalanceService, "get_balance", lambda self: {"ok": True, "data": {"balance_infos": [{}]}})
    text = BalanceService().format_balance()
    assert "未知 CNY" in text


# ── MusicService ─────────────────────────────────────────────────

NETEASE_STANDARD = {
    "result": {
        "songs": [
            {
                "id": 123,
                "name": "晴天",
                "artists": [{"name": "周杰伦"}],
                "album": {"name": "叶惠美", "picId": 999},
                "duration": 269000,
            },
            "not-a-dict",
        ]
    }
}

NETEASE_NEW_SHAPE = {
    "result": {
        "songs": [
            {"id": 456, "name": "稻香", "ar": [{"name": "周杰伦"}, {"name": "方文山"}],
             "al": {"name": "魔杰座"}, "duration": 0},
        ]
    }
}


def test_parse_results_standard_shape():
    songs = MusicService()._parse_results(NETEASE_STANDARD)

    assert len(songs) == 1
    song = songs[0]
    assert song["id"] == 123 and song["name"] == "晴天"
    assert song["artist"] == "周杰伦"
    assert song["album"] == "叶惠美"
    assert song["cover"].endswith("/999.jpg")
    assert song["duration"] == 269
    assert song["audio_url"].endswith("id=123.mp3")
    assert song["music_type"] == "163"


def test_parse_results_new_shape_and_unknowns():
    songs = MusicService()._parse_results(NETEASE_NEW_SHAPE)

    assert songs[0]["artist"] == "周杰伦 / 方文山"
    assert songs[0]["cover"] == ""
    assert songs[0]["duration"] == 0


def test_parse_results_handles_garbage():
    service = MusicService()
    assert service._parse_results(None) == []
    assert service._parse_results({}) == []
    assert service._parse_results({"result": {"songs": [{}]}})[0]["name"] == "未知歌曲"


def test_search_success(monkeypatch):
    monkeypatch.setattr(music_service.requests, "get", lambda *a, **k: FakeResponse(NETEASE_STANDARD))
    songs = MusicService().search("晴天", limit=3)
    assert [s["name"] for s in songs] == ["晴天"]


def test_search_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(
        music_service.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down")),
    )
    assert MusicService().search("晴天") == []


def test_search_best_prefers_known_artist(monkeypatch):
    service = MusicService()
    monkeypatch.setattr(service, "search", lambda keyword, limit=3: [
        {"name": "a", "artist": "未知歌手"},
        {"name": "b", "artist": "周杰伦"},
    ])
    assert service.search_best("x")["name"] == "b"


def test_search_best_falls_back_to_first_and_empty(monkeypatch):
    service = MusicService()
    monkeypatch.setattr(service, "search", lambda keyword, limit=3: [{"name": "only", "artist": "未知歌手"}])
    assert service.search_best("x")["name"] == "only"

    monkeypatch.setattr(service, "search", lambda keyword, limit=3: [])
    assert service.search_best("x") is None


@pytest.mark.parametrize("text,expected", [
    ("https://music.163.com/song?id=123456789", "123456789"),
    ("https://music.163.com/song/987654321", "987654321"),
    ("https://music.163.com/#/song?id=112233", "112233"),
    ("id=12345", None),
    ("没有数字", None),
])
def test_extract_song_id(text, expected):
    assert MusicService()._extract_song_id(text) == expected


def test_download_audio_rejects_html(monkeypatch):
    resp = FakeResponse(headers={"Content-Type": "text/html"}, content=b"<!DOCTYPE html>")
    monkeypatch.setattr(music_service.requests, "get", lambda *a, **k: resp)

    assert MusicService().download_audio("http://x/a.mp3", 1) is None


def test_download_audio_rejects_tiny_file(monkeypatch, tmp_path):
    resp = FakeResponse(headers={"Content-Type": "audio/mpeg"}, chunks=[b"x" * 100])
    monkeypatch.setattr(music_service.requests, "get", lambda *a, **k: resp)

    assert MusicService().download_audio("http://x/a.mp3", 2) is None


def test_download_audio_success(monkeypatch):
    resp = FakeResponse(headers={"Content-Type": "audio/mpeg"}, chunks=[b"a" * 1024] * 20)
    monkeypatch.setattr(music_service.requests, "get", lambda *a, **k: resp)

    path = MusicService().download_audio("http://x/a.mp3", 3)

    assert path and os.path.isfile(path)
    assert os.path.getsize(path) >= 10 * 1024
    os.remove(path)


def test_download_audio_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        music_service.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert MusicService().download_audio("http://x/a.mp3", 4) is None
