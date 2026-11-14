from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 真实音频的文件头：ID3 标签，或 MP3 帧同步（0xFF 后跟 111xxxxx 位）。
# 以「Content-Type」或文件名判断都不可靠 —— 网易云/百度反爬经常把 HTML 错误页
# 以 application/octet-stream 下发，存成 .mp3 后 LLBot 只会报「未知文件类型」。
_AUDIO_SNIFF_BYTES = 16

# 反爬有时会返回一个巨大的页面/视频；shared_files 在 1.6G 的机器上不能随便撑。
_MAX_AUDIO_BYTES = 30 * 1024 * 1024


def _is_mp3_header(head: bytes) -> bool:
    """文件头是不是真 MP3：ID3 标签或帧同步 0xFFFB/0xFFF3 等。"""
    if not head:
        return False
    if head.startswith(b"ID3"):
        return True
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _shared_files_dir() -> str:
    """与 llbot 容器共享的目录（compose 里两边都挂到 /app/shared_files）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_files")


class MusicService:
    """Search songs via Netease Cloud Music API. Returns song info for OneBot music share cards.

    Primary delivery: OneBot 'music' type share card (native QQ music share UI).
    Audio download is attempted but may fail due to Netease anti-hotlinking.
    """

    SEARCH_API = "https://music.163.com/api/search/get"
    TIMEOUT = 15

    # Headers to mimic a browser (required by Netease)
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://music.163.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    def search(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search songs by keyword via Netease API. Returns list of song info dicts."""
        # Build params with encoded keyword
        params = {
            "s": keyword,
            "type": 1,      # 1 = song
            "limit": limit,
            "offset": 0,
        }

        try:
            r = requests.get(
                self.SEARCH_API,
                params=params,
                timeout=self.TIMEOUT,
                headers=self.HEADERS,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            logger.exception("Music search API failed for keyword '%s'", keyword)
            return []

        songs = self._parse_results(data)
        logger.info("Music search for '%s': found %d results", keyword, len(songs))
        return songs

    def _parse_results(self, data: Any) -> list[dict[str, Any]]:
        """Parse Netease search response into normalized song dicts."""
        results: list[dict[str, Any]] = []

        try:
            raw_songs = data.get("result", {}).get("songs", [])
        except Exception:
            return results

        for item in raw_songs:
            if not isinstance(item, dict):
                continue

            song_id = item.get("id", 0)

            # Artists
            artists = []
            for a in item.get("artists", []) or item.get("ar", []):
                if isinstance(a, dict):
                    artists.append(a.get("name", ""))

            # Album
            album = item.get("album", {}) or item.get("al", {}) or {}
            album_name = album.get("name", "") if isinstance(album, dict) else ""

            # Cover image
            cover = ""
            if isinstance(album, dict):
                pic_id = album.get("picId", 0)
                if pic_id:
                    cover = f"https://p2.music.126.net/{pic_id}.jpg"

            # Duration
            duration_ms = item.get("duration", 0)
            duration_sec = duration_ms // 1000 if duration_ms else 0

            # Audio URL — try to construct from Netease (may redirect to HTML)
            audio_url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

            results.append({
                "id": song_id,
                "name": item.get("name", "未知歌曲"),
                "artist": " / ".join(artists) if artists else "未知歌手",
                "artists": artists,
                "album": album_name,
                "cover": cover,
                "audio_url": audio_url,
                "duration": duration_sec,
                # OneBot music share card uses this
                "music_type": "163",  # Netease Cloud Music
            })

        return results

    def search_best(self, keyword: str) -> dict[str, Any] | None:
        """Search and return the best matching song."""
        songs = self.search(keyword, limit=3)
        if not songs:
            return None

        # Prefer results with known artists
        for song in songs:
            if song.get("artist") and song["artist"] != "未知歌手":
                return song

        return songs[0]

    def _extract_song_id(self, text: str) -> str | None:
        """Try to extract a Netease song ID from a URL or text."""
        # Match patterns like: id=123456, song?id=123456, /song/123456
        patterns = [
            r"id[=/](\d+)",
            r"/song/(\d+)",
            r"/(\d{5,12})",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m and len(m.group(1)) >= 6:
                return m.group(1)
        return None

    def download_audio(
        self,
        audio_url: str,
        song_id: int,
        dest_dir: str | None = None,
    ) -> str | None:
        """下载音频到 llbot 可见的 shared_files，并校验它真的是 MP3。

        Netease anti-hotlinking often blocks this — it's a best-effort attempt.
        返回落盘路径（失败返回 None）；落盘位置必须能让 llbot 容器读到，
        所以不再写 /tmp（那是 kirikobot 容器私有的，llbot 看不到）。
        """
        if not audio_url:
            return None

        dest = dest_dir or _shared_files_dir()
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError:
            logger.warning("音频缓存目录不可用，跳过下载: %s", dest)
            return None

        fname = f"music_{song_id}_{uuid.uuid4().hex[:8]}.mp3"
        fpath = os.path.join(dest, fname)
        head = b""

        try:
            r = requests.get(
                audio_url,
                timeout=30,
                headers=self.HEADERS,
                stream=True,
                allow_redirects=True,
            )
            r.raise_for_status()

            written = 0
            too_big = False
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_AUDIO_BYTES:
                        too_big = True
                        break
                    if len(head) < _AUDIO_SNIFF_BYTES:
                        head += chunk[:_AUDIO_SNIFF_BYTES]
                    f.write(chunk)

            if too_big:
                self._discard(fpath)
                logger.warning(
                    "音频超过 %.0f MB 上限，已丢弃 song_id=%s",
                    _MAX_AUDIO_BYTES / 1024 / 1024, song_id,
                )
                return None

            # 文件头不是 MP3 就一律丢弃（HTML 错误页、空响应、其它格式）。
            if not _is_mp3_header(head):
                self._discard(fpath)
                logger.warning(
                    "下载内容不是音频（文件头 %r，Content-Type=%r），已丢弃 song_id=%s",
                    head[:8], r.headers.get("Content-Type", ""), song_id,
                )
                return None

            size_kb = os.path.getsize(fpath) / 1024
            if size_kb < 10:
                self._discard(fpath)
                logger.warning("音频文件太小（%.1f KB），已丢弃 song_id=%s", size_kb, song_id)
                return None

            logger.info("Downloaded audio: %s (%.1f KB)", fpath, size_kb)
            return fpath
        except Exception:
            self._discard(fpath)
            logger.warning("Failed to download audio for song_id=%s", song_id, exc_info=True)
            return None

    @staticmethod
    def _discard(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.warning("清理无效音频失败: %s", path)
