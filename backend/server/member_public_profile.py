"""Read-only public forum identity, resolved from a visible published post.

Never exposes personal health profile, preferences, contact details or private
files. All queries retain the existing authenticated member and RLS context.
"""
from __future__ import annotations


def read_public_profile(database, actor_id, post_id):
    if type(actor_id) is not int or actor_id <= 0 or type(post_id) is not int or not 0 < post_id < 2**63:
        raise ValueError('not_found')
    from ollama_chat_app.services.experience_forum import ExperienceForumService
    with database.connect() as connection:
        actor = connection.execute("SELECT id FROM users WHERE id=? AND role_code='member' AND account_status='active'", (actor_id,)).fetchone()
        if actor is None:
            raise ValueError('not_found')
        member = connection.execute("SELECT p.author_user_id,u.username FROM forum_posts p LEFT JOIN users u ON u.id=p.author_user_id WHERE p.id=? AND p.status='active'", (post_id,)).fetchone()
        if member is None:
            raise ValueError('not_found')
        owner = int(member['author_user_id'])
        rows = connection.execute(
            "SELECT p.id,p.author_user_id,t.name AS topic,p.content,p.created_at,"
            "(SELECT COUNT(*) FROM forum_likes l WHERE l.post_id=p.id) AS likes,"
            "(SELECT COUNT(*) FROM forum_comments c WHERE c.post_id=p.id) AS comments,"
            "EXISTS(SELECT 1 FROM forum_likes l WHERE l.post_id=p.id AND l.user_id=?) AS liked "
            "FROM forum_posts p JOIN forum_topics t ON t.id=p.topic_id WHERE p.author_user_id=? AND p.status='active' "
            "ORDER BY p.created_at DESC,p.id DESC LIMIT 100", (actor_id, owner)).fetchall()
        counts = connection.execute(
            "SELECT COUNT(*) AS post_count,COALESCE(SUM((SELECT COUNT(*) FROM forum_likes l WHERE l.post_id=p.id)),0) AS like_count "
            "FROM forum_posts p WHERE p.author_user_id=? AND p.status='active'", (owner,)).fetchone()
    posts = []
    for row in rows:
        item = dict(row)
        item['author'] = str(member['username'] or '社区成员')
        item['content'], item['attachments'] = ExperienceForumService._unpack_content(item['content'])
        item['liked'] = bool(item['liked'])
        stamp = item.get('created_at')
        if hasattr(stamp, 'isoformat'):
            item['created_at'] = stamp.isoformat()
        posts.append(item)
    return {'member': {'id': owner, 'display_name': str(member['username'] or '社区成员'), 'avatar_url': None},
            'posts': posts, 'post_count': int(counts['post_count']), 'like_count': int(counts['like_count']),
            'is_self': owner == actor_id}


def create_public_profile_router(auth, database):
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
    from .platform_auth import authentication_dependency
    router = APIRouter(prefix='/v1/member', tags=['member'])
    principal = Depends(authentication_dependency(auth))

    @router.get('/forum/profile/{post_id}')
    def profile(post_id: int, identity=principal):
        if identity.user.role_code != 'member':
            raise HTTPException(403, 'permission')
        try:
            with auth.identity(identity):
                result = read_public_profile(database, identity.user.id, post_id)
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except ValueError:
            raise HTTPException(404, 'not_found') from None
    return router
