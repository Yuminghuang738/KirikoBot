from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from llbot_client import MessageBuilder

logger = logging.getLogger(__name__)


class VersionManager:
    """Manage app versions, changelog entries, and group notifications."""

    def __init__(self, db: Any, llbot: Any) -> None:
        self.db = db
        self.llbot = llbot

    # ── Current version ─────────────────────────────────

    def get_current_version(self) -> dict[str, Any] | None:
        """Return the latest version record (or None if no versions exist)."""
        rows = self.db.fetch_data(
            "SELECT id, version, release_date, description, author, digest_sent, created_at "
            "FROM app_versions ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r[0], "version": r[1], "release_date": r[2],
            "description": r[3], "author": r[4], "digest_sent": bool(r[5]),
            "created_at": r[6],
        }

    def read_version_file(self) -> str:
        """Read current version from the VERSION file."""
        try:
            with open("VERSION", "r") as f:
                return f.read().strip()
        except Exception:
            return "0.0.0"

    def write_version_file(self, version: str) -> None:
        """Write version string to the VERSION file."""
        try:
            with open("VERSION", "w") as f:
                f.write(version + "\n")
        except Exception:
            logger.exception("Failed to write VERSION file")

    # ── Version CRUD ────────────────────────────────────

    def create_version(
        self, version: str, description: str = "", author: str = "developer",
        notify: bool = True,
    ) -> dict[str, Any]:
        """Create a new version record. Writes VERSION file and notifies groups."""
        release_date = datetime.now().strftime("%Y-%m-%d")

        self.db.deposit(
            "app_versions",
            "(version, release_date, description, author)",
            "(?, ?, ?, ?)",
            (version, release_date, description, author),
        )

        # Get the newly created version ID
        rows = self.db.fetch_data(
            "SELECT id FROM app_versions WHERE version = ? ORDER BY id DESC LIMIT 1",
            (version,),
        )
        version_id = rows[0][0] if rows else 0

        # Write to VERSION file
        self.write_version_file(version)

        logger.info("Created version %s (id=%d) by %s", version, version_id, author)

        result = {
            "id": version_id, "version": version,
            "release_date": release_date, "description": description,
            "author": author,
        }

        # Notify groups
        if notify:
            self.notify_version_release(result)

        return result

    def get_all_versions(self) -> list[dict[str, Any]]:
        """List all versions with changelog entry counts."""
        rows = self.db.fetch_data(
            "SELECT v.id, v.version, v.release_date, v.description, v.author, v.created_at, "
            "COUNT(c.id) as changelog_count "
            "FROM app_versions v LEFT JOIN changelog c ON v.id = c.version_id "
            "GROUP BY v.id ORDER BY v.id DESC"
        )
        return [
            {
                "id": r[0], "version": r[1], "release_date": r[2],
                "description": r[3], "author": r[4], "created_at": r[5],
                "changelog_count": r[6],
            }
            for r in rows
        ]

    def get_version_detail(self, version_id: int) -> dict[str, Any] | None:
        """Get a single version with all its changelog entries."""
        rows = self.db.fetch_data(
            "SELECT id, version, release_date, description, author, created_at "
            "FROM app_versions WHERE id = ?", (version_id,)
        )
        if not rows:
            return None
        r = rows[0]
        changelogs = self.get_changelogs(version_id=version_id)
        return {
            "id": r[0], "version": r[1], "release_date": r[2],
            "description": r[3], "author": r[4], "created_at": r[5],
            "changelogs": changelogs,
        }

    # ── Changelog CRUD ──────────────────────────────────

    def add_changelog(
        self, version_id: int, entry_type: str, title: str,
        description: str = "", author: str = "developer",
    ) -> dict[str, Any]:
        """Add a changelog entry for a given version."""
        valid_types = {"feature", "fix", "improve", "breaking"}
        if entry_type not in valid_types:
            entry_type = "feature"

        self.db.deposit(
            "changelog",
            "(version_id, entry_type, title, description, author)",
            "(?, ?, ?, ?, ?)",
            (version_id, entry_type, title, description, author),
        )

        # Get the new ID
        rows = self.db.fetch_data(
            "SELECT id, created_at FROM changelog ORDER BY id DESC LIMIT 1"
        )
        new_id = rows[0][0] if rows else 0
        created_at = rows[0][1] if rows else ""

        logger.info("Added changelog entry '%s' [%s] for version_id=%d", title, entry_type, version_id)

        return {
            "id": new_id, "version_id": version_id, "entry_type": entry_type,
            "title": title, "description": description, "author": author,
            "created_at": created_at,
        }

    def get_changelogs(
        self, version_id: int | None = None, entry_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get changelog entries, optionally filtered by version_id and/or entry_type."""
        sql = (
            "SELECT c.id, c.version_id, c.entry_type, c.title, c.description, "
            "c.author, c.created_at, v.version "
            "FROM changelog c JOIN app_versions v ON c.version_id = v.id "
        )
        conditions: list[str] = []
        params: list[Any] = []

        if version_id is not None:
            conditions.append("c.version_id = ?")
            params.append(version_id)
        if entry_type is not None:
            conditions.append("c.entry_type = ?")
            params.append(entry_type)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY c.id DESC LIMIT ?"
        params.append(limit)

        rows = self.db.fetch_data(sql, tuple(params))
        return [
            {
                "id": r[0], "version_id": r[1], "entry_type": r[2],
                "title": r[3], "description": r[4], "author": r[5],
                "created_at": r[6], "version": r[7],
            }
            for r in rows
        ]

    def auto_changelog_for_feature(
        self, feature_request: str, feature_summary: str, user_name: str,
    ) -> None:
        """Automatically add a changelog entry when a feature request is completed.
        Appends to the latest version if one exists, and notifies all QQ groups."""
        current = self.get_current_version()
        if not current:
            return
        try:
            entry = self.add_changelog(
                version_id=current["id"],
                entry_type="feature",
                title=feature_summary or feature_request[:30],
                description=f"来自 {user_name} 的需求：{feature_request[:200]}",
                author=user_name,
            )
            # Notify all active QQ groups about the new feature
            if entry:
                self.notify_changelog_entry(entry)
        except Exception:
            logger.exception("Failed to auto-add changelog for feature completion")

    # ── Version bump ────────────────────────────────────

    def bump_version(self, bump_type: str = "patch") -> str:
        """Increment version number. bump_type: major, minor, or patch."""
        current = self.read_version_file()
        try:
            parts = [int(x) for x in current.split(".")]
            while len(parts) < 3:
                parts.append(0)
        except (ValueError, TypeError):
            parts = [0, 0, 0]

        if bump_type == "major":
            parts[0] += 1
            parts[1] = 0
            parts[2] = 0
        elif bump_type == "minor":
            parts[1] += 1
            parts[2] = 0
        else:  # patch
            parts[2] += 1

        new_version = ".".join(str(p) for p in parts)
        return new_version

    # ── Group notification ──────────────────────────────

    def _get_active_group_ids(self) -> list[str]:
        """Get all distinct group IDs from recorded messages."""
        try:
            rows = self.db.fetch_data(
                "SELECT DISTINCT group_id FROM group_messages WHERE group_id IS NOT NULL"
            )
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def notify_version_release(self, version_info: dict[str, Any]) -> None:
        """Send version release notification to all active QQ groups."""
        groups = self._get_active_group_ids()
        if not groups:
            logger.info("No active groups to notify for version %s", version_info.get("version"))
            return

        version = version_info.get("version", "?")
        release_date = version_info.get("release_date", "")
        description = version_info.get("description", "")
        version_id = version_info.get("id", 0)

        # Get changelogs for this version
        changelogs = self.get_changelogs(version_id=version_id)

        # Build message
        lines = [
            f"📦 KirikoBot 更新啦！",
            f"",
            f"版本：v{version}  |  日期：{release_date}",
        ]
        if description:
            lines.append(f"更新说明：{description}")

        if changelogs:
            # Group by type
            groups_by_type: dict[str, list[dict[str, Any]]] = {}
            for c in changelogs:
                groups_by_type.setdefault(c["entry_type"], []).append(c)

            type_emoji = {
                "feature": ("🎉 新功能", "+"),
                "fix": ("🔧 修复", "-"),
                "improve": ("💡 改进", "*"),
                "breaking": ("⚠️ 重大变更", "!"),
            }

            for entry_type in ("feature", "improve", "fix", "breaking"):
                entries = groups_by_type.get(entry_type, [])
                if not entries:
                    continue
                emoji_label, bullet = type_emoji.get(entry_type, ("📌", "•"))
                lines.append(f"")
                lines.append(f"{emoji_label}：")
                for e in entries:
                    title = e["title"]
                    desc = e.get("description", "")
                    # Clean description
                    if desc.startswith("来自 "):
                        parts = desc.split("的需求：", 1)
                        if len(parts) == 2:
                            desc = parts[1].strip()
                    if desc:
                        lines.append(f"  {bullet} {title} — {desc[:80]}")
                    else:
                        lines.append(f"  {bullet} {title}")

        lines.append(f"")
        lines.append(f"感谢使用 KirikoBot！(◕‿◕✿)")

        message = "\n".join(lines)
        success_count = 0

        for gid in groups:
            try:
                builder = MessageBuilder()
                builder.text(message)
                self.llbot.send_group_msg(gid, builder.build())
                success_count += 1
                logger.info("Version notification sent to group %s", gid)
            except Exception:
                logger.exception("Failed to send version notification to group %s", gid)

        logger.info(
            "Version %s notification sent to %d/%d groups",
            version, success_count, len(groups),
        )

    def _build_changelog_message(self, entry: dict[str, Any], version: str) -> str:
        """Build a clean, AI-summary-style notification message from a changelog entry."""
        entry_type = entry.get("entry_type", "feature")
        emoji_map = {
            "feature": ("🎉 新功能上线", "✨"),
            "fix": ("🔧 问题修复", "🛠️"),
            "improve": ("💡 功能改进", "📈"),
            "breaking": ("⚠️ 重要变更", "📢"),
        }
        label, icon = emoji_map.get(entry_type, ("📌 更新", "•"))

        title = entry.get("title", "未知更新")
        description = entry.get("description", "")

        lines = [
            f"{label}：{title}",
        ]

        if description:
            # Clean up the description — remove raw "来自 XXX 的需求：" prefix
            desc = description
            # If it's a feature request style, make it more natural
            if desc.startswith("来自 "):
                # Extract just the feature description
                parts = desc.split("的需求：", 1)
                if len(parts) == 2:
                    desc = f"群友建议：{parts[1].strip()}"
            lines.append(f"")
            lines.append(f"{desc[:200]}")

        lines.append(f"")
        lines.append(f"📦 版本：v{version}")
        lines.append(f"感谢大家对 KirikoBot 的支持！{icon}")

        return "\n".join(lines)

    def notify_changelog_entry(self, entry: dict[str, Any]) -> None:
        """Send a single changelog entry notification to all active QQ groups."""
        groups = self._get_active_group_ids()
        if not groups:
            logger.info("No active groups to notify for changelog entry '%s'", entry.get("title"))
            return

        # Get the version string
        version = ""
        try:
            rows = self.db.fetch_data(
                "SELECT version FROM app_versions WHERE id = ?", (entry.get("version_id", 0),)
            )
            if rows:
                version = rows[0][0]
        except Exception:
            logger.debug("version_manager.notify_changelog_entry 忽略了异常", exc_info=True)

        message = self._build_changelog_message(entry, version or "?")

        success_count = 0
        for gid in groups:
            try:
                builder = MessageBuilder()
                builder.text(message)
                self.llbot.send_group_msg(gid, builder.build())
                success_count += 1
                logger.info("Changelog notification sent to group %s", gid)
            except Exception:
                logger.exception("Failed to send changelog notification to group %s", gid)

        logger.info(
            "Changelog notification '%s' sent to %d/%d groups",
            entry.get("title"), success_count, len(groups),
        )

    # ── Seed initial version ────────────────────────────

    def seed_initial_version(self) -> None:
        """Create an initial version if no versions exist in the database."""
        existing = self.get_current_version()
        if existing:
            logger.info("Database already has version %s, skipping seed", existing["version"])
            # Sync VERSION file
            self.write_version_file(existing["version"])
            return

        version = self.read_version_file()
        if version == "0.0.0":
            version = "1.0.0"
            self.write_version_file(version)

        release_date = datetime.now().strftime("%Y-%m-%d")
        self.db.deposit(
            "app_versions",
            "(version, release_date, description, author)",
            "(?, ?, ?, ?)",
            (version, release_date, "KirikoBot 初始版本", "developer"),
        )
        logger.info("Seeded initial version %s", version)
