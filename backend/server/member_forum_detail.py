"""Member-scoped public discussion detail and idempotent social operations."""
from __future__ import annotations

import base64
import hashlib
import json
import asyncio
import struct
from uuid import UUID

from starlette.requests import Request
from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.experience_forum import ExperienceForumService as Forum


class DetailNotFound(ValueError):
    pass


class DetailConflict(ValueError):
    pass


def positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('invalid_id')
    return value


def avatar(value):
    if not isinstance(value, str) or not 46 <= len(value) <= 350000 or not value.startswith('data:image/png;base64,'):
        return ''
    try:
        raw = base64.b64decode(value[22:], validate=True)
        if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
            return ''
        width, height = struct.unpack('>II', raw[16:24])
        if not 1 <= width <= 1024 or not 1 <= height <= 1024:
            return ''
    except (ValueError, struct.error):
        return ''
    return value


class ForumDetail:
    def __init__(self, database):
        self.db = database

    def _visible(self, connection, actor, post_id):
        positive(actor);positive(post_id)
        if connection.execute("SELECT id FROM users WHERE id=? AND role_code='member' AND account_status='active'", (actor,)).fetchone() is None:
            raise PermissionError('member_required')
        row = connection.execute("SELECT p.id,p.author_user_id,p.content,p.created_at,t.name AS topic "
            "FROM forum_posts p JOIN forum_topics t ON t.id=p.topic_id "
            "WHERE p.id=? AND p.status='active'", (post_id,)).fetchone()
        if row is None:
            raise DetailNotFound('not_found')
        result=dict(row)
        author=self._identity(connection,int(result['author_user_id']))
        if author is None:raise DetailNotFound('not_found')
        result['author']=author['username'];result['avatar_data']=author['avatar_data']
        return result

    def _identity(self, connection, owner):
        if getattr(self.db, 'dialect', 'sqlite') == 'postgresql':
            row = connection.execute('SELECT forum_public_identity(?) AS public_identity', (owner,)).fetchone()
            value=json.loads(row['public_identity']) if row['public_identity'] is not None else None
            if value is None:return None
            return {'username':str(value.get('username') or '社区成员')[:100],'avatar_data':avatar(value.get('avatar_data'))}
        # Disposable SQLite fixtures have no RLS; mirror the SQL function's
        # visible-contributor check without widening preferences elsewhere.
        row = connection.execute("SELECT u.username,pref.preferences_json FROM users u LEFT JOIN user_preferences pref ON u.id=pref.user_id WHERE u.id=? AND u.account_status='active' "
            "AND (EXISTS(SELECT 1 FROM forum_posts p WHERE p.author_user_id=? AND p.status='active') OR "
            "EXISTS(SELECT 1 FROM forum_comments c JOIN forum_posts p ON p.id=c.post_id WHERE c.author_user_id=? AND p.status='active'))", (owner,owner,owner)).fetchone()
        try:
            return {'username':str(row['username'] or '社区成员')[:100],'avatar_data':avatar(json.loads(row['preferences_json'] or '{}').get('avatar_data'))} if row else None
        except (ValueError, TypeError, AttributeError):
            return {'username':'社区成员','avatar_data':''} if row else None

    @staticmethod
    def _counts(connection, actor, post_id):
        row = connection.execute('SELECT '
            '(SELECT COUNT(*) FROM forum_likes WHERE post_id=?) AS likes,'
            '(SELECT COUNT(*) FROM forum_bookmarks WHERE post_id=?) AS saves,'
            '(SELECT COUNT(*) FROM forum_comments WHERE post_id=?) AS comments,'
            'EXISTS(SELECT 1 FROM forum_likes WHERE post_id=? AND user_id=?) AS liked,'
            'EXISTS(SELECT 1 FROM forum_bookmarks WHERE post_id=? AND user_id=?) AS saved',
            (post_id,post_id,post_id,post_id,actor,post_id,actor)).fetchone()
        return {key: bool(row[key]) if key in ('liked','saved') else int(row[key]) for key in row.keys()}

    def get(self, actor, post_id):
        with self.db.connect() as connection:
            row = self._visible(connection,actor,post_id)
            row.update(self._counts(connection,actor,post_id))
            row['is_self'] = actor == row['author_user_id']
            row['following'] = bool(connection.execute('SELECT 1 FROM forum_follows WHERE user_id=? AND author_user_id=?', (actor,row['author_user_id'])).fetchone())
        row['content'],row['attachments'] = Forum._unpack_content(row['content'])
        return row

    def comments(self, actor, post_id, *, limit=30, offset=0):
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('invalid_range')
        with self.db.connect() as connection:
            self._visible(connection,actor,post_id)
            rows = connection.execute("SELECT c.id,c.post_id,c.author_user_id,c.content,c.created_at,COUNT(*) OVER() AS total "
                "FROM forum_comments c WHERE c.post_id=? ORDER BY c.created_at ASC,c.id ASC LIMIT ? OFFSET ?", (post_id,limit,offset)).fetchall()
            total = int(rows[0]['total']) if rows else int(connection.execute('SELECT COUNT(*) AS total FROM forum_comments WHERE post_id=?', (post_id,)).fetchone()['total'])
            avatars = {}
            result = []
            for raw in rows:
                item = dict(raw);item.pop('total')
                owner = int(item['author_user_id'])
                if owner not in avatars:
                    avatars[owner] = self._identity(connection,owner) or {'username':'社区成员','avatar_data':''}
                item['avatar_data'] = avatars[owner]['avatar_data']
                item['author'] = avatars[owner]['username']
                item['content'],item['attachments'] = Forum._unpack_content(item['content'])
                result.append(item)
        end = offset+len(result)
        return {'items':result,'total':total,'limit':limit,'offset':offset,'next_offset':end if result and end<total else None}

    def engage(self, actor, post_id, *, kind, desired):
        if kind not in ('like','bookmark','follow') or type(desired) is not bool:
            raise ValueError('invalid_engagement')
        table,column,flag = {'like':('forum_likes','post_id','liked'),'bookmark':('forum_bookmarks','post_id','saved'),'follow':('forum_follows','author_user_id','following')}[kind]
        with self.db.transaction() as connection:
            post = self._visible(connection,actor,post_id)
            target = post['author_user_id'] if kind == 'follow' else post_id
            if kind == 'follow' and target == actor:
                raise ValueError('cannot_follow_self')
            if desired:
                connection.execute(f'INSERT INTO {table}({column},user_id,created_at) VALUES(?,?,?) ON CONFLICT ({column},user_id) DO NOTHING', (target,actor,timestamp_to_db(utc_now())))
            else:
                connection.execute(f'DELETE FROM {table} WHERE {column}=? AND user_id=?', (target,actor))
            result = {'post_id':post_id,flag:desired}
            if kind == 'follow':
                result['author_user_id'] = target
            else:
                counts = self._counts(connection,actor,post_id)
                count = 'likes' if kind == 'like' else 'saves'
                result[count] = counts[count]
        return result

    def comment(self, actor, post_id, *, content, attachment_ids=None, request_id):
        if not isinstance(request_id,str):
            raise ValueError('invalid_request_id')
        try:
            if str(UUID(request_id)) != request_id or UUID(request_id).version != 4:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError('invalid_request_id') from None
        attachments = Forum._attachment_ids(attachment_ids)
        if any(value>=2**63 for value in attachments) or isinstance(content,str) and '\x00' in content:
            raise ValueError('invalid_comment')
        content = Forum._text(content,4000,required=not attachments)
        packed = Forum._pack_content(content,attachments)
        digest = hashlib.sha256(json.dumps([post_id,content,attachments],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        with self.db.transaction() as connection:
            self._visible(connection,actor,post_id)
            prior = connection.execute('SELECT comment_id,post_id,content_digest FROM forum_comment_requests WHERE user_id=? AND request_id=?', (actor,request_id)).fetchone()
            if prior and (prior['post_id'] != post_id or prior['content_digest'] != digest):
                raise DetailConflict('request_content_changed')
            if prior:
                row = connection.execute('SELECT id,post_id,author_user_id,content,created_at FROM forum_comments WHERE id=? AND author_user_id=?', (prior['comment_id'],actor)).fetchone()
                if row is None:
                    raise DetailConflict('comment_no_longer_available')
            else:
                Forum._check_attachments(connection,actor,attachments)
                stamp = timestamp_to_db(utc_now())
                row = connection.execute('INSERT INTO forum_comments(post_id,author_user_id,content,created_at) VALUES(?,?,?,?) RETURNING id,post_id,author_user_id,content,created_at', (post_id,actor,packed,stamp)).fetchone()
                connection.execute('INSERT INTO forum_comment_requests(user_id,request_id,post_id,comment_id,content_digest,created_at) VALUES(?,?,?,?,?,?)', (actor,request_id,post_id,row['id'],digest,stamp))
            result = dict(row)
            result['content'],result['attachments'] = Forum._unpack_content(result['content'])
            result['author'] = connection.execute('SELECT username FROM users WHERE id=?', (actor,)).fetchone()['username']
            result['avatar_data'] = (self._identity(connection,actor) or {}) .get('avatar_data','')
            result['comments'] = self._counts(connection,actor,post_id)['comments']
        return result


def create_detail_router(auth,database):
    from fastapi import APIRouter,Depends,HTTPException,Query
    from fastapi.responses import JSONResponse
    from .platform_auth import authentication_dependency
    router = APIRouter(prefix='/v1/member/forum/detail',tags=['member'])
    principal = Depends(authentication_dependency(auth))
    service = ForumDetail(database)

    def run(identity,call):
        if identity.user.role_code != 'member':
            raise HTTPException(403,'member_required')
        try:
            with auth.identity(identity):
                return JSONResponse(call(identity.user.id),headers={'Cache-Control':'no-store'})
        except PermissionError:
            raise HTTPException(403,'member_required') from None
        except DetailNotFound:
            raise HTTPException(404,'not_found') from None
        except DetailConflict:
            raise HTTPException(409,'request_content_changed') from None
        except ValueError:
            raise HTTPException(422,'invalid_detail_request') from None

    def query(request,allowed):
        if set(request.query_params)-set(allowed) or any(len(request.query_params.getlist(k))!=1 for k in request.query_params):
            raise HTTPException(422,'invalid_detail_query')

    async def body(request,allowed):
        query(request,())
        try:
            raw=await request.body()
            if not 0<len(raw)<=18000:
                raise ValueError
            def unique(pairs):
                obj={}
                for k,v in pairs:
                    if k in obj:raise ValueError
                    obj[k]=v
                return obj
            obj=json.loads(raw,object_pairs_hook=unique)
            if type(obj) is not dict or set(obj)!=set(allowed):
                raise ValueError
            return obj
        except (ValueError,UnicodeError):
            raise HTTPException(422,'invalid_detail_body') from None

    @router.get('/{post_id}')
    def detail(post_id:int,request:Request,identity=principal):
        query(request,())
        return run(identity,lambda actor:service.get(actor,post_id))

    @router.get('/{post_id}/comments')
    def comments(post_id:int,request:Request,limit:int=Query(30,ge=1,le=50),offset:int=Query(0,ge=0,le=1000000),identity=principal):
        query(request,('limit','offset'))
        return run(identity,lambda actor:service.comments(actor,post_id,limit=limit,offset=offset))

    @router.put('/{post_id}/like')
    async def like(post_id:int,request:Request,identity=principal):
        value=await body(request,('liked',))
        return await asyncio.to_thread(run,identity,lambda actor:service.engage(actor,post_id,kind='like',desired=value['liked']))

    @router.put('/{post_id}/bookmark')
    async def bookmark(post_id:int,request:Request,identity=principal):
        value=await body(request,('saved',))
        return await asyncio.to_thread(run,identity,lambda actor:service.engage(actor,post_id,kind='bookmark',desired=value['saved']))

    @router.put('/{post_id}/follow')
    async def follow(post_id:int,request:Request,identity=principal):
        value=await body(request,('following',))
        return await asyncio.to_thread(run,identity,lambda actor:service.engage(actor,post_id,kind='follow',desired=value['following']))

    @router.post('/{post_id}/comments')
    async def add(post_id:int,request:Request,identity=principal):
        value=await body(request,('content','attachment_ids','request_id'))
        return await asyncio.to_thread(run,identity,lambda actor:service.comment(actor,post_id,**value))
    return router
