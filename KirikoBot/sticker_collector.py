from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import requests
from PIL import Image
import imagehash

logger = logging.getLogger(__name__)

import os as _os
_DOCKER_DIR = "/app/stickers"
_LOCAL_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "stickers")
try:
    _os.makedirs(_DOCKER_DIR, exist_ok=True)
    STICKER_DIR = _DOCKER_DIR
except (PermissionError, OSError):
    _os.makedirs(_LOCAL_DIR, exist_ok=True)
    STICKER_DIR = _LOCAL_DIR

MAX_SIZE = 10 * 1024 * 1024  # 10MB
VALID_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# If True (default), only save images with subType=1 (stickers/表情包).
# Set to False to save all images regardless (original behavior).
# NOTE: LLBot may not provide subType in all cases. When subType is missing,
# we default to collecting the image (conservative: collect more, not less).
STICKER_ONLY = True
STICKER_CATEGORIES = {"可爱", "搞笑", "生气", "惊讶", "悲伤", "打招呼", "鼓励", "庆祝", "动物", "动漫", "其他", "未分类"}

# Perceptual hash threshold — images with hamming distance <= this are duplicates
PHASH_THRESHOLD = 8


class StickerCollector:
    """Save group-shared images to stickers dir. Deduplicate by content hash + perceptual hash."""

    def __init__(self, db: Any = None) -> None:
        os.makedirs(STICKER_DIR, exist_ok=True)
        self._hashes: set[str] | None = None  # lazy init (MD5)
        self._phashes: dict[str, str] | None = None  # lazy init (pHash hex → filename)
        self._db = db
        self._executor: Any = None

    def set_executor(self, executor: Any) -> None:
        """Inject thread pool for async auto-categorization."""
        self._executor = executor

    def _build_index(self) -> set[str]:
        """Scan all existing stickers and compute MD5 hashes."""
        hashes: set[str] = set()
        if not os.path.isdir(STICKER_DIR):
            return hashes
        for fname in os.listdir(STICKER_DIR):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                h = self._md5_file(fpath)
                if h:
                    hashes.add(h)
            except Exception:
                logger.debug("sticker_collector._build_index 忽略了异常", exc_info=True)
        logger.debug("Sticker index built: %d hashes", len(hashes))
        return hashes

    def _build_phash_index(self) -> dict[str, str]:
        """Scan all existing stickers and compute perceptual hashes.
        Returns dict of {phash_hex: filename} for the first occurrence of each hash."""
        phashes: dict[str, str] = {}
        if not os.path.isdir(STICKER_DIR):
            return phashes
        for fname in sorted(os.listdir(STICKER_DIR)):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                ph = self._phash_file(fpath)
                if ph and ph not in phashes:
                    phashes[ph] = fname
            except Exception:
                logger.debug("sticker_collector._build_phash_index 忽略了异常", exc_info=True)
        logger.debug("pHash index built: %d unique hashes", len(phashes))
        return phashes

    @property
    def hashes(self) -> set[str]:
        if self._hashes is None:
            self._hashes = self._build_index()
        return self._hashes

    @property
    def phashes(self) -> dict[str, str]:
        if self._phashes is None:
            self._phashes = self._build_phash_index()
        return self._phashes

    @staticmethod
    def _md5_file(filepath: str) -> str | None:
        try:
            h = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    @staticmethod
    def _md5_data(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    @staticmethod
    def _phash_file(filepath: str) -> str | None:
        """Compute perceptual hash (pHash) for an image file.
        Returns hex string of the hash, or None on failure."""
        try:
            img = Image.open(filepath)
            # Convert to RGB if necessary (e.g., RGBA, P mode)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            ph = imagehash.phash(img)
            return str(ph)
        except Exception:
            logger.debug("pHash computation failed for %s", os.path.basename(filepath))
            return None

    @staticmethod
    def _phash_data(data: bytes) -> str | None:
        """Compute perceptual hash from raw image bytes."""
        import io
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            ph = imagehash.phash(img)
            return str(ph)
        except Exception:
            return None

    def _is_duplicate(self, data: bytes) -> bool:
        """Full-file MD5 check against existing stickers."""
        return self._md5_data(data) in self.hashes

    def _is_visual_duplicate(self, data: bytes) -> bool:
        """Check if image data is visually similar to any existing sticker using pHash.
        Returns True if a visually similar sticker already exists."""
        new_ph = self._phash_data(data)
        if not new_ph:
            return False

        try:
            new_hash = imagehash.hex_to_hash(new_ph)
        except Exception:
            return False

        for existing_ph in self.phashes:
            try:
                existing_hash = imagehash.hex_to_hash(existing_ph)
                distance = new_hash - existing_hash
                if distance <= PHASH_THRESHOLD:
                    logger.debug(
                        "Visual duplicate detected: pHash distance=%d (threshold=%d), existing=%s",
                        distance, PHASH_THRESHOLD, self.phashes[existing_ph],
                    )
                    return True
            except Exception:
                logger.debug("sticker_collector._is_visual_duplicate 忽略了异常", exc_info=True)
                continue

        return False

    def find_duplicates(self) -> list[list[dict[str, Any]]]:
        """Scan all stickers and group visually similar ones.
        Returns list of groups, each group containing 2+ similar stickers.
        Each sticker dict has: filename, file_size, phash."""
        all_files = []
        if not os.path.isdir(STICKER_DIR):
            return []

        for fname in os.listdir(STICKER_DIR):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                ph = self._phash_file(fpath)
                size = os.path.getsize(fpath)
                all_files.append({
                    "filename": fname,
                    "filepath": fpath,
                    "file_size": size,
                    "phash": ph,
                })
            except Exception:
                logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)

        if not all_files:
            return []

        # Group by perceptual hash proximity using Union-Find
        parent = {f["filename"]: f["filename"] for f in all_files}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        # Compare all pairs (O(n²), but N is typically < 1000)
        for i in range(len(all_files)):
            phi = all_files[i]["phash"]
            if not phi:
                continue
            try:
                hi = imagehash.hex_to_hash(phi)
            except Exception:
                logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)
                continue
            for j in range(i + 1, len(all_files)):
                phj = all_files[j]["phash"]
                if not phj:
                    continue
                try:
                    hj = imagehash.hex_to_hash(phj)
                except Exception:
                    logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)
                    continue
                if hi - hj <= PHASH_THRESHOLD:
                    union(all_files[i]["filename"], all_files[j]["filename"])

        # Collect groups
        groups_map: dict[str, list[dict[str, Any]]] = {}
        for f in all_files:
            root = find(f["filename"])
            if root not in groups_map:
                groups_map[root] = []
            groups_map[root].append(f)

        # Return groups with 2+ members
        result = [g for g in groups_map.values() if len(g) >= 2]
        result.sort(key=lambda g: -len(g))  # largest groups first

        logger.info("Duplicate scan: %d groups found (%d total stickers)",
                     len(result), len(all_files))
        return result

    def cleanup_duplicates(self, dry_run: bool = True) -> dict[str, Any]:
        """Find and remove duplicate stickers, keeping the best quality one.

        For each group of visually similar stickers, keeps the largest file
        (best quality) and removes rest.

        Returns: {dry_run: bool, groups_cleaned: int, files_removed: int,
                  removed: [filenames], kept: [filenames]}
        """
        groups = self.find_duplicates()
        removed: list[str] = []
        kept: list[str] = []

        for group in groups:
            # Sort by file_size descending — keep the largest
            group.sort(key=lambda x: x["file_size"], reverse=True)
            best = group[0]
            kept.append(best["filename"])

            for dup in group[1:]:
                removed.append(dup["filename"])
                if not dry_run:
                    try:
                        os.remove(dup["filepath"])
                        # Remove from DB if present
                        if self._db:
                            self._db.execute_action(
                                "DELETE FROM stickers WHERE filename = ?",
                                (dup["filename"],),
                            )
                        # Invalidate caches
                        md5 = self._md5_file(dup["filepath"]) if os.path.exists(dup["filepath"]) else None
                        if md5 and md5 in self.hashes:
                            self.hashes.discard(md5)
                        if dup["phash"] and dup["phash"] in self.phashes:
                            del self.phashes[dup["phash"]]
                        logger.info("Removed duplicate: %s (kept %s)", dup["filename"], best["filename"])
                    except Exception:
                        logger.exception("Failed to remove duplicate %s", dup["filename"])

        result = {
            "dry_run": dry_run,
            "groups_cleaned": len(groups),
            "files_removed": len(removed),
            "files_kept": len(kept),
            "kept": kept,
            "removed": removed,
            "total_waste_bytes": sum(
                sum(d["file_size"] for d in group[1:]) for group in groups
            ) if groups else 0,
        }

        logger.info(
            "Cleanup %s: %d groups, %d files removed, %d kept, %d bytes wasted",
            "DRY RUN" if dry_run else "EXECUTED",
            result["groups_cleaned"],
            result["files_removed"],
            result["files_kept"],
            result["total_waste_bytes"],
        )
        return result

    def collect(self, msg_data: dict[str, Any]) -> tuple[int, list[str]]:
        message = msg_data.get("message")
        if not isinstance(message, list):
            return (0, [])

        saved = 0
        saved_filenames: list[str] = []
        group_id = str(msg_data.get("group_id", "") or "")
        user_id = str(msg_data.get("user_id", "") or "")
        for seg in message:
            if seg.get("type") != "image":
                continue
            data = seg.get("data", {})
            # Only collect stickers (subType=1) when STICKER_ONLY is set.
            # If subType is missing (LLBot may not provide it), default to collecting.
            if STICKER_ONLY:
                sub_type = data.get("subType")
                if sub_type is not None and str(sub_type) not in ("", "1"):
                    logger.debug("Skipping non-sticker image (subType=%s)", sub_type)
                    continue
            image_url = data.get("url", "")
            image_file = data.get("file", "")

            fname: str | None = None
            if image_url:
                fname = self._download(image_url)
            elif image_file and os.path.isfile(image_file):
                fname = self._copy_local(image_file)

            if fname:
                saved += 1
                saved_filenames.append(fname)
                # Record in database if available
                if self._db:
                    fpath = os.path.join(STICKER_DIR, fname)
                    try:
                        file_size = os.path.getsize(fpath)
                        file_hash = self._md5_file(fpath) or ""
                        self._db.insert_sticker(fname, file_hash, file_size, group_id, user_id)
                    except Exception:
                        logger.debug("sticker_collector.collect 忽略了异常", exc_info=True)
                # Auto-categorize asynchronously using vision API
                if self._executor and self._db:
                    try:
                        url_for_vision = image_url or os.path.join(STICKER_DIR, fname)
                        self._executor.submit(self._auto_categorize, fname, url_for_vision)
                    except Exception:
                        logger.debug("sticker_collector.collect 忽略了异常", exc_info=True)

        return (saved, saved_filenames)

    def _download(self, url: str) -> str | None:
        try:
            ext = os.path.splitext(url.split("?")[0])[1].lower()
            if ext not in VALID_EXTENSIONS:
                ext = ".png"

            # HEAD check for size
            try:
                head = requests.head(url, timeout=5, allow_redirects=True)
                cl = int(head.headers.get("content-length", 0))
                if cl > MAX_SIZE:
                    logger.info("Skipping large: %s (%d)", url[:60], cl)
                    return None
            except Exception:
                logger.debug("sticker_collector._download 忽略了异常", exc_info=True)

            r = requests.get(url, timeout=15, allow_redirects=True, stream=True)
            r.raise_for_status()

            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(chunk_size=8192):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_SIZE:
                    return None

            data = b"".join(chunks)
            if len(data) < 100:
                return None

            # Deduplicate by full content hash (MD5 — fast, exact match)
            if self._is_duplicate(data):
                logger.debug("Skipping exact duplicate sticker from: %s", url[:60])
                return None

            # Deduplicate by perceptual hash (pHash — visual similarity)
            if self._is_visual_duplicate(data):
                logger.info("Skipping visually similar sticker from: %s", url[:60])
                return None

            full_hash = self._md5_data(data)
            fname = f"sticker_{full_hash[:12]}{ext}"
            fpath = os.path.join(STICKER_DIR, fname)

            # Race condition check: if file already exists, skip
            if os.path.exists(fpath):
                self.hashes.add(full_hash)
                # Also add to pHash index
                ph = self._phash_file(fpath)
                if ph and ph not in self.phashes:
                    self.phashes[ph] = fname
                return fname  # Already exists — still return name for DB

            with open(fpath, "wb") as f:
                f.write(data)

            self.hashes.add(full_hash)
            # Add to pHash index
            ph = self._phash_file(fpath)
            if ph:
                self.phashes[ph] = fname

            logger.info("Collected: %s (%d bytes)", fname, len(data))
            return fname

        except Exception:
            logger.debug("Download failed: %s", url[:60])
            return None

    def _copy_local(self, filepath: str) -> str | None:
        try:
            size = os.path.getsize(filepath)
            if size > MAX_SIZE or size < 100:
                return None
            ext = os.path.splitext(filepath)[1].lower()
            if ext not in VALID_EXTENSIONS:
                return None

            with open(filepath, "rb") as src:
                data = src.read()

            if self._is_duplicate(data):
                logger.debug("Skipping exact duplicate local: %s", os.path.basename(filepath))
                return None

            if self._is_visual_duplicate(data):
                logger.info("Skipping visually similar local: %s", os.path.basename(filepath))
                return None

            full_hash = self._md5_data(data)
            fname = f"sticker_{full_hash[:12]}{ext}"
            fpath = os.path.join(STICKER_DIR, fname)

            if os.path.exists(fpath):
                self.hashes.add(full_hash)
                ph = self._phash_file(fpath)
                if ph and ph not in self.phashes:
                    self.phashes[ph] = fname
                return fname  # Already exists — still return name for DB

            with open(fpath, "wb") as dst:
                dst.write(data)

            self.hashes.add(full_hash)
            ph = self._phash_file(fpath)
            if ph:
                self.phashes[ph] = fname

            logger.info("Collected local: %s", fname)
            return fname

        except Exception:
            logger.debug("Copy failed: %s", filepath)
            return None

    # ── Auto-categorization ─────────────────────────────

    def _auto_categorize(self, fname: str, image_url_or_path: str) -> None:
        """Classify a freshly collected sticker with one vision call.

        Called on the background executor from collect(), so the webhook reply
        is never blocked. Prefers the already-downloaded local file (the source
        URL often expires); falls back to 未分类 when vision is unavailable or
        returns something outside STICKER_CATEGORIES.
        """
        if not self._db:
            return
        local_path = os.path.join(STICKER_DIR, fname)
        target = local_path if os.path.isfile(local_path) else image_url_or_path
        if not target:
            return
        try:
            # Local import: ai_server.vision_analyze_with_category imports
            # STICKER_CATEGORIES from this module, so a top-level import
            # would create a cycle.
            from ai_server import AiServer

            data = AiServer.vision_analyze_with_category(target)
            if not data:
                self._db.update_sticker_category(fname, "未分类", "", "")
                logger.info("Sticker %s left uncategorized (vision unavailable)", fname)
                return

            category = str(data.get("category") or "").strip()
            if category not in STICKER_CATEGORIES:
                category = "其他"
            self._db.update_sticker_category(
                fname,
                category,
                str(data.get("description") or ""),
                str(data.get("emotion") or ""),
            )
            logger.info("Auto-categorized sticker %s → %s", fname, category)
        except Exception:
            logger.debug("Auto-categorize failed for %s", fname, exc_info=True)
