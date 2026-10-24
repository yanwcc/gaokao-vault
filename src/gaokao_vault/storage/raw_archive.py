"""Content-addressed archive of raw crawl responses.

Bodies are gzip-compressed under ``<root>/<domain>/<hash[:2]>/<hash><ext>.gz`` and indexed in
``source_documents``, so parsers can be re-run over past crawls without fetching again.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

_CONTENT_TYPE_EXTENSIONS = (
    ("html", ".html"),
    ("json", ".json"),
    ("ms-excel", ".xls"),
    ("spreadsheetml", ".xlsx"),
    ("pdf", ".pdf"),
)
_URL_EXTENSIONS = (".html", ".htm", ".json", ".xls", ".xlsx", ".pdf", ".doc", ".docx")


def guess_extension(url: str, content_type: str | None) -> str:
    path = urlsplit(url).path.lower()
    for extension in _URL_EXTENSIONS:
        if path.endswith(extension):
            return extension
    lowered = (content_type or "").lower()
    for marker, extension in _CONTENT_TYPE_EXTENSIONS:
        if marker in lowered:
            return extension
    return ".bin"


class RawArchive:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @staticmethod
    def storage_key(domain: str, content_hash: str, extension: str) -> str:
        return f"{domain}/{content_hash[:2]}/{content_hash}{extension}.gz"

    async def save(self, body: bytes, *, url: str, content_type: str | None = None) -> tuple[str, str]:
        """Store ``body`` (once per content hash) and return ``(content_hash, storage_key)``."""
        content_hash = hashlib.sha256(body).hexdigest()
        domain = urlsplit(url).hostname or "unknown"
        key = self.storage_key(domain, content_hash, guess_extension(url, content_type))
        await asyncio.to_thread(self._write_once, self._root / key, body)
        return content_hash, key

    def load(self, storage_key: str) -> bytes:
        return gzip.decompress((self._root / storage_key).read_bytes())

    @staticmethod
    def _write_once(path: Path, body: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp_path.write_bytes(gzip.compress(body))
        tmp_path.replace(path)
