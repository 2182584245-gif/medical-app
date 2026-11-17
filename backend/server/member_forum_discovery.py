"""Authenticated, read-only discovery across every visible community post.

This endpoint adds pagination without changing the legacy topic RPC or granting
access to private profiles, files, hidden posts or new database tables.
"""
from __future__ import annotations

from starlette.requests import Request


SLOW_SUGAR_TERMS = (
    "慢糖", "控糖", "低糖", "少糖", "减糖", "无糖", "血糖", "升糖",
    "糖友", "糖尿病", "糖化", "胰岛素", "低gi",
)
IMAGE_MARKER = "\n\u2063fawnsee-images:"


def discovery_posts(database, actor_id, *, query="", topic="", section="", limit=24, offset=0):
    if type(actor_id) is not int or actor_id <= 0:
        raise ValueError("invalid_actor")
    if not isinstance(query, str) or len(query) > 200 or "\x00" in query:
        raise ValueError("invalid_query")
    if not isinstance(topic, str) or len(topic) > 80 or "\x00" in topic:
        raise ValueError("invalid_topic")
    if section not in ("", "slow-sugar") or not isinstance(section, str):
        raise ValueError("invalid_section")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid_limit")
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError("invalid_offset")
    query, topic = query.strip().lower(), topic.strip()
    # instr is supported by SQLite and compiled to strpos by PlatformDatabase.
    # Strip attachment transport metadata before searching visible post text.
    visible = (
        "SELECT p.id,p.author_user_id,t.name AS topic,p.content,p.created_at,"
        "COALESCE(u.username,?) AS author,"
        "CASE WHEN instr(p.content,?)>0 THEN substr(p.content,1,instr(p.content,?)-1) "
        "ELSE p.content END AS searchable_content "
        "FROM forum_posts p JOIN forum_topics t ON t.id=p.topic_id "
        "LEFT JOIN users u ON u.id=p.author_user_id WHERE p.status='active'"
    )
    parameters = ["社区成员", IMAGE_MARKER, IMAGE_MARKER]
    clauses = []
    if query:
        clauses.append("(instr(lower(p.searchable_content),?)>0 OR instr(lower(p.topic),?)>0 OR instr(lower(p.author),?)>0)")
        parameters.extend((query, query, query))
    if topic:
        clauses.append("p.topic=?")
        parameters.append(topic)
    if section:
        clauses.append("(" + " OR ".join(
            "(instr(lower(p.searchable_content),?)>0 OR instr(lower(p.topic),?)>0)"
            for _ in SLOW_SUGAR_TERMS
        ) + ")")
        for term in SLOW_SUGAR_TERMS:
            parameters.extend((term, term))
    source = " FROM (" + visible + ") p" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    with database.connect() as connection:
        actor = connection.execute(
            "SELECT id FROM users WHERE id=? AND role_code='member' AND account_status='active'", (actor_id,)
        ).fetchone()
        if actor is None:
            raise PermissionError("member_required")
        rows = connection.execute(
            "SELECT p.id,p.author_user_id,p.author,p.topic,p.content,p.created_at,"
            "(SELECT COUNT(*) FROM forum_likes l WHERE l.post_id=p.id) AS likes,"
            "(SELECT COUNT(*) FROM forum_comments c WHERE c.post_id=p.id) AS comments,"
            "EXISTS(SELECT 1 FROM forum_likes mine WHERE mine.post_id=p.id AND mine.user_id=?) AS liked,"
            "COUNT(*) OVER() AS discovery_total" + source +
            " ORDER BY p.created_at DESC,p.id DESC LIMIT ? OFFSET ?",
            (actor_id, *parameters, limit, offset),
        ).fetchall()
        # An empty page still reports the matching total. Non-empty pages use
        # one SQL statement/snapshot for their records and exact result count.
        total = int(rows[0]["discovery_total"]) if rows else int(connection.execute(
            "SELECT COUNT(*) AS total" + source, parameters,
        ).fetchone()["total"])
    from ollama_chat_app.services.experience_forum import ExperienceForumService
    items = []
    for row in rows:
        item = dict(row)
        item.pop("discovery_total")
        item["content"], item["attachments"] = ExperienceForumService._unpack_content(item["content"])
        item["liked"] = bool(item["liked"])
        item["likes"], item["comments"] = int(item["likes"]), int(item["comments"])
        if hasattr(item["created_at"], "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        items.append(item)
    end = offset + len(items)
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "next_offset": end if items and end < total else None}


def create_discovery_router(auth, database):
    from fastapi import APIRouter, Depends, HTTPException, Query
    from fastapi.responses import JSONResponse
    from .platform_auth import authentication_dependency

    router = APIRouter(prefix="/v1/member", tags=["member"])
    principal = Depends(authentication_dependency(auth))

    @router.get("/forum/discovery")
    def discovery(request: Request, query: str = Query("", max_length=200),
                  topic: str = Query("", max_length=80), section: str = Query(""),
                  limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000),
                  identity=principal):
        if identity.user.role_code != "member":
            raise HTTPException(403, "member_required")
        allowed = {"query", "topic", "section", "limit", "offset"}
        if set(request.query_params) - allowed or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise HTTPException(422, "invalid_discovery_query")
        try:
            with auth.identity(identity):
                result = discovery_posts(database, identity.user.id, query=query, topic=topic,
                                         section=section, limit=limit, offset=offset)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except PermissionError:
            raise HTTPException(403, "member_required") from None
        except ValueError:
            raise HTTPException(422, "invalid_discovery_query") from None

    return router
