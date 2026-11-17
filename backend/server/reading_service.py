"""Authenticated read-only presentation of independently preserved articles.

No library mutation, database write, arbitrary path, remote fetch or model call
exists in this module. Source articles and media are precompiled at release time.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from urllib.parse import urlsplit

MAX_ARTICLE_BYTES=8*1024*1024
MAX_ASSET_BYTES=2*1024*1024
ID=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$')
SHA=re.compile(r'^[a-f0-9]{64}$')
ANCHOR=re.compile(r'^r-[a-f0-9]{16}$')
MIMES=frozenset({'image/webp','image/png','image/jpeg','image/gif'})
INLINE=frozenset({'text','a','strong','em','code','br','span','sup','sub','s'})
BLOCK=frozenset({'p','h1','h2','h3','h4','h5','h6','blockquote','ul','ol','table','figure'})


class ReadingError(ValueError):pass


def require(value):
    if not value:raise ReadingError('阅读资料暂时无法显示，请稍后重试。')


def json_object(raw):
    try:
        value=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,TypeError,UnicodeDecodeError,RecursionError):raise ReadingError('阅读资料暂时无法显示，请稍后重试。') from None
    require(type(value) is dict)
    return value


def safe_link(value):
    if type(value) is not str or len(value)>8192 or any(ord(char)<32 for char in value):return False
    if value.startswith('#'):return ANCHOR.fullmatch(value[1:]) is not None
    try:parsed=urlsplit(value)
    except ValueError:return False
    return parsed.scheme in {'http','https'} and bool(parsed.hostname) and not parsed.username and not parsed.password


def inline_nodes(values, *, depth=0):
    require(type(values) is list and len(values)<=20000 and depth<=16)
    for row in values:
        require(type(row) is dict and row.get('tag') in INLINE and set(row)<={'tag','text','href','children','id'})
        if 'id' in row:require(type(row['id']) is str and ANCHOR.fullmatch(row['id']))
        if row['tag']=='text':require(type(row.get('text')) is str and len(row['text'])<=500000)
        elif row['tag']=='a':require(safe_link(row.get('href')))
        if row['tag'] not in {'text','br'}:inline_nodes(row.get('children',[]),depth=depth+1)


def validate_blocks(values, assets, *, depth=0):
    require(type(values) is list and (0<len(values) or depth>0) and len(values)<=20000 and depth<=16)
    for row in values:
        require(type(row) is dict and row.get('tag') in BLOCK
                and set(row)<={'tag','id','children','items','rows','asset_id','alt','caption','href','blocks'})
        if 'id' in row:require(type(row['id']) is str and ANCHOR.fullmatch(row['id']))
        tag=row['tag']
        if 'href' in row:require(tag=='figure' and safe_link(row['href']))
        if 'blocks' in row:
            require(tag in {'h1','h2','h3','h4','h5','h6','blockquote'} and 'children' not in row)
            validate_blocks(row['blocks'],assets,depth=depth+1)
        elif tag in {'p','h1','h2','h3','h4','h5','h6','blockquote'}:inline_nodes(row.get('children',[]))
        elif tag in {'ul','ol'}:
            require(type(row.get('items')) is list and len(row['items'])<=10000)
            for item in row['items']:inline_nodes(item)
        elif tag=='figure':
            require(type(row.get('asset_id')) is str and row['asset_id'] in assets)
            for field in ('alt','caption'):
                if field in row:require(type(row[field]) is str and len(row[field])<=10000)
        elif tag=='table':
            require(type(row.get('rows')) is list and 0<len(row['rows'])<=10000)
            if 'caption' in row:require(type(row['caption']) is str and len(row['caption'])<=10000)
            for line in row['rows']:
                require(type(line) is dict and set(line)=={'cells'} and type(line['cells']) is list and 0<len(line['cells'])<=1000)
                for cell in line['cells']:
                    require(type(cell) is dict and set(cell)=={'header','colspan','rowspan','children'}
                            and type(cell['header']) is bool and type(cell['colspan']) is int and type(cell['rowspan']) is int
                            and 1<=cell['colspan']<=100 and 1<=cell['rowspan']<=100)
                    inline_nodes(cell['children'])


class ReadingPresentation:
    def __init__(self,root):
        self.root=Path(root).absolute();self._lock=RLock();self._articles=OrderedDict();self._assets=OrderedDict()
        self._article_bytes=0;self._asset_bytes=0
        require(self.root.resolve()==self.root and self.root.is_dir() and not self.root.is_symlink())
        manifest=json_object(self._read('manifest.json',16*1024*1024))
        require(manifest.get('schema_version')==1 and type(manifest.get('documents')) is list and type(manifest.get('assets')) is dict)
        require(0<len(manifest['documents'])<=10000 and len(manifest['assets'])<=20000)
        self.assets=manifest['assets'];self.documents=[];self.by_id={}
        for key,value in self.assets.items():
            require(type(key) is str and SHA.fullmatch(key) and type(value) is dict and value.get('sha256')==key
                    and value.get('media_type') in MIMES and type(value.get('size_bytes')) is int and 1<=value['size_bytes']<=MAX_ASSET_BYTES
                    and type(value.get('path')) is str and re.fullmatch(r'assets/'+key+r'\.(?:webp|png|jpg|gif)',value['path']))
        for row in manifest['documents']:
            require(type(row) is dict and type(row.get('id')) is str and ID.fullmatch(row['id']) and row['id'] not in self.by_id)
            require(row.get('origin') in {'content','card'} and type(row.get('title')) is str and 0<len(row['title'])<=2000
                    and type(row.get('today_eligible')) is bool and row.get('cover_asset') in self.assets)
            require(row.get('article_path')=='articles/'+row['id']+'.json'
                    and type(row.get('article_sha256')) is str and SHA.fullmatch(row['article_sha256'])
                    and type(row.get('raw_sha256')) is str and SHA.fullmatch(row['raw_sha256']))
            self.documents.append(row);self.by_id[row['id']]=row
        # Each article has its own illustration. Repeated source-media files
        # remain deduplicated in the body, but cannot become repeated covers.
        require(len({row['cover_asset'] for row in self.documents})==len(self.documents))
    def _read(self,relative,maximum):
        require(type(relative) is str and relative and '\\' not in relative)
        path=self.root/relative
        require(path.resolve()==path and path.is_relative_to(self.root) and not path.is_symlink()
                and not any(parent.is_symlink() for parent in path.parents) and path.is_file()
                and 0<path.stat().st_size<=maximum)
        return path.read_bytes()
    @staticmethod
    def public(row):
        return {key:row[key] for key in ('id','origin','title','source_title','source_url','published_at','cover_asset',
            'today_eligible','summary','reading_minutes','raw_sha256') if key in row}
    def catalog(self,*,scope='all',query='',limit=100,offset=0):
        require(scope in {'all','today'} and type(query) is str and len(query)<=200
                and type(limit) is int and 1<=limit<=100 and type(offset) is int and 0<=offset<=100000)
        query=query.strip().casefold()
        rows=[row for row in self.documents if (scope=='all' or row['today_eligible'])
              and (not query or query in (row['title']+' '+str(row.get('summary',''))).casefold())]
        return {'schema_version':1,'items':[self.public(row) for row in rows[offset:offset+limit]],
                'total':len(rows),'limit':limit,'offset':offset,'scope':scope}
    def article(self,article_id):
        require(type(article_id) is str and ID.fullmatch(article_id) and article_id in self.by_id)
        with self._lock:
            if article_id not in self._articles:
                meta=self.by_id[article_id];raw=self._read(meta['article_path'],MAX_ARTICLE_BYTES)
                require(sha256(raw).hexdigest()==meta['article_sha256'])
                value=json_object(raw)
                require(value.get('schema_version')==1 and value.get('id')==article_id and value.get('raw_sha256')==meta['raw_sha256'])
                validate_blocks(value.get('blocks'),self.assets)
                require(value.get('cover_asset')==meta['cover_asset'])
                self._articles[article_id]=raw
                self._article_bytes+=len(raw)
                while len(self._articles)>1 and (self._article_bytes>24*1024*1024 or len(self._articles)>128):
                    _,old=self._articles.popitem(last=False);self._article_bytes-=len(old)
            self._articles.move_to_end(article_id)
            # Do not share a mutable cached dictionary across requests.
            return json_object(self._articles[article_id])
    def asset(self,asset_id):
        require(type(asset_id) is str and SHA.fullmatch(asset_id) and asset_id in self.assets)
        with self._lock:
            if asset_id not in self._assets:
                meta=self.assets[asset_id];raw=self._read(meta['path'],MAX_ASSET_BYTES)
                require(len(raw)==meta['size_bytes'] and sha256(raw).hexdigest()==asset_id)
                self._assets[asset_id]=raw
                self._asset_bytes+=len(raw)
                while len(self._assets)>1 and (self._asset_bytes>24*1024*1024 or len(self._assets)>256):
                    _,old=self._assets.popitem(last=False);self._asset_bytes-=len(old)
            self._assets.move_to_end(asset_id)
            raw=self._assets[asset_id];meta=self.assets[asset_id]
        return {'asset_id':asset_id,'media_type':meta['media_type'],'size_bytes':len(raw),'sha256':asset_id,
                'content':base64.b64encode(raw).decode('ascii')}


def create_reading_router(auth,root):
    from fastapi import APIRouter,Depends,HTTPException,Query
    from fastapi.routing import APIRoute
    from .platform_auth import authentication_dependency
    class ReadingRoute(APIRoute):
        def get_route_handler(self):
            original=super().get_route_handler()
            async def handler(request):
                response=await original(request)
                response.headers['Cache-Control']='no-store'
                response.headers['X-Content-Type-Options']='nosniff'
                return response
            return handler
    reader=ReadingPresentation(root)
    router=APIRouter(prefix='/v1/member/reading',tags=['reading'],route_class=ReadingRoute)
    dependency=Depends(authentication_dependency(auth))
    def member(principal):
        if principal.user.role_code!='member':raise HTTPException(403,detail='请使用会员账号阅读。')
    def call(function,*args,**kwargs):
        try:return function(*args,**kwargs)
        except ReadingError:raise HTTPException(404,detail='阅读资料暂时无法显示，请稍后重试。') from None
    @router.get('/catalog')
    def catalog(scope:str=Query(default='all',pattern='^(all|today)$'),query:str=Query(default='',max_length=200),
                limit:int=Query(default=100,ge=1,le=100),offset:int=Query(default=0,ge=0,le=100000),principal=dependency):
        member(principal)
        return call(reader.catalog,scope=scope,query=query,limit=limit,offset=offset)
    @router.get('/articles/{article_id}')
    def article(article_id:str,principal=dependency):
        member(principal);return call(reader.article,article_id)
    @router.get('/assets/{asset_id}')
    def asset(asset_id:str,principal=dependency):
        member(principal);return call(reader.asset,asset_id)
    return router


__all__=['ReadingError','ReadingPresentation','create_reading_router','validate_blocks']
