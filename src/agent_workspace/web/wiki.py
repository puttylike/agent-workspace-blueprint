"""Private Markdown knowledge reader backed by a rebuildable FTS5 cache.

Markdown files remain authoritative.  SQLite is deliberately limited to search
indexing and can be removed or rebuilt without losing knowledge.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any, Iterator, Mapping

from markdown_it import MarkdownIt
import nh3
import yaml


class WikiError(RuntimeError):
    """Base error for the read-only Wiki source."""


class UnsafeWikiPath(WikiError):
    """Raised when a requested path could leave the configured root."""


class WikiDocumentNotFound(WikiError):
    """Raised when a requested Markdown document does not exist."""


@dataclass(frozen=True, slots=True)
class WikiDocument:
    path: str
    title: str
    markdown: str
    html: str
    metadata: Mapping[str, Any]
    updated_at: str | None

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        value = asdict(self)
        if not include_body:
            value.pop("markdown", None)
            value.pop("html", None)
        return value


@dataclass(frozen=True, slots=True)
class WikiSearchResult:
    path: str
    title: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_SEARCH_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_DANGEROUS_SCHEME_TEXT = re.compile(r"\b(?:javascript|vbscript|data)\s*:", re.IGNORECASE)

# Keep the rendered surface deliberately small. Raw Markdown HTML is disabled
# before this second sanitization pass.
_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "code": {"class"}}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _split_front_matter(source: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(source)
    if not match:
        return {}, source
    try:
        loaded = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        loaded = {}
    metadata = loaded if isinstance(loaded, dict) else {}
    return metadata, source[match.end() :]


def render_markdown(source: str) -> str:
    """Render Markdown with raw HTML disabled and sanitize the result."""

    renderer = MarkdownIt("commonmark", {"html": False, "linkify": False})
    rendered = renderer.render(source)
    cleaned = nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        strip_comments=True,
    )
    # markdown-it intentionally leaves a rejected link target as visible text.
    # Neutralize dangerous scheme spellings as well so neither rendered links
    # nor literal fallback text can later become active through DOM rewriting.
    return _DANGEROUS_SCHEME_TEXT.sub("blocked-scheme:", cleaned)


def _plain_text(rendered_html: str) -> str:
    parser = _TextExtractor()
    parser.feed(rendered_html)
    return unescape(" ".join(parser.parts))


def _title_for(path: Path, metadata: Mapping[str, Any], body: str) -> str:
    explicit = metadata.get("title")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for line in body.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ").title()


class WikiIndex:
    """Read Markdown safely and maintain its disposable FTS5 search index."""

    def __init__(self, knowledge_root: str | Path, database_path: str | Path) -> None:
        self.knowledge_root = Path(knowledge_root)
        self.database_path = Path(database_path)

    def _root(self) -> Path:
        if self.knowledge_root.is_symlink():
            raise UnsafeWikiPath("The configured knowledge root must not be a symlink")
        try:
            root = self.knowledge_root.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise WikiError("The configured knowledge source is unavailable") from exc
        if not root.is_dir():
            raise WikiError("The configured knowledge source is unavailable")
        return root

    @staticmethod
    def _relative_parts(relative_path: str) -> tuple[str, ...]:
        if not relative_path or "\x00" in relative_path or "\\" in relative_path:
            raise UnsafeWikiPath("Invalid Wiki document path")
        parsed = PurePosixPath(relative_path)
        if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
            raise UnsafeWikiPath("Invalid Wiki document path")
        if parsed.suffix.lower() != ".md":
            raise UnsafeWikiPath("Wiki documents must be Markdown files")
        return parsed.parts

    def _safe_document_path(self, relative_path: str) -> tuple[Path, Path]:
        root = self._root()
        parts = self._relative_parts(relative_path)
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise UnsafeWikiPath("Symlinked Wiki paths are not allowed")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise WikiDocumentNotFound("Wiki document not found") from exc
        if not resolved.is_file():
            raise WikiDocumentNotFound("Wiki document not found")
        return root, resolved

    def _iter_paths(self) -> Iterator[tuple[Path, Path]]:
        root = self._root()
        for candidate in sorted(root.rglob("*.md")):
            relative = candidate.relative_to(root)
            try:
                _, safe_path = self._safe_document_path(relative.as_posix())
            except (UnsafeWikiPath, WikiDocumentNotFound):
                continue
            yield root, safe_path

    def read(self, relative_path: str) -> WikiDocument:
        root, path = self._safe_document_path(relative_path)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WikiError("Wiki document could not be read") from exc
        metadata, body = _split_front_matter(source)
        rendered = render_markdown(body)
        updated = metadata.get("updated_at")
        if updated is not None and not isinstance(updated, str):
            updated = str(updated)
        return WikiDocument(
            path=path.relative_to(root).as_posix(),
            title=_title_for(path, metadata, body),
            markdown=body,
            html=rendered,
            metadata=metadata,
            updated_at=updated,
        )

    def documents(self) -> list[WikiDocument]:
        documents: list[WikiDocument] = []
        for root, path in self._iter_paths():
            try:
                documents.append(self.read(path.relative_to(root).as_posix()))
            except WikiError:
                continue
        return sorted(documents, key=lambda document: (document.title.casefold(), document.path))

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_documents
            USING fts5(path UNINDEXED, title, body, tokenize='unicode61')
            """
        )

    def index(self) -> dict[str, Any]:
        """Rebuild the disposable FTS index from authoritative Markdown."""

        documents = self.documents()
        indexed_at = _utc_now()
        try:
            with self._connect() as connection:
                self._create_schema(connection)
                connection.execute("DELETE FROM wiki_documents")
                connection.executemany(
                    "INSERT INTO wiki_documents(path, title, body) VALUES (?, ?, ?)",
                    [
                        (document.path, document.title, _plain_text(document.html))
                        for document in documents
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO wiki_index_meta(key, value) VALUES ('indexed_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (indexed_at,),
                )
        except sqlite3.Error as exc:
            raise WikiError("Wiki search index is unavailable") from exc
        return {"documents": len(documents), "indexed_at": indexed_at}

    def _ensure_index(self) -> None:
        if not self.database_path.exists():
            self.index()
            return
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value FROM wiki_index_meta WHERE key = 'indexed_at'"
                ).fetchone()
            if row is None:
                self.index()
        except sqlite3.Error:
            self.index()

    def search(self, query: str, *, limit: int = 20) -> list[WikiSearchResult]:
        tokens = _SEARCH_TOKEN.findall(query)[:10]
        if not tokens:
            return []
        limit = max(1, min(int(limit), 50))
        expression = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        self._ensure_index()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT path, title,
                           snippet(wiki_documents, 2, '', '', ' … ', 24) AS snippet
                    FROM wiki_documents
                    WHERE wiki_documents MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (expression, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise WikiError("Wiki search is unavailable") from exc
        return [
            WikiSearchResult(path=row["path"], title=row["title"], snippet=row["snippet"] or "")
            for row in rows
        ]


# A descriptive alias for dependency-injection call sites.
WikiService = WikiIndex
