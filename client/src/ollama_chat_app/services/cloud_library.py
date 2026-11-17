"""Cloud counterpart of :mod:`knowledge_library` for the 2.1 client.

The adapter intentionally implements the small interface consumed by
``KnowledgeAwareProvider``.  It never accepts a filesystem path: the server
resolves document ids against its configured, read-only library root.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from .cloud_client import CloudAPIClient, CloudAPIError
from .knowledge_library import LibraryError

LibraryKind = Literal["knowledge", "language"]
_ID_RE = re.compile(r"^D-[A-Za-z0-9-]{1,159}$")


def _kind(kind: str) -> LibraryKind:
    if kind not in {"knowledge", "language"}:
        raise LibraryError("资料类型无效")
    return kind  # type: ignore[return-value]


def _document_id(value: str) -> str:
    value = str(value or "").strip()
    if not _ID_RE.fullmatch(value):
        raise LibraryError("资料编号无效")
    return value


class CloudKnowledgeLibrary:
    """Read-only API adapter using an already authenticated CloudAPIClient."""

    def __init__(self, client: CloudAPIClient) -> None:
        self.client = client

    def _get(self, path: str, *, params: dict[str, Any] | list[tuple[str, str]] | None = None):
        try:
            return self.client.request("GET", path, params=params)
        except CloudAPIError as error:
            # Do not let transport or server details enter an AI prompt.
            raise LibraryError("云端资料库暂时不可用") from error

    def catalog(self, kind: LibraryKind, *, limit: int = 200, offset: int = 0):
        kind = _kind(kind)
        result = self._get(f"/v1/library/catalog/{kind}", params={"limit": limit, "offset": offset})
        return list(result.get("documents", [])) if isinstance(result, dict) else []

    def search(self, kind: LibraryKind, query: str, *, limit: int = 5):
        kind = _kind(kind)
        result = self._get(
            "/v1/library/search",
            params={"kind": kind, "q": str(query or "")[:2000], "limit": limit},
        )
        return list(result.get("hits", [])) if isinstance(result, dict) else []

    def document(self, kind: LibraryKind, document_id: str):
        kind = _kind(kind)
        document_id = _document_id(document_id)
        result = self._get(f"/v1/library/document/{kind}/{document_id}")
        if not isinstance(result, dict):
            raise LibraryError("云端资料目录响应无效")
        return result

    def open_knowledge_card(self, document_id: str, section_id: str | None = None):
        return self._open_card("knowledge", document_id, section_id)

    def open_language_card(self, document_id: str, section_id: str | None = None):
        return self._open_card("language", document_id, section_id)

    def _open_card(self, kind: LibraryKind, document_id: str, section_id: str | None):
        kind = _kind(kind)
        document_id = _document_id(document_id)
        params = {"section_id": section_id} if section_id else None
        result = self._get(f"/v1/library/card/{kind}/{document_id}", params=params)
        if not isinstance(result, dict):
            raise LibraryError("云端资料卡响应无效")
        return result

    def open_source(self, document_id: str, *, anchors=()):
        document_id = _document_id(document_id)
        safe_anchors = [str(anchor)[:160] for anchor in anchors if str(anchor).strip()][:20]
        params = [("anchor", anchor) for anchor in safe_anchors]
        result = self._get(f"/v1/library/source/{document_id}", params=params or None)
        if not isinstance(result, dict):
            raise LibraryError("云端原文响应无效")
        return result


__all__ = ["CloudKnowledgeLibrary"]
