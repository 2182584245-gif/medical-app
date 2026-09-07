"""Bounded JSON codec shared by the desktop and the authenticated RPC server.

Only explicitly imported value types are admitted. No pickle, eval, dynamic
imports, arbitrary class names, filesystem paths or executable payloads exist.
The reserved tag is '$__type'. Ordinary mappings may not use that key.
"""

from __future__ import annotations

import base64
import math
from dataclasses import fields
from datetime import date, datetime

from ..data.database import Conversation, Message, User
from .chat import ChatTurn, PendingExchange
from .chat_attachments import ChatAttachment

TAG = "$__type"
MAX_DEPTH = 32
MAX_ITEMS = 100_000
MAX_BINARY_BYTES = 20 * 1024 * 1024
MAX_STRING_CHARS = 32 * 1024 * 1024
_TYPES = {cls.__name__: cls for cls in (
    User, Conversation, Message, ChatTurn, PendingExchange, ChatAttachment,
)}


class CloudCodecError(ValueError):
    def __init__(self) -> None:
        super().__init__("云端数据格式不受支持或超过安全上限。")


class _Budget:
    def __init__(self):
        self.items = MAX_ITEMS
        self.binary = MAX_BINARY_BYTES
        self.characters = MAX_STRING_CHARS

    def item(self, depth):
        self.items -= 1
        if depth > MAX_DEPTH or self.items < 0:
            raise CloudCodecError

    def string(self, value):
        self.characters -= len(value)
        if self.characters < 0:
            raise CloudCodecError


def _encode(value, budget, depth):
    budget.item(depth)
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if value_type is str:
        budget.string(value)
        return value
    if value_type is int:
        if not -(2**63) <= value < 2**63:
            raise CloudCodecError
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise CloudCodecError
        return value
    if value_type in (datetime, date):
        return {TAG: value_type.__name__, "value": value.isoformat()}
    if value_type is bytes:
        budget.binary -= len(value)
        if budget.binary < 0:
            raise CloudCodecError
        return {TAG: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if value_type in (list, tuple, set):
        sequence = [_encode(item, budget, depth + 1) for item in value]
        return sequence if value_type is list else {TAG: value_type.__name__, "items": sequence}
    if value_type is dict:
        if TAG in value or any(type(key) is not str for key in value):
            raise CloudCodecError
        for key in value:
            budget.string(key)
        return {key: _encode(item, budget, depth + 1) for key, item in value.items()}
    if value_type in _TYPES.values():
        return {TAG: value_type.__name__, "fields": {
            field.name: _encode(getattr(value, field.name), budget, depth + 1)
            for field in fields(value_type)
        }}
    raise CloudCodecError


def _decode(value, budget, depth):
    budget.item(depth)
    value_type = type(value)
    if value_type is list:
        return [_decode(item, budget, depth + 1) for item in value]
    if value_type is not dict:
        if value is None or value_type in (str, bool, int, float):
            return _encode(value, budget, depth)
        raise CloudCodecError
    if TAG not in value:
        if any(type(key) is not str for key in value):
            raise CloudCodecError
        for key in value:
            budget.string(key)
        return {key: _decode(item, budget, depth + 1) for key, item in value.items()}
    name = value[TAG]
    if not isinstance(name, str):
        raise CloudCodecError
    if name in {"datetime", "date", "bytes"}:
        if set(value) != {TAG, "value"} or type(value["value"]) is not str:
            raise CloudCodecError
        text = value["value"]
        budget.string(text)
        if name == "bytes":
            if len(text) > ((budget.binary + 2) // 3) * 4:
                raise CloudCodecError
            result = base64.b64decode(text, validate=True)
            budget.binary -= len(result)
            if budget.binary < 0:
                raise CloudCodecError
            return result
        if len(text) > 64:
            raise CloudCodecError
        return datetime.fromisoformat(text) if name == "datetime" else date.fromisoformat(text)
    if name in {"tuple", "set"}:
        if set(value) != {TAG, "items"} or type(value["items"]) is not list:
            raise CloudCodecError
        result = [_decode(item, budget, depth + 1) for item in value["items"]]
        return tuple(result) if name == "tuple" else set(result)
    if name in _TYPES:
        cls = _TYPES[name]
        if set(value) != {TAG, "fields"} or type(value["fields"]) is not dict:
            raise CloudCodecError
        if set(value["fields"]) != {field.name for field in fields(cls)}:
            raise CloudCodecError
        values = {key: _decode(item, budget, depth + 1)
                  for key, item in value["fields"].items()}
        return cls(**values)
    raise CloudCodecError


def encode_rpc(value):
    try:
        return _encode(value, _Budget(), 0)
    except Exception:
        raise CloudCodecError from None


def decode_rpc(value):
    try:
        return _decode(value, _Budget(), 0)
    except Exception:
        raise CloudCodecError from None
