"""Authenticated, read-only HTTP access to the bundled card library.

The desktop client can use these endpoints when it is configured for the
cloud route.  The API deliberately exposes document ids and bounded text only;
callers cannot submit paths, SQL, glob patterns, or arbitrary file names.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ollama_chat_app.services.knowledge_library import KnowledgeLibrary, LibraryError

from .platform_auth import PlatformAuth, PlatformPrincipal, authentication_dependency

LibraryKind = Literal["knowledge", "language"]


def _library_error(_error: LibraryError) -> HTTPException:
    # Do not disclose local paths, catalog parse details, or filesystem state.
    return HTTPException(status_code=404, detail="资料不存在或输入无效")


def create_library_router(
    auth: PlatformAuth,
    library: KnowledgeLibrary,
) -> APIRouter:
    """Create the authenticated library router for one application instance."""

    router = APIRouter(prefix="/v1/library", tags=["library"])
    principal_dependency = Depends(authentication_dependency(auth))

    @router.get("/catalog/{kind}")
    def catalog(
        kind: LibraryKind,
        # Keep one response comfortably below the platform transport budget;
        # callers can page through larger catalogues with ``offset``.
        limit: Annotated[int, Query(ge=1, le=200)] = 200,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        _principal: PlatformPrincipal = principal_dependency,
    ):
        try:
            return {
                "kind": kind,
                "offset": offset,
                "limit": limit,
                "documents": library.catalog(kind, limit=limit, offset=offset),
            }
        except LibraryError as error:
            raise _library_error(error) from None

    @router.get("/search")
    def search(
        kind: LibraryKind,
        q: Annotated[str, Query(min_length=1, max_length=2000)],
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
        _principal: PlatformPrincipal = principal_dependency,
    ):
        try:
            # ``KnowledgeLibrary`` uses absolute paths internally for the
            # desktop reader.  Never send those paths over the network.
            raw_hits = library.search(kind, q, limit=limit)
            hits = [
                {
                    "document_id": str(item.get("document_id", "")),
                    "title": str(item.get("title", "")),
                    "kind": kind,
                    "score": int(item.get("score", 0)),
                    "matched_entries": [
                        str(value)[:500]
                        for value in item.get("matched_entries", [])
                        if str(value).strip()
                    ],
                    "status": str(item.get("status", "unknown")),
                }
                for item in raw_hits
            ]
            return {"kind": kind, "query": q[:2000], "hits": hits}
        except LibraryError as error:
            raise _library_error(error) from None

    @router.get("/document/{kind}/{document_id}")
    def document(
        kind: LibraryKind,
        document_id: str,
        _principal: PlatformPrincipal = principal_dependency,
    ):
        try:
            return library.document(kind, document_id)
        except LibraryError as error:
            raise _library_error(error) from None

    @router.get("/card/{kind}/{document_id}")
    def card(
        kind: LibraryKind,
        document_id: str,
        section_id: Annotated[str | None, Query(max_length=160)] = None,
        _principal: PlatformPrincipal = principal_dependency,
    ):
        try:
            if kind == "knowledge":
                result = library.open_knowledge_card(document_id, section_id)
            else:
                result = library.open_language_card(document_id, section_id)
            # Filesystem paths are an internal implementation detail and must
            # never cross the public API boundary.
            result.pop("path", None)
            return result
        except LibraryError as error:
            raise _library_error(error) from None

    @router.get("/source/{document_id}")
    def source(
        document_id: str,
        anchor: Annotated[list[str] | None, Query(max_length=20)] = None,
        _principal: PlatformPrincipal = principal_dependency,
    ):
        try:
            result = library.open_source(document_id, anchors=anchor or ())
            # Do not disclose the container's absolute library path.
            result.pop("path", None)
            return result
        except LibraryError as error:
            raise _library_error(error) from None

    return router


__all__ = ["create_library_router"]
