"""Read-only access to the independent original-article presentation bundle."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import quote

from ollama_chat_app.web_desktop.bridge import BridgeRequestError

from mobile_reading_social import READS as SOCIAL_READS, WRITES as SOCIAL_WRITES, check as check_social
READING_READS = frozenset({'reading.catalog', 'reading.get', 'reading.asset'}) | SOCIAL_READS
READING_ACTIONS = READING_READS | SOCIAL_WRITES
MAX_ASSET = 2 * 1024 * 1024


def reading_request(api, action, body):
    if type(body) is not dict:
        raise BridgeRequestError('阅读请求格式无效。')
    if action in SOCIAL_READS:
        check_social(action,body)
        params=dict(body)
        if action=='reading.social.get':
            path='/v1/member/reading/social/'+quote(params.pop('article_id'),safe='')
        else:path='/v1/member/reading/'+{'reading.bookmarks':'bookmarks','reading.comments':'comments','reading.recommendations':'recommendations'}[action]
        result=api._client.request('GET',path,params={key:value for key,value in params.items() if value is not None})
        maximum=4*1024*1024
    elif action == 'reading.catalog':
        if set(body) - {'scope', 'query', 'limit', 'offset'}:
            raise BridgeRequestError('阅读目录参数无效。')
        scope, query = body.get('scope', 'all'), body.get('query', '')
        limit, offset = body.get('limit', 100), body.get('offset', 0)
        if (scope not in {'today', 'all'} or not isinstance(query, str) or len(query) > 200
                or type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or not 0 <= offset <= 100000):
            raise BridgeRequestError('阅读目录参数无效。')
        result = api._client.request('GET', '/v1/member/reading/catalog',
                                     params={'scope': scope, 'query': query, 'limit': limit, 'offset': offset})
        maximum = 512 * 1024
    elif action == 'reading.get':
        identifier = body.get('article_id')
        if set(body) != {'article_id'} or not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', identifier):
            raise BridgeRequestError('文章编号无效。')
        result = api._client.request('GET', '/v1/member/reading/articles/' + quote(identifier, safe=''))
        maximum = 8 * 1024 * 1024
    elif action == 'reading.asset':
        identifier = body.get('asset_id')
        if set(body) != {'asset_id'} or not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier):
            raise BridgeRequestError('文章图片编号无效。')
        result = api._client.request('GET', '/v1/member/reading/assets/' + identifier)
        maximum = 4 * 1024 * 1024
        if (type(result) is not dict or result.get('asset_id') != identifier or result.get('sha256') != identifier
                or result.get('media_type') not in {'image/webp', 'image/jpeg', 'image/png', 'image/gif'}
                or type(result.get('size_bytes')) is not int or not 1 <= result['size_bytes'] <= MAX_ASSET
                or not isinstance(result.get('content'), str) or len(result['content']) > 4 * ((MAX_ASSET + 2) // 3)):
            raise BridgeRequestError('文章图片暂时无法核对，请重新读取。')
        try:
            raw = base64.b64decode(result['content'], validate=True)
        except (ValueError, TypeError):
            raise BridgeRequestError('文章图片暂时无法核对，请重新读取。') from None
        if len(raw) != result['size_bytes'] or hashlib.sha256(raw).hexdigest() != identifier:
            raise BridgeRequestError('文章图片完整性检查未通过，请重新读取。')
    else:
        raise BridgeRequestError('阅读功能暂不可用。')
    try:
        size = len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf8'))
    except (TypeError, ValueError):
        raise BridgeRequestError('阅读内容格式暂时无法核对。') from None
    if type(result) is not dict or size > maximum:
        raise BridgeRequestError('阅读内容暂时无法完整读取，请重试。')
    return result
