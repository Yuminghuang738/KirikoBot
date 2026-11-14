"""点歌发送链路：卡片失败要回退、音频要交给 llbot 能访问的地址。

线上事故（2026-11-14）：
1. 网易云卡片签名失败，但代码照样写了「Music shared」成功日志；
2. 音频落 /tmp，llbot 在另一个容器看不见，报「未知文件类型或路径不存在」；
3. 反爬 HTML 错误页被当成 mp3。
这里把三条都钉住：卡片结果被检查、record 目标不是容器私有路径、失败走文字兜底。
"""
from __future__ import annotations

import json
import logging

from ai_tools import MusicTool
from config import Config

SONG = {
    "id": 123456,
    "name": "测试歌",
    "artist": "测试歌手",
    "album": "测试专辑",
    "audio_url": "https://music.163.com/song/media/outer/url?id=123456.mp3",
    "music_type": "163",
}


class FakeRobot:
    def __init__(self, send_results=None):
        self.sent: list[list[dict]] = []
        self.msg_type = "group"
        self.group_id = "100"
        self.user_id = "200"
        self.msg = "点首歌"
        self._results = list(send_results or [])

    def send(self, message):
        self.sent.append(message)
        return self._results.pop(0) if self._results else True

    def reply(self, text):
        self.sent.append([{"type": "text", "data": {"text": text}}])
        return True

    def texts(self) -> list[str]:
        out = []
        for message in self.sent:
            for seg in message:
                if seg["type"] == "text":
                    out.append(seg["data"]["text"])
        return out

    def records(self) -> list[str]:
        return [seg["data"]["file"] for m in self.sent for seg in m if seg["type"] == "record"]

    def has_music_card(self) -> bool:
        return any(seg["type"] == "music" for m in self.sent for seg in m)


class FakeAI:
    def __init__(self):
        self.ai_message = {
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "music_search", "arguments": json.dumps({"keyword": "测试"})},
            }]
        }
        self.user_text = ""


class FakeMusicService:
    def __init__(self, song=None, audio=None):
        self.song = song
        self.audio = audio

    def search_best(self, keyword):
        return self.song

    def search(self, keyword, limit=5):
        return [self.song] if self.song else []

    def download_audio(self, url, song_id):
        return self.audio


def _tool(service):
    return MusicTool(service, msg_package=None)


def _run(robot, service):
    ai = FakeAI()
    _tool(service).music_search_call(robot, ai)
    return ai


def test_card_and_audio_success_has_no_fallback():
    robot = FakeRobot()
    service = FakeMusicService(SONG, "/app/shared_files/music_123456_abcd1234.mp3")
    ai = _run(robot, service)

    assert robot.has_music_card(), "歌曲卡片应该发出"
    assert robot.records() == [
        f"{Config.MUSIC_AUDIO_BASE_URL}/download/music_123456_abcd1234.mp3"
    ], "音频必须用 llbot 能访问的 URL，不能再传容器私有路径"
    assert not any("点这里听" in t for t in robot.texts()), "成功时不该出现兜底文案"
    assert ai.user_text == "播放歌曲: 测试歌 - 测试歌手"


def test_card_failure_falls_back_to_text_link(caplog):
    """卡片签名失败时 robot.send 返回 False，必须回退文字直链并记 warning。"""
    # ① 信息 ② 卡片(失败) ③ 音频 ④ 兜底文字
    robot = FakeRobot(send_results=[True, False, True, True])
    service = FakeMusicService(SONG, "/app/shared_files/music_123456_abcd1234.mp3")
    with caplog.at_level(logging.WARNING, logger="ai_tools"):
        _run(robot, service)

    texts = robot.texts()
    assert any("https://music.163.com/song?id=123456" in t for t in texts), "卡片失败要给可点的歌曲页"
    assert any("音乐卡片发送失败" in r.message for r in caplog.records)


def test_audio_send_failure_falls_back_to_download_link(monkeypatch):
    """URL 和共享目录路径都发不出去时，至少把直链写出来，并记 warning。"""
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://kb.example.com")
    # ① 信息 ② 卡片 ③ 音频 URL ④ 音频路径 ⑤ 兜底文字
    robot = FakeRobot(send_results=[True, True, False, False, True])
    service = FakeMusicService(SONG, "/app/shared_files/music_123456_abcd1234.mp3")
    ai = _run(robot, service)

    assert len(robot.records()) == 2, "两种引用方式都要试过"
    assert any(
        "https://kb.example.com/download/music_123456_abcd1234.mp3" in t
        for t in robot.texts()
    )
    assert ai.user_text == "播放歌曲: 测试歌 - 测试歌手"


def test_no_audio_leaves_no_record_and_no_link():
    robot = FakeRobot()
    service = FakeMusicService(SONG, None)
    _run(robot, service)

    assert robot.records() == []
    assert not any("音频直链" in t for t in robot.texts())


def test_info_send_failure_is_warned(caplog):
    robot = FakeRobot(send_results=[False])
    service = FakeMusicService(SONG, None)
    with caplog.at_level(logging.WARNING, logger="ai_tools"):
        _run(robot, service)

    assert any("歌曲信息发送失败" in r.message for r in caplog.records)
