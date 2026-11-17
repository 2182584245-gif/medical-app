"""Small photo references in records, with private-file integrity checks."""
from __future__ import annotations

import hashlib
import hmac

MAX_PHOTO_BYTES = 2 * 1024 * 1024
PHOTO_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
PHOTO_PREFIX = '凡思记录照片-'
RAW_PHOTO_FIELDS = {'photo_base64', 'image_base64', 'data_base64', 'photo_data', 'image_data'}


def photo_ids(value, *, allow_local=False):
    if (type(value) is not list or len(value)>4 or any(type(item)is not int or item==0
            or abs(item)>=2**53 or not allow_local and item<0 for item in value)
            or len(set(value))!=len(value)):
        raise ValueError('照片信息无效，请重新选择照片。')
    return list(value)


def validate_owned_photos(connection, actor, details):
    if not details or 'photo_file_ids' not in details:
        return
    identifiers=photo_ids(details['photo_file_ids'])
    from .chat_attachments import _validate_image
    for identifier in identifiers:
        row=connection.execute('SELECT f.id,f.original_name,f.media_type,f.size_bytes,f.sha256,c.content,c.mime_type,c.sha256 AS content_sha256 FROM user_files f JOIN user_file_contents c ON c.file_id=f.id WHERE f.id=? AND f.user_id=?',(identifier,actor)).fetchone()
        if row is None or row['media_type']not in PHOTO_TYPES:
            raise ValueError('照片不存在或不属于当前账号，请重新选择。')
        data=bytes(row['content']);digest=hashlib.sha256(data).hexdigest()
        if (not 0<len(data)<=MAX_PHOTO_BYTES or len(data)!=row['size_bytes']
                or row['mime_type']!=row['media_type'] or row['content_sha256']!=row['sha256']
                or not hmac.compare_digest(digest,str(row['sha256']))):
            raise ValueError('照片校验未通过，请重新选择不超过2 MB的照片。')
        _validate_image(data,str(row['media_type']))
