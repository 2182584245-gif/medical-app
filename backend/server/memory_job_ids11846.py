"""Disjoint UUIDv8 partition for internal, append-only memory job receipts.

Public RPC rejects this exact marker before accessing its idempotency cache.
Existing public UUID4 and internal AI/sync UUID5 receipts are unchanged.
"""
import hashlib
from uuid import UUID

MARKER = bytes.fromhex('11846c01')


def receipt_id(job_digest, attempt, phase):
    if len(job_digest) != 64 or attempt not in (1, 2) or phase not in ('claim', 'terminal'):
        raise ValueError('invalid_memory_job_receipt')
    raw = bytearray(hashlib.sha256(f'conversation-index11846:{job_digest}:{attempt}:{phase}'.encode()).digest()[:16])
    raw[:4] = MARKER
    raw[6] = (raw[6] & 15) | 0x80
    raw[8] = (raw[8] & 63) | 0x80
    return str(UUID(bytes=bytes(raw)))


def is_internal_memory_request(value):
    try:
        identifier = UUID(str(value))
        return identifier.version == 8 and identifier.bytes[:4] == MARKER
    except (ValueError, TypeError, AttributeError):
        return False
