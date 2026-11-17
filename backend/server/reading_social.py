"""Member reading interactions, separate from immutable source libraries."""
from __future__ import annotations
from datetime import datetime,timezone
from hashlib import sha256
import json
import math
import random
from uuid import UUID

from .member_forum_detail import avatar

READS=frozenset({'reading.social.get','reading.bookmarks','reading.comments','reading.recommendations'})
WRITES=frozenset({'reading.social.set','reading.share','reading.comment.add','reading.comment.like'})

def stamp():return datetime.now(timezone.utc).isoformat()
def require(value,message='阅读操作无效，请重新打开文章。'):
    if not value:raise ValueError(message)
def uuid(value):
    try:require(type(value)is str and str(UUID(value))==value and UUID(value).version==4)
    except (TypeError,AttributeError,ValueError):raise ValueError('操作编号无效。') from None
    return value
def window(body):
    limit,offset=body.get('limit',30),body.get('offset',0)
    require(type(limit)is int and 1<=limit<=100 and type(offset)is int and 0<=offset<=100000)
    return limit,offset

class ReadingSocial:
    def __init__(self,database,reader,*,rng=None):self.db,self.reader,self.rng=database,reader,rng or random.SystemRandom()
    def metadata(self,article_id):
        require(type(article_id)is str and article_id in self.reader.by_id,'文章暂不可用。')
        return self.reader.by_id[article_id]
    def publisher(self,article_id):
        row=self.metadata(article_id)
        # A source publisher is not presented as an invented member author.
        name=str(row.get('author') or row.get('source_title') or '文章来源').strip()
        identity=str(row.get('author') or row.get('source_title') or row.get('source_url') or '文章来源').strip().casefold()
        return {'id':'source-'+sha256(identity.encode()).hexdigest()[:24],'name':name,'avatar_data':'','kind':'source'}
    def _actor(self,conn,actor):
        require(type(actor)is int and actor>0)
        row=conn.execute('SELECT username,role_code,account_status FROM users WHERE id=?',(actor,)).fetchone()
        require(row is not None and row['role_code']=='member' and row['account_status']=='active','请重新登录会员账号。')
        return row
    def _counts(self,conn,article_id):
        if getattr(self.db,'dialect','sqlite')=='postgresql':
            raw=conn.execute('SELECT medical_app_platform.reading_public_counts(?) AS value',(article_id,)).fetchone()['value']
            require(raw is not None);return json.loads(raw)
        row=conn.execute('SELECT '
          '(SELECT COUNT(*) FROM reading_engagement WHERE article_id=? AND liked=1) AS likes,'
          '(SELECT COUNT(*) FROM expansion_content_progress WHERE content_id=? AND saved=1) AS saves,'
          '(SELECT COUNT(*) FROM reading_shares WHERE article_id=?) AS shares,'
          '(SELECT COUNT(*) FROM reading_comments WHERE article_id=?) AS comments', (article_id,)*4).fetchone()
        return dict(row)
    def _state(self,conn,actor,article_id):
        publisher=self.publisher(article_id)
        row=conn.execute('SELECT liked,saved,is_read,saved_at,updated_at FROM reading_engagement WHERE user_id=? AND article_id=?',(actor,article_id)).fetchone()
        # Existing read/bookmark progress remains visible until first explicit
        # interaction migrates it into the dedicated reading-interaction row.
        legacy=conn.execute('SELECT progress,saved,updated_at FROM expansion_content_progress WHERE user_id=? AND content_id=?',(actor,article_id)).fetchone()
        return {'article_id':article_id,'title':self.metadata(article_id)['title'],'publisher':publisher,'liked':bool(row['liked']) if row else False,
            'saved':bool(legacy['saved']) if legacy else bool(row and row['saved']),
            'read':bool(legacy['progress']==1) if legacy else bool(row and row['is_read']),
            'saved_at':(row['saved_at'] if row and row['saved_at'] and (not legacy or str(row['updated_at'])>=str(legacy['updated_at'])) else legacy['updated_at'] if legacy else row['saved_at'] if row else None) if (bool(legacy['saved']) if legacy else bool(row and row['saved'])) else None,
            'following':conn.execute('SELECT 1 FROM reading_follows WHERE user_id=? AND publisher_id=?',(actor,publisher['id'])).fetchone() is not None,
            **self._counts(conn,article_id)}
    def get(self,actor,article_id):
        with self.db.connect() as conn:self._actor(conn,actor);return self._state(conn,actor,article_id)
    def snapshot(self,conn,actor):
        self._actor(conn,actor)
        articles={}
        for row in conn.execute('SELECT article_id,liked,saved,is_read,saved_at,updated_at FROM reading_engagement WHERE user_id=? ORDER BY article_id',(actor,)).fetchall():
            if row['article_id'] not in self.reader.by_id:continue
            value=dict(row);value['read']=bool(value.pop('is_read'));value['saved']=bool(value['saved']);value['liked']=bool(value['liked'])
            value['title']=self.reader.by_id[row['article_id']]['title'];value['publisher']=self.publisher(row['article_id']);articles[row['article_id']]=value
        for row in conn.execute('SELECT content_id,progress,saved,updated_at FROM expansion_content_progress WHERE user_id=? ORDER BY content_id',(actor,)).fetchall():
            aid=row['content_id']
            if aid not in self.reader.by_id:continue
            if aid in articles:
                existing=articles[aid];existing['saved']=bool(row['saved']);existing['read']=row['progress']==1
                if not existing['saved']:existing['saved_at']=None
                elif not existing.get('saved_at') or str(row['updated_at'])>str(existing['updated_at']):existing['saved_at']=row['updated_at']
                continue
            articles[aid]={'article_id':aid,'liked':False,'saved':bool(row['saved']),'read':row['progress']==1,'saved_at':row['updated_at'] if row['saved'] else None,'updated_at':row['updated_at'],'title':self.reader.by_id[aid]['title'],'publisher':self.publisher(aid)}
        follows={row['publisher_id']:True for row in conn.execute('SELECT publisher_id FROM reading_follows WHERE user_id=? ORDER BY publisher_id',(actor,)).fetchall()}
        likes={row['comment_id']:True for row in conn.execute('SELECT comment_id FROM reading_comment_likes WHERE user_id=? ORDER BY comment_id',(actor,)).fetchall()}
        return {'articles':articles,'follows':follows,'comment_likes':likes}
    def bookmarks(self,actor,body):
        require(not set(body)-{'limit','offset'});limit,offset=window(body)
        with self.db.connect() as conn:
            values=[row for row in self.snapshot(conn,actor)['articles'].values() if row['saved']]
        values.sort(key=lambda row:(str(row['saved_at'] or ''),row['article_id']),reverse=True)
        return {'items':values[offset:offset+limit],'total':len(values),'limit':limit,'offset':offset,'next_offset':offset+limit if offset+limit<len(values) else None}
    def recommendations(self,actor,body):
        require(not set(body)-{'exclude_article_id','limit'});limit=body.get('limit',10);excluded=body.get('exclude_article_id')
        require(type(limit)is int and 1<=limit<=100 and (excluded is None or type(excluded)is str))
        with self.db.connect() as conn:
            self._actor(conn,actor);following={row['publisher_id'] for row in conn.execute('SELECT publisher_id FROM reading_follows WHERE user_id=?',(actor,)).fetchall()}
        candidates=[row for row in self.reader.documents if row['today_eligible'] and row['id']!=excluded]
        # Efraimidis-Spirakis weighted sampling without replacement. A followed
        # source gets four times the draw weight, not a fabricated popularity.
        ranked=sorted(candidates,key=lambda row:-math.log(max(self.rng.random(),1e-15))/(4 if self.publisher(row['id'])['id'] in following else 1))
        return {'items':[{**self.reader.public(row),'publisher':self.publisher(row['id'])} for row in ranked[:limit]],'total':len(candidates)}
    def _comment(self,conn,actor,row):
        value=dict(row)
        counts=conn.execute('SELECT (SELECT COUNT(*) FROM reading_comment_likes WHERE comment_id=?) AS likes,'
            'EXISTS(SELECT 1 FROM reading_comment_likes WHERE comment_id=? AND user_id=?) AS liked,'
            '(SELECT COUNT(*) FROM reading_comments WHERE parent_id=?) AS replies',(row['id'],row['id'],actor,row['id'])).fetchone()
        value.pop('user_id',None);value.update(likes=int(counts['likes']),liked=bool(counts['liked']),replies=int(counts['replies']))
        return value
    def comments(self,actor,body):
        require(not set(body)-{'article_id','parent_id','limit','offset'});aid=body.get('article_id');self.metadata(aid)
        limit,offset=window(body);parent=body.get('parent_id')
        with self.db.connect() as conn:
            self._actor(conn,actor)
            if parent is not None:
                uuid(parent);row=conn.execute('SELECT article_id,parent_id FROM reading_comments WHERE id=?',(parent,)).fetchone()
                require(row is not None and row['article_id']==aid and row['parent_id'] is None,'这条评论暂不可用。')
            clause='parent_id IS NULL' if parent is None else 'parent_id=?';params=(aid,) if parent is None else (aid,parent)
            total=conn.execute('SELECT COUNT(*) AS n FROM reading_comments WHERE article_id=? AND '+clause,params).fetchone()['n']
            rows=conn.execute('SELECT * FROM reading_comments WHERE article_id=? AND '+clause+' ORDER BY created_at ASC,id ASC LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
            # Avatars are real PNG data URLs and may be substantially larger
            # than comment text. Page by bytes as well as rows, preserving a
            # contiguous prefix so next_offset never skips an unseen comment.
            items=[];used=4096
            for row in rows:
                item=self._comment(conn,actor,row)
                size=len(json.dumps(item,ensure_ascii=False,separators=(',',':')).encode())
                if items and used+size>3*1024*1024:break
                require(used+size<=3*1024*1024,'评论暂时无法完整读取。')
                items.append(item);used+=size
            root_total=conn.execute('SELECT COUNT(*) AS n FROM reading_comments WHERE article_id=? AND parent_id IS NULL',(aid,)).fetchone()['n']
        return {'items':items,'total':total,'root_total':root_total,'parent_total':total if parent else None,'limit':limit,'offset':offset,'next_offset':offset+len(items) if offset+len(items)<total else None,'generated_at':stamp()}
    def mutate(self,actor,op,action,body):
        uuid(op);aid=body.get('article_id');self.metadata(aid);require(action in WRITES)
        with self.db.transaction() as conn:
            identity=self._actor(conn,actor)
            if action=='reading.social.set':
                require(set(body)=={'article_id','kind','desired'} and body['kind'] in {'like','bookmark','follow','read'} and type(body['desired'])is bool)
                desired=body['desired'];kind=body['kind'];now=stamp()
                if kind=='follow':
                    publisher=self.publisher(aid)['id']
                    if desired:conn.execute('INSERT INTO reading_follows(user_id,publisher_id,created_at) VALUES(?,?,?) ON CONFLICT(user_id,publisher_id) DO NOTHING',(actor,publisher,now))
                    else:conn.execute('DELETE FROM reading_follows WHERE user_id=? AND publisher_id=?',(actor,publisher))
                else:
                    state=self._state(conn,actor,aid);field={'like':'liked','bookmark':'saved','read':'read'}[kind];state[field]=desired
                    saved_at=(state['saved_at'] or now) if state['saved'] else None
                    conn.execute('INSERT INTO reading_engagement(user_id,article_id,liked,saved,is_read,saved_at,updated_at) VALUES(?,?,?,?,?,?,?) '
                        'ON CONFLICT(user_id,article_id) DO UPDATE SET liked=excluded.liked,saved=excluded.saved,is_read=excluded.is_read,saved_at=excluded.saved_at,updated_at=excluded.updated_at',
                        (actor,aid,int(state['liked']),int(state['saved']),int(state['read']),saved_at,now))
                    # Existing dashboard/memory reads still use this original
                    # progress table. Keep it in the same transaction.
                    conn.execute('INSERT INTO expansion_content_progress(user_id,content_id,progress,saved,completed_at,updated_at) VALUES(?,?,?,?,?,?) '
                        'ON CONFLICT(user_id,content_id) DO UPDATE SET progress=excluded.progress,saved=excluded.saved,completed_at=excluded.completed_at,updated_at=excluded.updated_at',
                        (actor,aid,int(state['read']),int(state['saved']),now if state['read'] else None,now))
            elif action=='reading.share':
                require(set(body)=={'article_id'})
                old=conn.execute('SELECT user_id,article_id FROM reading_shares WHERE id=?',(op,)).fetchone()
                require(old is None or old['user_id']==actor and old['article_id']==aid)
                conn.execute('INSERT INTO reading_shares(id,user_id,article_id,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING',(op,actor,aid,stamp()))
            elif action=='reading.comment.add':
                require(not set(body)-{'article_id','content','parent_id'});text=body.get('content');parent=body.get('parent_id')
                require(type(text)is str and 1<=len(text.strip())<=4000 and '\x00' not in text,'请输入1至4000字的评论。');text=text.strip()
                if parent is not None:
                    uuid(parent);row=conn.execute('SELECT article_id,parent_id FROM reading_comments WHERE id=?',(parent,)).fetchone()
                    require(row is not None and row['article_id']==aid and row['parent_id'] is None,'仅支持两级评论，请回复主评论。')
                previous=conn.execute('SELECT * FROM reading_comments WHERE id=?',(op,)).fetchone()
                require(previous is None or previous['user_id']==actor and previous['article_id']==aid and previous['content']==text and previous['parent_id']==parent,'同一操作不能发布不同评论。')
                prefs=conn.execute('SELECT preferences_json FROM user_preferences WHERE user_id=?',(actor,)).fetchone()
                try:picture=avatar(json.loads(prefs['preferences_json'] or '{}').get('avatar_data')) if prefs else ''
                except (ValueError,TypeError):picture=''
                conn.execute('INSERT INTO reading_comments(id,user_id,article_id,parent_id,content,author,avatar_data,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (op,actor,aid,parent,text,identity['username'],picture,stamp()))
            elif action=='reading.comment.like':
                require(set(body)=={'article_id','comment_id','desired'} and type(body['desired'])is bool);cid=uuid(body['comment_id'])
                target=conn.execute('SELECT article_id FROM reading_comments WHERE id=?',(cid,)).fetchone();require(target is not None and target['article_id']==aid,'这条评论暂不可用。')
                if body['desired']:conn.execute('INSERT INTO reading_comment_likes(comment_id,user_id,created_at) VALUES(?,?,?) ON CONFLICT(comment_id,user_id) DO NOTHING',(cid,actor,stamp()))
                else:conn.execute('DELETE FROM reading_comment_likes WHERE comment_id=? AND user_id=?',(cid,actor))
            result=self._state(conn,actor,aid)
            if action=='reading.comment.add':
                result['comment']=self._comment(conn,actor,conn.execute('SELECT * FROM reading_comments WHERE id=?',(op,)).fetchone())
                result['root_comments']=conn.execute('SELECT COUNT(*) AS n FROM reading_comments WHERE article_id=? AND parent_id IS NULL',(aid,)).fetchone()['n']
                if body.get('parent_id'):result['parent_replies']=conn.execute('SELECT COUNT(*) AS n FROM reading_comments WHERE parent_id=?',(body['parent_id'],)).fetchone()['n']
            if action=='reading.comment.like':result['comment']=self._comment(conn,actor,conn.execute('SELECT * FROM reading_comments WHERE id=?',(body['comment_id'],)).fetchone())
            return result

def create_reading_social_router(auth,service):
    from fastapi import APIRouter,Depends,HTTPException,Query
    from fastapi.responses import JSONResponse
    from .platform_auth import authentication_dependency
    router=APIRouter(prefix='/v1/member/reading',tags=['reading']);principal=Depends(authentication_dependency(auth))
    def call(p,method,*args):
        if p.user.role_code!='member':raise HTTPException(403,'请使用会员账号。')
        try:
            with auth.identity(p):result=method(p.user.id,*args)
            return JSONResponse(result,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        except ValueError as error:raise HTTPException(422,str(error)) from None
    @router.get('/social/{article_id}')
    def social(article_id:str,p=principal):return call(p,service.get,article_id)
    @router.get('/bookmarks')
    def bookmarks(limit:int=Query(30,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),p=principal):return call(p,service.bookmarks,{'limit':limit,'offset':offset})
    @router.get('/comments')
    def comments(article_id:str,parent_id:str|None=None,limit:int=Query(30,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),p=principal):return call(p,service.comments,{'article_id':article_id,'parent_id':parent_id,'limit':limit,'offset':offset})
    @router.get('/recommendations')
    def recommendations(exclude_article_id:str|None=None,limit:int=Query(10,ge=1,le=100),p=principal):return call(p,service.recommendations,{'exclude_article_id':exclude_article_id,'limit':limit})
    return router
