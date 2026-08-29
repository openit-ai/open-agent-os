"""Deterministic chunking with stable IDs and content hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_CHARS = 800
DEFAULT_OVERLAP = 100


def content_hash(text: str) -> str:
    """Deterministic SHA256 hex digest of UTF-8 text.

    Normalizes None to empty string; does not strip — raw bytes define hash
    so "hello " != "hello". Empty string => sha256("").
    """
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(
    text: str,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """Split text into overlapping char chunks.

    - Deterministic, no randomness.
    - max_chars clamped >=64, overlap in [0, max_chars-1]
    - overlap==0 => non-overlapping
    - Returns [] for empty/whitespace-only input, otherwise >=1 chunk.
    - Last chunk may be shorter than max_chars.
    """
    if not text:
        return []
    if not text.strip():
        return []
    cs = max_chars if max_chars is not None else DEFAULT_MAX_CHARS
    ov = overlap if overlap is not None else DEFAULT_OVERLAP
    # allow env-driven defaults to be overridden already; just clamp
    cs = max(64, int(cs))
    ov = max(0, min(int(ov), cs - 1))
    if len(text) <= cs:
        return [text]
    step = cs - ov
    chunks: list[str] = []
    for i in range(0, len(text), step):
        c = text[i : i + cs]
        if not c.strip():
            continue
        chunks.append(c)
        if i + cs >= len(text):
            break
    return chunks or [text[:cs]]


@dataclass
class ChunkConfig:
    """Configurable chunking parameters."""

    max_chars: int = DEFAULT_MAX_CHARS
    overlap: int = DEFAULT_OVERLAP

    def __post_init__(self) -> None:
        self.max_chars = max(64, int(self.max_chars))
        self.overlap = max(0, min(int(self.overlap), self.max_chars - 1))


@dataclass
class Chunk:
    """A single chunk with stable deterministic ID."""

    chunk_id: str
    resource_id: str
    index: int
    text: str
    content_hash: str  # hash of chunk text
    source_content_hash: str  # hash of full source document content
    token_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "resource_id": self.resource_id,
            "index": self.index,
            "text": self.text,
            "content_hash": self.content_hash,
            "source_content_hash": self.source_content_hash,
        }


def _stable_chunk_id(resource_id: str, index: int, chunk_text_hash: str) -> str:
    """Stable chunk ID: deterministic on resource_id + index + chunk hash prefix.

    Uses 12-char hash prefix to keep IDs readable but still collision-resistant
    within a document. Full determinism without UUID.
    """
    # 16 hex chars of sha256(resource_id:index:chunk_hash)
    raw = f"{resource_id}:{index:06d}:{chunk_text_hash}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    # also include short hash prefix for debuggability
    short = chunk_text_hash[:8]
    return f"{resource_id}#c{index:04d}-{short}-{digest}"


def make_chunks(
    resource_id: str,
    content: str,
    source_content_hash: str | None = None,
    config: ChunkConfig | None = None,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """Create stable chunks for a resource.

    Args:
        resource_id: e.g. "outline/team/doc_001" or "notion/page/abc"
        content: full document text
        source_content_hash: precomputed doc hash; if None computed
        config: ChunkConfig or individually supplied max_chars/overlap
    """
    if config is None:
        config = ChunkConfig(
            max_chars=max_chars if max_chars is not None else DEFAULT_MAX_CHARS,
            overlap=overlap if overlap is not None else DEFAULT_OVERLAP,
        )
    else:
        # allow override via args
        if max_chars is not None:
            config = ChunkConfig(max_chars=max_chars, overlap=config.overlap)
        if overlap is not None:
            config = ChunkConfig(max_chars=config.max_chars, overlap=overlap)

    sch = source_content_hash if source_content_hash is not None else content_hash(content)
    texts = chunk_text(content, max_chars=config.max_chars, overlap=config.overlap)
    chunks: list[Chunk] = []
    for idx, t in enumerate(texts):
        ch = content_hash(t)
        cid = _stable_chunk_id(resource_id, idx, ch)
        chunks.append(
            Chunk(
                chunk_id=cid,
                resource_id=resource_id,
                index=idx,
                text=t,
                content_hash=ch,
                source_content_hash=sch,
            )
        )
    return chunks
