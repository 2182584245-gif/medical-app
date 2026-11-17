"""Cloud-backed experience forum service.

The public desktop only talks to this small RPC surface.  Topic and post
validation lives here so the server and the desktop facade share the same
limits; no arbitrary SQL or user supplied account identifiers cross the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..data.database import timestamp_to_db, utc_now

MAX_TOPIC = 80
MAX_CONTENT = 4_000
MAX_ATTACHMENTS = 4
ATTACHMENT_MARKER = "\n\u2063fawnsee-images:"
PUBLIC_AUTHOR_FALLBACK = "社区成员"
OWN_AUTHOR_FALLBACK = "我"


class ExperienceForumService:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _text(value, limit: int, *, required=True) -> str:
        if not isinstance(value, str):
            raise ValueError("内容格式不正确")
        value = value.strip()
        if required and not value:
            raise ValueError("内容不能为空")
        if len(value) > limit:
            raise ValueError("内容过长")
        return value

    @staticmethod
    def _attachment_ids(value) -> list[int]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
            raise ValueError("图片数量不正确")
        result = []
        for item in value:
            # JSON numbers arrive as ints.  Do not coerce strings, floats or
            # booleans: coercion could make a crafted request attach a file
            # other than the one the user selected.
            if type(item) is not int:
                raise ValueError("图片编号不正确")
            number = item
            if number <= 0 or number in result:
                raise ValueError("图片编号不正确")
            result.append(number)
        return result

    @staticmethod
    def _pack_content(content: str, attachment_ids: list[int]) -> str:
        if not attachment_ids:
            return content
        packed = f"{content}{ATTACHMENT_MARKER}{','.join(str(item) for item in attachment_ids)}\u2063"
        if len(packed) > MAX_CONTENT:
            raise ValueError("内容过长")
        return packed

    @staticmethod
    def _unpack_content(value: str) -> tuple[str, list[dict]]:
        raw = str(value or "")
        marker = raw.rfind(ATTACHMENT_MARKER)
        if marker < 0 or not raw.endswith("\u2063"):
            return raw, []
        encoded = raw[marker + len(ATTACHMENT_MARKER):-1]
        try:
            ids = [int(item) for item in encoded.split(",") if item]
            if not ids or any(item <= 0 for item in ids) or len(ids) > MAX_ATTACHMENTS:
                return raw, []
        except ValueError:
            return raw, []
        return raw[:marker], [{"file_id": item} for item in ids]

    @staticmethod
    def _check_attachments(connection, actor_user_id: int, attachment_ids: list[int]) -> None:
        if not attachment_ids:
            return
        placeholders = ",".join("?" for _ in attachment_ids)
        rows = connection.execute(
            f"SELECT id FROM user_files WHERE user_id = ? AND media_type IN ('image/jpeg','image/png','image/webp') AND id IN ({placeholders})",
            (actor_user_id, *attachment_ids),
        ).fetchall()
        if len(rows) != len(attachment_ids):
            raise ValueError("图片不存在或无权使用")

    def list_topics(self, actor_user_id: int, query: str = "") -> list[dict]:
        query = self._text(query, MAX_TOPIC, required=False)
        with self.database.connect() as connection:
            if query:
                rows = connection.execute(
                    "SELECT id, slug, name FROM forum_topics WHERE name ILIKE ? "
                    "ORDER BY name LIMIT 20", (f"%{query}%",)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, slug, name FROM forum_topics ORDER BY name LIMIT 50"
                ).fetchall()
        return [dict(row) for row in rows]

    def list_posts(self, actor_user_id: int, topic: str, limit: int = 50) -> list[dict]:
        topic = self._text(topic, MAX_TOPIC)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("查询数量不正确")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT p.id, t.name AS topic, p.content, p.created_at, "
                "CASE WHEN p.author_user_id = ? "
                "THEN COALESCE(u.username, ?) "
                "ELSE COALESCE(u.username, ?) END AS author, "
                "(SELECT COUNT(*) FROM forum_likes l WHERE l.post_id = p.id) AS likes, "
                "(SELECT COUNT(*) FROM forum_comments c WHERE c.post_id = p.id) AS comments, "
                "EXISTS(SELECT 1 FROM forum_likes mine WHERE mine.post_id = p.id "
                "AND mine.user_id = ?) AS liked "
                "FROM forum_posts p JOIN forum_topics t ON t.id = p.topic_id "
                "LEFT JOIN users u ON u.id = p.author_user_id "
                "WHERE t.name = ? AND p.status = 'active' "
                "ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
                (actor_user_id, OWN_AUTHOR_FALLBACK, PUBLIC_AUTHOR_FALLBACK, actor_user_id, topic, limit),
            ).fetchall()
        result = []
        with self.database.connect() as connection:
            for row in rows:
                item = dict(row)
                item["content"], item["attachments"] = self._unpack_content(item.get("content", ""))
                result.append(item)
        return result

    def create_post(self, actor_user_id: int, topic: str, content: str, attachment_ids=None) -> dict:
        """Create a text post; image attachments are optional metadata."""
        topic = self._text(topic, MAX_TOPIC)
        attachment_ids = self._attachment_ids(attachment_ids)
        content = self._text(content, MAX_CONTENT)
        stamp = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._check_attachments(connection, actor_user_id, attachment_ids)
            row = connection.execute(
                "SELECT id, name FROM forum_topics WHERE name = ?", (topic,)
            ).fetchone()
            if row is None:
                raise ValueError("话题不存在")
            packed_content = self._pack_content(content, attachment_ids)
            created = connection.execute(
                "INSERT INTO forum_posts(topic_id, author_user_id, content, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?) RETURNING id",
                (row["id"], actor_user_id, packed_content, "active", stamp, stamp),
            ).fetchone()
        return {"id": int(created["id"]), "topic": topic, "content": content, "created_at": stamp, "attachments": [{"file_id": item} for item in attachment_ids]}

    def toggle_like(self, actor_user_id: int, post_id: int) -> dict:
        if type(post_id) is not int or post_id <= 0:
            raise ValueError("帖子编号不正确")
        stamp = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM forum_likes WHERE post_id = ? AND user_id = ?",
                (post_id, actor_user_id),
            ).fetchone()
            if exists:
                connection.execute(
                    "DELETE FROM forum_likes WHERE post_id = ? AND user_id = ?",
                    (post_id, actor_user_id),
                )
                liked = False
            else:
                connection.execute(
                    "INSERT INTO forum_likes(post_id, user_id, created_at) VALUES(?,?,?)",
                    (post_id, actor_user_id, stamp),
                )
                liked = True
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM forum_likes WHERE post_id = ?", (post_id,)
            ).fetchone()
        return {"post_id": post_id, "liked": liked, "likes": int(row["count"])}

    def list_comments(self, actor_user_id: int, post_id: int, limit: int = 100) -> list[dict]:
        if type(post_id) is not int or post_id <= 0:
            raise ValueError("帖子编号不正确")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("查询数量不正确")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT c.id, c.post_id, c.content, c.created_at, "
                "CASE WHEN c.author_user_id = ? "
                "THEN COALESCE(u.username, ?) "
                "ELSE COALESCE(u.username, ?) END AS author "
                "FROM forum_comments c LEFT JOIN users u ON u.id = c.author_user_id "
                "WHERE c.post_id = ? ORDER BY c.created_at ASC, c.id ASC LIMIT ?",
                (actor_user_id, OWN_AUTHOR_FALLBACK, PUBLIC_AUTHOR_FALLBACK, post_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["content"], item["attachments"] = self._unpack_content(item.get("content", ""))
                result.append(item)
        return result

    def add_comment(self, actor_user_id: int, post_id: int, content: str, attachment_ids=None) -> dict:
        if type(post_id) is not int or post_id <= 0:
            raise ValueError("帖子编号不正确")
        attachment_ids = self._attachment_ids(attachment_ids)
        # A comment may consist of an image alone; text remains bounded when
        # it is present.  The post form still requires text at the UI layer.
        content = self._text(content, MAX_CONTENT, required=not attachment_ids)
        stamp = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._check_attachments(connection, actor_user_id, attachment_ids)
            packed_content = self._pack_content(content, attachment_ids)
            row = connection.execute(
                "INSERT INTO forum_comments(post_id, author_user_id, content, created_at) "
                "VALUES(?,?,?,?) RETURNING id",
                (post_id, actor_user_id, packed_content, stamp),
            ).fetchone()
        return {"id": int(row["id"]), "post_id": post_id, "content": content, "created_at": stamp, "attachments": [{"file_id": item} for item in attachment_ids]}

