"""Owner-bound encrypted record photos; no image recognition or plain disk copy."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from uuid import UUID, uuid4, uuid5

from mobile_local149 import LocalDataError
from ollama_chat_app.services.chat_attachments import prepare_attachment_bytes
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc, decode_rpc

MAX_BYTES = 2 * 1024 * 1024
TYPES = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
PREFIX = '凡思记录照片-'


def _scope(identity):
    return {'actor_id': identity.actor_id, 'endpoint': identity.endpoint,
            'epoch': identity.epoch, 'server_instance_id': identity.server_instance_id, 'authorized': True}


def _reference(value):
    if type(value) is not int or value == 0 or abs(value) >= 2**53:
        raise LocalDataError('validation', '照片编号无效，请重新拍摄。')
    return value


def _asset_key(reference):
    return ['record-photo-asset11839', reference]


def _image(value):
    if type(value) is not dict or set(value) - {'name', 'media_type', 'data_base64', 'size_bytes'}:
        raise LocalDataError('validation', '照片格式无效，请重新拍摄。')
    media, encoded, declared = value.get('media_type'), value.get('data_base64'), value.get('size_bytes')
    if media not in TYPES or type(encoded) is not str or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
        raise LocalDataError('validation', '请选择不超过2 MB的照片。')
    try:
        raw = base64.b64decode(encoded, validate=True)
        if type(declared) is not int or not 0 < declared <= MAX_BYTES or len(raw) != declared:
            raise ValueError
        attachment = prepare_attachment_bytes('record-photo' + TYPES[media], raw)
        if not attachment.is_image or attachment.media_type != media:
            raise ValueError
    except (ValueError, TypeError):
        raise LocalDataError('validation', '照片不完整或格式不符合要求，请重新拍摄。') from None
    return {'media_type': media, 'data_base64': encoded, 'size_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def _public(reference, image):
    return {'file_id': reference, 'media_type': image['media_type'], 'size_bytes': image['size_bytes'],
            'src': 'data:' + image['media_type'] + ';base64,' + image['data_base64']}


def _rpc(local, identity, method, args, request_id):
    local._guard(identity)
    result = local.cloud_transport('POST', '/v1/rpc/files/' + method,
        json={'args': encode_rpc(args), 'kwargs': {}, 'request_id': request_id}, expected_identity=_scope(identity))
    local._guard(identity)
    if type(result) is not dict or set(result) != {'result'}:
        raise LocalDataError('sync_protocol', '照片同步结果尚未确认，请稍后重试。')
    return decode_rpc(result['result'])


def stage_record_photo(local, identity, image):
    checked = _image(image)
    identifier = uuid4()
    reference = -(identifier.int % (2**52 - 1) + 1)
    value = {**checked, 'upload_operation_id': str(identifier), 'name': PREFIX + identifier.hex + TYPES[checked['media_type']]}
    local._guard(identity)
    stored = local.store.put_if_absent(identity, 'meta', _asset_key(reference), value)
    if stored != value:
        raise LocalDataError('storage', '照片暂未保存，请重新拍摄。')
    local._guard(identity)
    return _public(reference, stored)


def get_record_photo(local, identity, reference):
    reference = _reference(reference)
    local._guard(identity)
    image = local.store.get(identity, 'meta', _asset_key(reference))
    if image is not None:
        local._guard(identity)
        return _public(reference, image)
    if reference < 0:
        mapped = local.store.get(identity, 'mapping', ['record-photo-file11839', reference])
        if not mapped:
            raise LocalDataError('local_not_found', '这张照片尚未保存在本机，请重新拍摄。')
        remote = _reference(mapped['file_id'])
    else:
        remote = reference
    result = _rpc(local, identity, 'get_file', [identity.actor_id, remote], str(uuid4()))
    if (type(result) is not dict or result.get('id') != remote or result.get('user_id') != identity.actor_id
            or type(result.get('content')) is not bytes):
        raise LocalDataError('sync_protocol', '照片暂时无法读取，请稍后重试。')
    checked = _image({'name': result.get('original_name', ''), 'media_type': result.get('media_type'),
        'size_bytes': result.get('size_bytes'), 'data_base64': base64.b64encode(result['content']).decode('ascii')})
    if checked['sha256'] != result.get('sha256'):
        raise LocalDataError('sync_protocol', '照片校验未通过，请重新读取。')
    local._guard(identity)
    local.store.put(identity, 'meta', _asset_key(remote), checked)
    return _public(reference, checked)


def _uses_photo(value, identifiers):
    if not isinstance(value, dict):
        return False
    details = value.get('details')
    references = details.get('photo_file_ids') if isinstance(details, dict) else None
    return isinstance(references, list) and any(type(item) is int and item in identifiers for item in references)


def discard_record_photo(local, identity, reference):
    reference = _reference(reference)
    if reference > 0:
        return {'removed': False, 'saved_photo': True}
    local._guard(identity)
    # Retain receipt -> store lock order. These checks are local only; neither
    # lock is held across upload/network I/O. A later edit can hide a photo in
    # the current projection while an earlier queued create still needs it.
    with local._record_receipt_lock, local.store._db():
        mapped = local.store.get(identity, 'mapping', ['record-photo-file11839', reference], {})
        identifiers = {reference, mapped.get('file_id')}
        resources = local._project(identity, {'life_records'})
        rows = resources.get('life_records', [])
        if isinstance(rows, dict):
            rows = rows.get('items', [])
        if any(_uses_photo(row, identifiers) for row in rows):
            return {'removed': False, 'in_use': True}
        # Default operations excludes applied/discarded rows, but includes all
        # queued, uncertain/frozen, conflict, rejected and confirmed intents.
        for operation in local.store.operations(identity):
            if operation.get('resource') != 'life_records':
                continue
            values = [operation.get(name) for name in ('payload', 'wire_payload',
                'local_before', 'local_result', 'result_entity')]
            values += [value.get('changes') for value in values if isinstance(value, dict)]
            if any(_uses_photo(value, identifiers) for value in values):
                return {'removed': False, 'in_use': True}
        local._guard(identity)
        local.store.delete(identity, 'meta', _asset_key(reference))
    return {'removed': True}


def wire_record_photos(local, identity, operation, body):
    """Upload only first-attempt refs; the caller freezes the returned payload.

    The original encrypted local operation remains unchanged. Each photo has a
    stable RPC UUID, so a lost upload response retries the same receipt. Reuse
    the positive mapping thereafter, including when a record has a conflict.
    """
    if operation['action'] not in {'health.records.add', 'expansion.records.update'}:
        return body
    target = body if operation['action'] == 'health.records.add' else body.get('changes', {})
    details = target.get('details')
    if not isinstance(details, dict) or 'photo_file_ids' not in details:
        return body
    references = details['photo_file_ids']
    if (type(references) is not list or len(references) > 4
            or any(type(value) is not int or value == 0 or abs(value) >= 2**53 for value in references)
            or len(set(references)) != len(references)):
        raise LocalDataError('validation', '照片信息无效，请重新选择。')
    result = deepcopy(body)
    output = result if operation['action'] == 'health.records.add' else result['changes']
    remote_ids = []
    for reference in references:
        local._guard(identity)
        if reference > 0:
            remote_ids.append(reference)
            continue
        image = local.store.get(identity, 'meta', _asset_key(reference))
        mapped = local.store.get(identity, 'mapping', ['record-photo-file11839', reference])
        if mapped:
            if image is not None and mapped.get('sha256') != image.get('sha256'):
                raise LocalDataError('sync_protocol', '照片同步校验失败，请保留原记录后重试。')
            identifier = _reference(mapped.get('file_id'))
            if identifier <= 0:
                raise LocalDataError('sync_protocol', '照片同步编号无效。')
        else:
            if image is None:
                raise LocalDataError('dependency_pending', '照片尚未保存在本机，请重新选择后保存。')
            identifier = _rpc(local, identity, 'upload_bytes',
                [identity.actor_id, image['name'], base64.b64decode(image['data_base64'], validate=True)],
                str(uuid5(UUID(image['upload_operation_id']), 'record-photo-upload11839')))
            if type(identifier) is not int or not 0 < identifier < 2**53:
                raise LocalDataError('sync_protocol', '照片上传结果尚未确认，请稍后重试。')
            local._guard(identity)
            local.store.put(identity, 'mapping', ['record-photo-file11839', reference],
                {'file_id': identifier, 'sha256': image['sha256']})
            local.store.put(identity, 'meta', _asset_key(identifier), image)
        remote_ids.append(identifier)
    output['details']['photo_file_ids'] = remote_ids
    return result
