"""Read-only access to the bundled knowledge and language card library.

The language model is deliberately not allowed to open arbitrary paths.  This
service resolves a document id through the published catalog and then reads a
bounded file underneath the configured library root.  Catalog paths generated
on another machine are accepted only as hints; in the portable release the
file name is resolved against the bundled ``知识资料卡库``/``语言资料卡库`` and
``原文库`` directories.

This module does not call a model or an external service.  It is intended to
be used by the application's library tools (search catalog -> open card ->
open source paragraphs) and is safe to use from a worker thread.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from ..paths import application_dir

LibraryKind = Literal["knowledge", "language"]

MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_CARD_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RESULT_CHARS = 80_000
MAX_SEARCH_QUERY_CHARS = 2_000
MAX_REVIEW_BYTES = 2 * 1024 * 1024

# Card ids are deliberately parsed instead of treated as arbitrary text.  A
# knowledge card may mention the same id in its directory, a detailed entry,
# and a source cross-reference; callers can therefore ask for a C/K/P unit
# without receiving an unrelated part of the document.
_ENTRY_ID_RE = re.compile(
    r"(?P<doc>D-[A-Za-z0-9-]+?)(?P<parts>(?:-C\d+)?(?:-K\d+)?(?:-P\d+)?(?:-(?:IMG|FORM|R)\d+)?)\b"
)
_SECTION_HEAD_RE = re.compile(r"(?:层次编号|知识条目编号\+知识名)[:：]?\s*(D-[A-Za-z0-9-]+)", re.I)


class LibraryError(ValueError):
    """Raised for an invalid id, unavailable catalog, or unsafe file."""


@dataclass(frozen=True, slots=True)
class LibraryHit:
    document_id: str
    title: str
    kind: LibraryKind
    score: int
    matched_entries: tuple[str, ...]
    card_path: str
    source_path: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "kind": self.kind,
            "score": self.score,
            "matched_entries": list(self.matched_entries),
            "card_path": self.card_path,
            "source_path": self.source_path,
            "status": self.status,
        }


def _normalise_section_id(document_id: str, section_id: str) -> str:
    """Return a suffix such as ``C2-K1-P12`` for a card lookup.

    The model is allowed to return either the short suffix or the complete
    document id.  Only C/K/P/IMG/FORM/R components are accepted; this keeps a
    prompt-injected path or URL from reaching the file reader.
    """

    value = str(section_id or "").strip()
    if value.startswith(document_id + "-L-"):
        value = value[len(document_id) + 3 :]
    elif value.startswith(document_id + "-"):
        value = value[len(document_id) + 1 :]
    if not re.fullmatch(r"[CTEAM]\d+(?:-K\d+)?(?:-P\d+)?(?:-(?:IMG|FORM|R)\d+)?", value):
        raise LibraryError("资料卡定位编号无效")
    return value


def _section_level(value: str) -> int:
    """Return the hierarchy level (C=1, K=2, P/media/reference=3)."""

    if "-K" in value:
        return 2
    if "-P" in value or any(f"-{kind}" in value for kind in ("-IMG", "-FORM", "-R")):
        return 3
    return 1


def _entry_ids(line: str, document_id: str) -> list[str]:
    """Extract section ids belonging to ``document_id`` from one card line."""

    found: list[str] = []
    pattern = re.compile(
        re.escape(document_id)
        + r"(?:-L)?(?P<parts>(?:-[CTEAM]\d+)?(?:-K\d+)?(?:-P\d+)?(?:-(?:IMG|FORM|R)\d+)?)\b"
    )
    for match in pattern.finditer(str(line)):
        suffix = match.group("parts").lstrip("-")
        if suffix and suffix not in found:
            found.append(suffix)
    return found


class _TextHTMLParser(HTMLParser):
    """Small dependency-free HTML-to-text reader preserving paragraph ids."""

    _BLOCK = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"}:
            self._skip += 1
            return
        if self._skip:
            return
        attributes = dict(attrs)
        marker = attributes.get("id") or attributes.get("data-paragraph")
        if marker and str(marker).strip():
            self.parts.append(f"[{marker.strip()}] ")
        elif tag in self._BLOCK and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"}:
            self._skip = max(0, self._skip - 1)
            return
        if not self._skip and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def _tokens(value: str) -> set[str]:
    """Tokenise Chinese text by bigrams and latin/numeric words."""

    value = str(value or "").casefold()
    result = set(re.findall(r"[a-z0-9][a-z0-9._-]{1,}", value))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", value):
        result.update(phrase[i : i + 2] for i in range(len(phrase) - 1))
        result.update(phrase)
    return result


def _clean_id(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 160 or any(ch in value for ch in ("/", "\\", "\x00")):
        raise LibraryError("资料编号无效")
    return value


class KnowledgeLibrary:
    """A bounded, read-only view of the shipped card/source library.

    ``root`` may be supplied by the desktop bootstrap or through
    ``HEALTH_LIFE_LIBRARY_ROOT``.  If omitted, bundled paths are tried first,
    followed by the development data directory used by the initial library.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = self._find_root(root)
        self._catalog_cache: dict[LibraryKind, tuple[dict[str, Any], ...]] = {}
        self._review_cache: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def _find_root(root: str | Path | None) -> Path:
        candidates: list[Path] = []
        if root is not None:
            candidates.append(Path(root).expanduser())
        env_root = os.environ.get("HEALTH_LIFE_LIBRARY_ROOT")
        if env_root:
            candidates.append(Path(env_root).expanduser())
        # ``services/knowledge_library.py`` is one directory deeper than the
        # usual package modules; use the shared resolver so source and frozen
        # builds agree on the folder beside the executable.
        module_root = application_dir()
        bundled_root = getattr(sys, "_MEIPASS", None)
        if bundled_root:
            candidates.append(Path(bundled_root) / "assets" / "ai_library")
        candidates.extend(
            (
                module_root / "library",
                module_root / "assets" / "ai_library",
                module_root / "resources" / "library",
                module_root / "data" / "library",
            )
        )
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_dir() and any(
                (resolved / name).exists()
                for name in ("知识资料总目录.json", "知识资料总目录.txt", "知识资料卡库")
            ):
                return resolved
        # Keep the selected location deterministic; an operation then raises a
        # useful LibraryError instead of silently reading outside the app.
        return candidates[0].expanduser().resolve()

    def _catalog_file(self, kind: LibraryKind, suffix: str = ".json") -> Path:
        name = "知识资料总目录" if kind == "knowledge" else "语言资料总目录"
        path = self.root / f"{name}{suffix}"
        if path.is_file() and not path.is_symlink():
            return path
        # A release may keep catalogs under a metadata subfolder.
        path = self.root / "catalog" / f"{name}{suffix}"
        return path

    def _read_bounded(self, path: Path, limit: int) -> str:
        path = self._safe_path(path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise LibraryError("资料文件不可读取") from exc
        if size > limit:
            raise LibraryError("资料文件超过安全读取上限")
        try:
            return path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise LibraryError("资料文件无法按 UTF-8 读取") from exc

    def _load_review_metadata(self) -> dict[str, dict[str, Any]]:
        """Load per-document review boundaries without treating them as proof.

        The review file is deliberately separate from the cards: it records
        the user's review authorization and the intended use boundary, so the
        application can show provenance without rewriting the original cards.
        Missing metadata keeps the library usable for older bundles.
        """

        if self._review_cache is not None:
            return self._review_cache
        path = self.root / "资料库人工审核状态.json"
        try:
            raw = self._read_bounded(path, MAX_REVIEW_BYTES)
            payload = json.loads(raw)
        except (LibraryError, ValueError, TypeError):
            self._review_cache = {}
            return self._review_cache
        rows = payload.get("documents", []) if isinstance(payload, dict) else []
        result: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    document_id = _clean_id(row.get("document_id", ""))
                except LibraryError:
                    continue
                result[document_id] = {
                    str(key): value for key, value in row.items()
                    if key != "document_id"
                }
        self._review_cache = result
        return result

    def _safe_path(self, path: Path) -> Path:
        try:
            candidate = path.expanduser().resolve(strict=True)
            base = self.root.resolve(strict=True)
        except OSError as exc:
            raise LibraryError("资料文件不存在") from exc
        if candidate == base:
            raise LibraryError("资料文件路径无效")
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise LibraryError("资料文件路径超出资料库") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise LibraryError("资料文件不是普通文件")
        return candidate

    def _load_catalog(self, kind: LibraryKind) -> tuple[dict[str, Any], ...]:
        if kind not in ("knowledge", "language"):
            raise LibraryError("资料类型必须是 knowledge 或 language")
        if kind in self._catalog_cache:
            return self._catalog_cache[kind]
        json_path = self._catalog_file(kind)
        documents: Any = None
        if json_path.is_file():
            raw = self._read_bounded(json_path, MAX_CATALOG_BYTES)
            try:
                documents = json.loads(raw).get("documents", [])
            except (ValueError, AttributeError) as exc:
                raise LibraryError("资料目录 JSON 无效") from exc
        else:
            txt_path = self._catalog_file(kind, ".txt")
            if not txt_path.is_file():
                label = "知识" if kind == "knowledge" else "语言"
                raise LibraryError(f"未找到{label}资料目录")
            documents = self._parse_text_catalog(self._read_bounded(txt_path, MAX_CATALOG_BYTES))
        if not isinstance(documents, list):
            raise LibraryError("资料目录缺少 documents 列表")
        cleaned: list[dict[str, Any]] = []
        review_metadata = self._load_review_metadata()
        for item in documents:
            if not isinstance(item, dict):
                continue
            try:
                doc_id = _clean_id(item.get("document_id", ""))
            except LibraryError:
                continue
            # Copy only catalog values; callers must not mutate the cache.
            merged = {**item, "document_id": doc_id}
            review_row = review_metadata.get(doc_id, {})
            review_key = "knowledge_card" if kind == "knowledge" else "language_card"
            review = review_row.get(review_key, {})
            if isinstance(review, dict):
                merged["review"] = dict(review)
            source_review = review_row.get("original_source", {})
            if isinstance(source_review, dict):
                merged["source_review"] = dict(source_review)
            cleaned.append(merged)
        self._catalog_cache[kind] = tuple(cleaned)
        return self._catalog_cache[kind]

    @staticmethod
    def _parse_text_catalog(text: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "【", "[")):
                continue
            match = re.match(r"(?:编号[:：]\s*)?([A-Za-z0-9-]+)(?:[（(]([^）)]*)[）)])?", line)
            if not match:
                continue
            result.append({
                "document_id": match.group(1),
                "title": (match.group(2) or "").strip(),
                "directory": [line],
            })
        return result

    def _resolve_catalog_path(self, item: dict[str, Any], kind: LibraryKind, field: str) -> Path:
        raw = str(item.get(field, "") or "").strip()
        basename = Path(raw).name if raw else ""
        folder = "知识资料卡库" if kind == "knowledge" else "语言资料卡库"
        if field == "source_path" or field == "original_html":
            folder = "原文库"
        candidates: list[Path] = []
        if raw:
            supplied = Path(raw)
            if supplied.is_absolute():
                candidates.append(supplied)
            else:
                candidates.append(self.root / supplied)
        if basename:
            candidates.append(self.root / folder / basename)
        doc_id = str(item.get("document_id", ""))
        if field in {"card_path", "knowledge_card", "language_card"}:
            suffix = "_知识资料卡.txt" if kind == "knowledge" else "-L_语言资料卡.txt"
            candidates.append(self.root / folder / f"{doc_id}{suffix}")
        else:
            candidates.append(self.root / folder / f"{doc_id}.html")
        for candidate in candidates:
            try:
                return self._safe_path(candidate)
            except LibraryError:
                continue
        raise LibraryError("资料卡或原文不存在")

    def _item(self, kind: LibraryKind, document_id: str) -> dict[str, Any]:
        document_id = _clean_id(document_id)
        for item in self._load_catalog(kind):
            if item.get("document_id") == document_id:
                return item
        raise LibraryError("未找到该资料编号")

    def search(self, kind: LibraryKind, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Search titles and catalog entries; no card text is loaded here."""

        query = str(query or "").strip()[:MAX_SEARCH_QUERY_CHARS]
        if not query:
            return []
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 5
        query_tokens = _tokens(query)
        # A document explicitly named by the user must survive the candidate
        # limit even when common Chinese words score higher in other entries.
        # Match complete id tokens: D-A-FYEY-1 must not select D-A-FYEY-157,
        # and a longer/invalid token must not accidentally select its prefix.
        explicit_ids = {
            value.casefold() for value in re.findall(
                r"(?<![a-z0-9_-])d-[a-z0-9]+(?:-[a-z0-9]+)*(?![a-z0-9_-])",
                query, flags=re.I,
            )
        }
        scored: list[LibraryHit] = []
        for item in self._load_catalog(kind):
            title = str(item.get("title", ""))
            entries = item.get("directory", [])
            if not isinstance(entries, list):
                entries = []
            searchable = " ".join([title, str(item.get("document_id", "")), *map(str, entries)])
            matched = tuple(str(entry)[:500] for entry in entries if query_tokens & _tokens(entry))
            score = len(query_tokens & _tokens(title)) * 6
            score += len(query_tokens & _tokens(str(item.get("document_id", "")))) * 3
            score += sum(len(query_tokens & _tokens(str(entry))) for entry in entries)
            if score <= 0 and query.casefold() not in searchable.casefold():
                continue
            try:
                card = self._resolve_catalog_path(item, kind, "card_path")
            except LibraryError:
                card = self.root / ("知识资料卡库" if kind == "knowledge" else "语言资料卡库") / ""
            try:
                source = self._resolve_catalog_path(item, kind, "original_html")
            except LibraryError:
                source = self.root / "原文库" / ""
            scored.append(LibraryHit(
                document_id=str(item["document_id"]), title=title, kind=kind,
                score=max(score, 1), matched_entries=matched[:20],
                card_path=str(card), source_path=str(source),
                status=str(item.get("status", "unknown")),
            ))
        ordered = sorted(scored, key=lambda row: (
            row.document_id.casefold() not in explicit_ids, -row.score, row.document_id,
        ))
        return [hit.as_dict() for hit in ordered[:limit]]

    def catalog(
        self,
        kind: LibraryKind,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return bounded directory metadata without opening card contents."""

        try:
            limit = max(1, min(int(limit), 2_000))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            raise LibraryError("资料目录分页参数无效") from None
        entries = self._load_catalog(kind)
        result: list[dict[str, Any]] = []
        for item in entries[offset : offset + limit]:
            result.append(
                {
                    "document_id": str(item["document_id"]),
                    "title": str(item.get("title", "")),
                    "kind": kind,
                    "directory": [
                        str(value)[:2_000]
                        for value in item.get("directory", [])
                        if str(value).strip()
                    ],
                    "status": str(item.get("status", "unknown")),
                    "review": dict(item.get("review", {}))
                    if isinstance(item.get("review"), dict) else {},
                }
            )
        return result

    def _card_result(
        self, kind: LibraryKind, document_id: str, section_id: str | None
    ) -> dict[str, Any]:
        item = self._item(kind, document_id)
        path = self._resolve_catalog_path(item, kind, "card_path")
        text = self._read_bounded(path, MAX_CARD_BYTES)
        selected = text
        section_fallback = False
        if section_id:
            section_id = _normalise_section_id(document_id, section_id)
            lines = text.splitlines()
            wanted_level = _section_level(section_id)
            blocks: list[str] = []
            i = 0
            while i < len(lines):
                ids = _entry_ids(lines[i], document_id)
                if section_id not in ids:
                    i += 1
                    continue
                block = [lines[i]]
                j = i + 1
                while j < len(lines):
                    next_ids = _entry_ids(lines[j], document_id)
                    stop = any(
                        candidate != section_id
                        and _section_level(candidate) <= wanted_level
                        for candidate in next_ids
                    )
                    if wanted_level >= 2 and j > i and "【知识条目信息】" in lines[j]:
                        stop = True
                    if stop:
                        break
                    block.append(lines[j])
                    j += 1
                blocks.extend(block)
                i = max(j, i + 1)
            selected = "\n".join(blocks).strip()
            if not selected:
                # Early language cards may keep their T/E/A/M directory only
                # in the JSON catalog while the card body stores the related
                # knowledge references.  Do not lose the language-polishing
                # stage in that case: return the bounded full language card
                # and make the fallback explicit to the caller.
                if kind == "language":
                    selected = text
                    section_fallback = True
                else:
                    raise LibraryError("该资料卡中未找到指定条目")
        return {
            "document_id": str(item["document_id"]),
            "title": str(item.get("title", "")),
            "kind": kind,
            "section_id": section_id,
            "path": str(path),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": selected[:MAX_RESULT_CHARS],
            "truncated": len(selected) > MAX_RESULT_CHARS,
            "section_fallback": section_fallback,
            "status": str(item.get("status", "unknown")),
            "review": dict(item.get("review", {}))
            if isinstance(item.get("review"), dict) else {},
        }

    def open_knowledge_card(
        self, document_id: str, section_id: str | None = None
    ) -> dict[str, Any]:
        return self._card_result("knowledge", document_id, section_id)

    def open_language_card(self, document_id: str, section_id: str | None = None) -> dict[str, Any]:
        return self._card_result("language", document_id, section_id)

    def open_source(
        self,
        document_id: str,
        *,
        anchors: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        item = self._item("knowledge", document_id)
        path = self._resolve_catalog_path(item, "knowledge", "original_html")
        raw = self._read_bounded(path, MAX_SOURCE_BYTES)
        parser = _TextHTMLParser()
        try:
            parser.feed(raw)
            text = parser.text()
        except (ValueError, TypeError):
            text = raw
        selected = text
        safe_anchors = [_clean_id(anchor) for anchor in (anchors or ()) if str(anchor).strip()]
        if safe_anchors:
            lines = text.splitlines()
            def anchor_matches(line: str, anchor: str) -> bool:
                exact = re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(anchor)}(?![A-Za-z0-9])",
                    line,
                )
                if exact:
                    return True
                match = re.fullmatch(r"([A-Za-z]+)(\d+)", anchor)
                if not match:
                    return False
                return bool(re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(match.group(1))}0*{int(match.group(2))}(?![A-Za-z0-9])",
                    line,
                    re.I,
                ))

            matches = [
                line for line in lines
                if any(anchor_matches(line, anchor) for anchor in safe_anchors)
            ]
            if matches:
                selected = "\n".join(matches)
        return {
            "document_id": str(item["document_id"]),
            "title": str(item.get("title", "")),
            "path": str(path),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "anchors": safe_anchors,
            "text": selected[:MAX_RESULT_CHARS],
            "truncated": len(selected) > MAX_RESULT_CHARS,
            "review": dict(item.get("source_review", {}))
            if isinstance(item.get("source_review"), dict) else {},
        }

    def document(self, kind: LibraryKind, document_id: str) -> dict[str, Any]:
        """Return safe metadata and directory entries without opening a card."""

        item = self._item(kind, document_id)
        result = {
            "document_id": str(item["document_id"]),
            "title": str(item.get("title", "")),
            "kind": kind,
            "directory": [str(x)[:2000] for x in item.get("directory", []) if str(x).strip()],
            "status": str(item.get("status", "unknown")),
            "review": dict(item.get("review", {}))
            if isinstance(item.get("review"), dict) else {},
        }
        return result


# Alias used by callers that prefer a service-shaped name.
KnowledgeLibraryService = KnowledgeLibrary

__all__ = [
    "KnowledgeLibrary",
    "KnowledgeLibraryService",
    "LibraryError",
    "LibraryHit",
]
