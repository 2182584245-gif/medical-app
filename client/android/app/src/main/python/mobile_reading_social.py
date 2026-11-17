"""Account-bound projections for durable offline reading interactions."""
from copy import deepcopy
import json
import re
from uuid import UUID

READS=frozenset({'reading.social.get','reading.bookmarks','reading.comments','reading.recommendations'})
WRITES=frozenset({'reading.social.set','reading.share','reading.comment.add','reading.comment.like'})
COMMENT_META_BYTES=3*1024*1024

def bounded_comments(values,*,compact=False):
    """Keep newest account-bound metadata within an explicit byte budget."""
    keys={'id','article_id','parent_id','created_at','likes','liked','replies'}
    entries=sorted(((key,row) for key,row in values.items() if isinstance(row,dict)),
        key=lambda item:(item[1].get('created_at',''),item[0]),reverse=True)
    result={};used=2
    for key,row in entries:
        value={name:item for name,item in row.items() if name in keys} if compact else row
        size=len(json.dumps({key:value},ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())
        if used+size>COMMENT_META_BYTES:continue
        result[key]=value;used+=size
        if len(result)>=(200 if compact else 100):break
    return dict(reversed(tuple(result.items())))

def check(action,body):
    if type(body)is not dict:raise ValueError('阅读请求无效。')
    aid=body.get('article_id')
    if action not in {'reading.bookmarks','reading.recommendations'} and (type(aid)is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}',aid)is None):raise ValueError('文章编号无效。')
    allowed={
      'reading.social.get':{'article_id'},'reading.bookmarks':{'limit','offset'},
      'reading.comments':{'article_id','parent_id','limit','offset'},'reading.recommendations':{'exclude_article_id','limit'},
      'reading.social.set':{'article_id','kind','desired'},'reading.share':{'article_id'},
      'reading.comment.add':{'article_id','content','parent_id'},'reading.comment.like':{'article_id','comment_id','desired'}}
    if action not in allowed or set(body)-allowed[action]:raise ValueError('阅读字段无效。')
    if 'limit' in body and (type(body['limit'])is not int or not 1<=body['limit']<=100):raise ValueError('条数无效。')
    if 'offset' in body and (type(body['offset'])is not int or not 0<=body['offset']<=100000):raise ValueError('分页无效。')
    for key in ('comment_id','parent_id'):
        if body.get(key)is not None:
            try:
                if type(body[key])is not str or str(UUID(body[key]))!=body[key] or UUID(body[key]).version!=4:raise ValueError()
            except (TypeError,AttributeError,ValueError):raise ValueError('评论编号无效。') from None
    if action=='reading.social.set' and (set(body)!={'article_id','kind','desired'} or body['kind']not in {'like','bookmark','follow','read'}):raise ValueError('阅读状态无效。')
    if action in {'reading.social.set','reading.comment.like'} and type(body.get('desired'))is not bool:raise ValueError('请选择操作状态。')
    if action=='reading.comment.like' and not body.get('comment_id'):raise ValueError('评论编号无效。')
    if action=='reading.comment.add' and (type(body.get('content'))is not str or not 1<=len(body['content'].strip())<=4000 or '\x00' in body['content']):raise ValueError('请输入1至4000字的评论。')
    if body.get('exclude_article_id')is not None and (type(body['exclude_article_id'])is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}',body['exclude_article_id'])is None):raise ValueError('文章编号无效。')

def project(value,operation,actor):
    action=operation['action'];body=operation['payload'];check(action,body);aid=body['article_id'];now=operation['created_at']
    value.setdefault('articles',{});value.setdefault('follows',{});value.setdefault('comment_likes',{});value.setdefault('pending_comments',{})
    row=value['articles'].setdefault(aid,{'article_id':aid,'liked':False,'saved':False,'read':False,'saved_at':None})
    for key,item in {'article_id':aid,'liked':False,'saved':False,'read':False,'saved_at':None}.items():row.setdefault(key,item)
    if action=='reading.social.set':
        kind,desired=body['kind'],body['desired']
        if kind=='follow':
            # On a first-ever offline visit the source ID can be obtained from
            # the already cached article metadata by the facade before enqueue.
            publisher=row.get('publisher',{}).get('id')
            if not publisher:raise ValueError('请先联网打开这篇文章，再关注发布方。')
            value['follows'][publisher]=desired
        else:
            key={'like':'liked','bookmark':'saved','read':'read'}[kind];row[key]=desired
            if kind=='bookmark':row['saved_at']=(row.get('saved_at') or now) if desired else None
    elif action=='reading.share':row['pending_shares']=row.get('pending_shares',0)+1
    elif action=='reading.comment.add':
        comment={'id':operation['operation_id'],'article_id':aid,'parent_id':body.get('parent_id'),'author':value.get('_author','我'),
            'avatar_data':value.get('_avatar',''),'content':body['content'].strip(),'created_at':now,'likes':0,'liked':False,'replies':0,'sync_status':'pending'}
        value['pending_comments'][comment['id']]=comment
    elif action=='reading.comment.like':value['comment_likes'][body['comment_id']]=body['desired']
    row['updated_at']=now;row['sync_status']='pending'
    result=deepcopy(row)
    result['following']=bool(value['follows'].get(row.get('publisher',{}).get('id')))
    if action=='reading.comment.add':result['comment']=comment
    if action=='reading.comment.like':
        comment=deepcopy(operation.get('reading_comment') or value['pending_comments'].get(body['comment_id']) or {})
        if 'likes' in comment:comment['likes']=max(0,int(comment['likes'])+int(body['desired'])-int(bool(comment.get('liked'))))
        result['comment']={**comment,'id':body['comment_id'],'liked':body['desired']}
    return result

def read(facade,identity,action,body,resources):
    check(action,body);value=resources.get('reading_social')
    if value is None:return None,False
    value=deepcopy(value)
    # Older knowledge-list controls still use content.progress. Respect the
    # latest local edit in either interface without replacing public counters.
    legacy=resources.get('reading',{})
    legacy=legacy if isinstance(legacy,dict) else {row.get('content_id'):row for row in legacy}
    for key,progress in legacy.items():
        if not isinstance(progress,dict):continue
        ownrow=value.setdefault('articles',{}).get(key,{})
        if ownrow and str(progress.get('updated_at') or '')<str(ownrow.get('updated_at') or ''):continue
        row=value['articles'].setdefault(key,{'article_id':key,'liked':False})
        previous=row.get('saved');row.update(saved=bool(progress.get('saved')),read=progress.get('progress')==1)
        if not row['saved']:row['saved_at']=None
        elif not previous or not row.get('saved_at'):row['saved_at']=progress.get('updated_at')
    cached=facade.store.get(identity,'cache',[action,body]);aid=body.get('article_id');own=value.get('articles',{}).get(aid,{})
    if action=='reading.social.get':
        if cached is None and not own.get('publisher'):return None,False
        result={**(cached or {}),**deepcopy(own)}
        pub=result.get('publisher',{}).get('id');result['following']=bool(value.get('follows',{}).get(pub,False))
        for flag,count in (('liked','likes'),('saved','saves')):
            if cached is not None:result[count]=max(0,int(cached.get(count,0))+int(bool(own.get(flag,cached.get(flag))))-int(bool(cached.get(flag))))
        result['shares']=int((cached or {}).get('shares',0))+int(own.get('pending_shares',0))
        pending=[c for c in value.get('pending_comments',{}).values() if c['article_id']==aid]
        result['comments']=int((cached or {}).get('comments',0))+len(pending)
        facade._queue_read_refresh(identity,action,body)
        return result,True
    if action=='reading.bookmarks':
        rows=[deepcopy(row) for row in value.get('articles',{}).values() if row.get('saved')]
        rows.sort(key=lambda row:(str(row.get('saved_at') or ''),row['article_id']),reverse=True)
        limit,offset=body.get('limit',30),body.get('offset',0)
        return {'items':rows[offset:offset+limit],'total':len(rows),'limit':limit,'offset':offset,'next_offset':offset+limit if offset+limit<len(rows) else None},True
    if action=='reading.comments' and cached is not None:
        result=deepcopy(cached);rows=result.get('items',[]);seen={row['id'] for row in rows};parent=body.get('parent_id')
        remembered=facade.store.get(identity,'meta','reading-confirmed-comments',{})
        known={**value.get('pending_comments',{}),**remembered}
        generated=result.get('generated_at')
        def newer(row):return not generated or str(row.get('created_at') or '')>str(generated)
        for row in known.values():
            if row['article_id']==aid and row.get('parent_id')==parent and row['id']not in seen:
                rows.append(deepcopy(row));seen.add(row['id'])
                # A confirmed comment may simply be on a later server page.
                # Its absence from this page must not inflate the total.
                if newer(row):result['total']=result.get('total',0)+1
        for row in rows:
            if row['id'] in value.get('comment_likes',{}):
                desired=value['comment_likes'][row['id']];row['likes']=max(0,int(row.get('likes',0))+int(desired)-int(bool(row.get('liked'))));row['liked']=desired
            row['replies']=int(row.get('replies',0))+sum(1 for c in known.values() if c.get('parent_id')==row['id'] and newer(c))
        rows.sort(key=lambda row:(row['created_at'],row['id']));facade._queue_read_refresh(identity,action,body)
        return result,True
    return None,False
