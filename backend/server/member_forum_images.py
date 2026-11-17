"""Optional additive cloud endpoint for images deliberately published in forum.

Copy beside platform_app.py and register create_forum_image_router(auth,
database). Apply forum-image-read-policy.sql with the reviewed migration owner
first. Private files.get retains its existing owner-only SQL and is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac


class ForumImageError(ValueError):
    pass


def published_image_ids(content):
    marker = '\n\u2063fawnsee-images:'
    value = str(content or '')
    start = value.rfind(marker)
    if start < 0 or not value.endswith('\u2063'):
        return ()
    encoded = value[start + len(marker):-1]
    parts = encoded.split(',')
    if not 1 <= len(parts) <= 4 or any(not part.isascii() or not part.isdecimal() or len(part) > 19 for part in parts):
        return ()
    ids = tuple(int(part) for part in parts)
    return ids if all(number > 0 for number in ids) and len(set(ids)) == len(ids) else ()


def read_public_image(database, actor_id, file_id):
    if type(actor_id) is not int or actor_id <= 0 or type(file_id) is not int or not 0 < file_id < 2**63:
        raise ForumImageError('not_found')
    with database.connect() as connection:
        actor = connection.execute('SELECT id FROM users WHERE id=? AND account_status=\'active\'', (actor_id,)).fetchone()
        if actor is None:
            raise ForumImageError('not_found')
        # The file policy also checks owner == publisher. This check remains
        # necessary even on a SQLite fixture with no PostgreSQL RLS.
        refs = connection.execute(
            'SELECT p.author_user_id,p.content FROM forum_posts p WHERE p.status=\'active\' '
            'UNION ALL SELECT c.author_user_id,c.content FROM forum_comments c '
            'JOIN forum_posts p ON p.id=c.post_id WHERE p.status=\'active\''
        ).fetchall()
        owners = {int(row['author_user_id']) for row in refs if file_id in published_image_ids(row['content'])}
        if not owners:
            raise ForumImageError('not_found')
        row = connection.execute(
            'SELECT f.user_id,f.media_type,f.size_bytes,f.sha256,c.content,c.mime_type AS content_mime_type,c.sha256 AS content_sha256 '
            'FROM user_files f JOIN user_file_contents c ON c.file_id=f.id WHERE f.id=?', (file_id,)
        ).fetchone()
        if row is None or int(row['user_id']) not in owners or row['media_type'] not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise ForumImageError('not_found')
        raw = bytes(row['content'])
        if not 0 < len(raw) <= 15 * 1024 * 1024 or len(raw) != row['size_bytes'] or row['content_mime_type'] != row['media_type']:
            raise ForumImageError('not_found')
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, row['sha256']) or not hmac.compare_digest(digest, row['content_sha256']):
            raise ForumImageError('not_found')
    # Never return a private source filename, owner, path or report linkage.
    return {'file_id': file_id, 'media_type': row['media_type'], 'size_bytes': len(raw),
            'sha256': digest, 'content': base64.b64encode(raw).decode('ascii')}


def create_forum_image_router(auth, database):
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
    from .platform_auth import authentication_dependency

    router = APIRouter(prefix='/v1/member', tags=['member'])
    principal = Depends(authentication_dependency(auth))

    @router.get('/forum/images/{file_id}')
    def image(file_id: int, identity=principal):
        try:
            # PlatformDatabase keeps authenticated identity in contextvars via
            # this existing auth context, just as /v1/rpc does.
            with auth.identity(identity):
                result = read_public_image(database, identity.user.id, file_id)
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except ForumImageError:
            raise HTTPException(404, 'not_found') from None

    @router.get('/chat/images/{attachment_id}')
    def chat_image(attachment_id: int, identity=principal):
        try:
            with auth.identity(identity):
                result = read_owned_chat_image(database, identity.user.id, attachment_id)
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except ForumImageError:
            raise HTTPException(404, 'not_found') from None

    return router


def read_owned_chat_image(database, actor_id, attachment_id):
    if type(actor_id) is not int or actor_id <= 0 or type(attachment_id) is not int or not 0 < attachment_id < 2**63:
        raise ForumImageError('not_found')
    with database.connect() as connection:
        row = connection.execute(
            'SELECT a.media_type,a.content,a.sha256 FROM chat_attachments a '
            'JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.id=m.conversation_id '
            'WHERE a.id=? AND c.user_id=?', (attachment_id, actor_id)
        ).fetchone()
    if row is None or row['media_type'] not in {'image/jpeg', 'image/png', 'image/webp'}:
        raise ForumImageError('not_found')
    raw = bytes(row['content'])
    if not 0 < len(raw) <= 15 * 1024 * 1024 or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), row['sha256']):
        raise ForumImageError('not_found')
    return {'attachment_id': attachment_id, 'media_type': row['media_type'], 'size_bytes': len(raw),
            'sha256': row['sha256'], 'content': base64.b64encode(raw).decode('ascii')}
