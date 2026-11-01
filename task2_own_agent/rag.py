"""RAG component for the Task 2 agent: chunk, embed and search the style guide.

- Loads every Markdown document from task2_own_agent/style_guide/.
- Parses the document header (id, title, status, category, "replaced by").
- Splits each document body into fixed-size token windows with overlap
  (measured with tiktoken cl100k_base, windows snap to word boundaries).
- Embeds the chunks (gateway embedding model via LLMClient.embed by default,
  optionally a local sentence-transformers model) and keeps the L2-normalised
  vectors in memory as one numpy matrix.
- search(query, k) returns the k most similar chunks by cosine similarity.
- format_observation(chunks) renders search results for the agent and enforces
  the 1,000-token budget of a single search_style_guide observation.

Allowed dependencies only: standard library, numpy, tiktoken, common/ helpers,
optionally sentence-transformers.

Manual test from the repository root:
    uv run python task2_own_agent/rag.py "night mode background color" -k 5
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient, count_tokens  # noqa: E402

STYLE_GUIDE_DIR = Path(__file__).resolve().parent / "style_guide"

DEFAULT_CHUNK_TOKENS = 120
DEFAULT_OVERLAP_TOKENS = 30
DEFAULT_K = 5
OBSERVATION_TOKEN_BUDGET = 1000
DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

EmbedFn = Callable[[list[str]], np.ndarray]


# ---------------------------------------------------------------- data types


@dataclass
class Document:
    doc_id: str  # e.g. "SG-13"
    title: str  # e.g. "Night mode palette (version 2)"
    status: str  # full status line, e.g. "ACTIVE (version 2)"
    category: str
    body: str  # document text without the header lines
    path: Path
    replaced_by: str | None = None  # e.g. "SG-13" for deprecated documents

    @property
    def deprecated(self) -> bool:
        return self.status.upper().startswith("DEPRECATED")


@dataclass
class Chunk:
    chunk_id: str  # e.g. "SG-13#0"
    doc_id: str  # id of the source document, e.g. "SG-13"
    title: str
    status: str
    text: str  # the chunk text (body window only, without header)
    replaced_by: str | None = None
    score: float = field(default=0.0, compare=False)

    @property
    def deprecated(self) -> bool:
        return self.status.upper().startswith("DEPRECATED")

    @property
    def header(self) -> str:
        """Short source line shown to the model and prepended for embedding."""
        return f"[{self.doc_id}: {self.title} | Status: {self.status}]"

    def render(self) -> str:
        return f"{self.header}\n{self.text}"


# ------------------------------------------------------------------- loading

_TITLE_RE = re.compile(r"^#\s*(SG-\d+)\s*:\s*(.+?)\s*$")
_STATUS_RE = re.compile(r"^Status:\s*(.+?)\s*$", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"^Category:\s*(.+?)\s*$", re.IGNORECASE)
_REPLACED_RE = re.compile(r"replaced by\s+(SG-\d+)", re.IGNORECASE)
_FILENAME_ID_RE = re.compile(r"^(SG-\d+)")


def parse_document(path: Path) -> Document:
    """Parse one style guide file. Header lines are removed from the body."""
    lines = path.read_text(encoding="utf-8").splitlines()
    match = _FILENAME_ID_RE.match(path.stem)
    doc_id = match.group(1) if match else path.stem
    title, status, category = path.stem, "ACTIVE", ""
    body_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if m := _TITLE_RE.match(stripped):
            doc_id, title = m.group(1), m.group(2)
        elif (m := _STATUS_RE.match(stripped)) and not status_seen(body_lines):
            status = m.group(1)
        elif (m := _CATEGORY_RE.match(stripped)) and not status_seen(body_lines):
            category = m.group(1)
        else:
            body_lines.append(line)

    replaced = _REPLACED_RE.search(status)
    return Document(
        doc_id=doc_id,
        title=title,
        status=status,
        category=category,
        body="\n".join(body_lines).strip(),
        path=path,
        replaced_by=replaced.group(1) if replaced else None,
    )


def status_seen(body_lines: list[str]) -> bool:
    """Header lines only count before the first non-empty body line."""
    return any(line.strip() for line in body_lines)


def load_documents(style_dir: Path = STYLE_GUIDE_DIR) -> list[Document]:
    paths = sorted(Path(style_dir).glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"No style guide documents found in {style_dir}")
    return [parse_document(p) for p in paths]


# ------------------------------------------------------------------ chunking


def _token_counter() -> Callable[[str], int]:
    """Exact per-piece token counts with tiktoken; falls back to count_tokens."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return lambda s: len(enc.encode(s))
    except Exception:
        return count_tokens


def chunk_text(
    text: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """Fixed-size token windows with overlap.

    The text is split into words (keeping their trailing whitespace and newlines,
    so Markdown structure survives). Words are added to a window until it holds
    `chunk_tokens` tokens; the next window starts so that roughly `overlap_tokens`
    tokens of the previous window are repeated. Windows never cut a word in half.
    """
    if overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must be smaller than chunk_tokens")
    pieces = re.findall(r"\S+\s*", text)
    if not pieces:
        return []
    tok = _token_counter()
    sizes = [tok(p) for p in pieces]

    chunks: list[str] = []
    start = 0
    while start < len(pieces):
        end, total = start, 0
        while end < len(pieces) and (total + sizes[end] <= chunk_tokens or end == start):
            total += sizes[end]
            end += 1
        chunks.append("".join(pieces[start:end]).strip())
        if end >= len(pieces):
            break
        # step back from `end` until about overlap_tokens are covered
        next_start, back = end, 0
        while next_start > start + 1 and back + sizes[next_start - 1] <= overlap_tokens:
            next_start -= 1
            back += sizes[next_start]
        start = next_start
    return chunks


def chunk_documents(
    documents: list[Document],
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in documents:
        for i, window in enumerate(chunk_text(doc.body, chunk_tokens, overlap_tokens)):
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{i}",
                    doc_id=doc.doc_id,
                    title=doc.title,
                    status=doc.status,
                    text=window,
                    replaced_by=doc.replaced_by,
                )
            )
    return chunks


# ---------------------------------------------------------------- embeddings


def gateway_embedder(llm: LLMClient | None = None, model: str | None = None) -> EmbedFn:
    """Embeddings from the UPB AI Gateway ($LAB_EMBEDDING_MODEL, free)."""
    client = llm or LLMClient()
    return lambda texts: client.embed(texts, model=model)


def local_embedder(model_name: str = DEFAULT_LOCAL_MODEL) -> EmbedFn:
    """Local sentence-transformers model (optional dependency)."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is not installed. Install the optional 'local' extra "
            "or use the gateway backend."
        ) from exc
    model = SentenceTransformer(model_name)
    return lambda texts: np.asarray(model.encode(texts, show_progress_bar=False), dtype=np.float32)


def _normalize(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix[None, :]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.clip(norms, 1e-12, None)


# --------------------------------------------------------------------- index


class StyleGuideIndex:
    """In-memory vector index over the style guide chunks."""

    def __init__(
        self,
        style_dir: str | Path = STYLE_GUIDE_DIR,
        backend: str = "gateway",
        llm: LLMClient | None = None,
        embedding_model: str | None = None,
        embed_fn: EmbedFn | None = None,
        chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
        overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        """
        backend:         "gateway" (LLMClient.embed) or "local" (sentence-transformers)
        llm:             reuse an existing LLMClient (so embedding calls land in the same log)
        embedding_model: override the model for the chosen backend
        embed_fn:        inject your own embedding function (e.g. a fake for tests);
                         takes precedence over `backend`
        """
        if embed_fn is None:
            if backend == "gateway":
                embed_fn = gateway_embedder(llm, embedding_model)
            elif backend == "local":
                embed_fn = local_embedder(embedding_model or DEFAULT_LOCAL_MODEL)
            else:
                raise ValueError(f"Unknown embedding backend: {backend!r} (use 'gateway' or 'local')")
        self._embed = embed_fn

        self.documents = load_documents(Path(style_dir))
        self.chunks = chunk_documents(self.documents, chunk_tokens, overlap_tokens)
        # The header (id, title, status) is embedded together with the window text,
        # so a window from the middle of a document still knows what it is about.
        self.matrix = _normalize(self._embed([c.render() for c in self.chunks]))
        if self.matrix.shape[0] != len(self.chunks):
            raise RuntimeError("Embedding backend returned a wrong number of vectors")

    def search(self, query: str, k: int = DEFAULT_K, include_deprecated: bool = False) -> list[Chunk]:
        """Return the k chunks most similar to `query` by cosine similarity.

        Deprecated documents are excluded unless include_deprecated=True.
        Returned chunks are copies with `score` set (cosine similarity).
        """
        if not query or not query.strip() or k <= 0:
            return []
        q = _normalize(self._embed([query]))[0]
        scores = self.matrix @ q
        results: list[Chunk] = []
        for idx in np.argsort(-scores):
            chunk = self.chunks[int(idx)]
            if chunk.deprecated and not include_deprecated:
                continue
            results.append(_with_score(chunk, float(scores[idx])))
            if len(results) >= k:
                break
        return results


def _with_score(chunk: Chunk, score: float) -> Chunk:
    return Chunk(
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        title=chunk.title,
        status=chunk.status,
        text=chunk.text,
        replaced_by=chunk.replaced_by,
        score=score,
    )


# ------------------------------------------------------- observation budget


def format_observation(chunks: list[Chunk], max_tokens: int = OBSERVATION_TOKEN_BUDGET) -> str:
    """Render search results for the agent within `max_tokens` (count_tokens).

    The whole rendered string is counted (headers included), which is stricter
    than counting only style guide text. Chunks are added in rank order; a chunk
    that does not fit is cut at a word boundary and nothing after it is added.
    """
    if not chunks:
        return "No matching style guide passages found."
    parts: list[str] = []
    for chunk in chunks:
        block = chunk.render()
        candidate = "\n\n".join(parts + [block])
        if count_tokens(candidate) <= max_tokens:
            parts.append(block)
            continue
        words = block.split(" ")
        while words and count_tokens("\n\n".join(parts + [" ".join(words) + " ..."])) > max_tokens:
            words = words[: max(0, len(words) - max(1, len(words) // 10))]
        if len(" ".join(words)) > len(chunk.header):
            parts.append(" ".join(words) + " ...")
        break
    return "\n\n".join(parts)


# ------------------------------------------------- module-level convenience

_default_index: StyleGuideIndex | None = None


def get_index(**kwargs) -> StyleGuideIndex:
    """Build the default index on first use (kwargs are passed on that first call)."""
    global _default_index
    if _default_index is None:
        _default_index = StyleGuideIndex(**kwargs)
    return _default_index


def search(query: str, k: int = DEFAULT_K) -> list[Chunk]:
    """search(query, k) -> list[Chunk] on the default (gateway) index."""
    return get_index().search(query, k)


# ----------------------------------------------------------------------- CLI


def main() -> None:
    parser = argparse.ArgumentParser(description="Search the style guide")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=DEFAULT_K)
    parser.add_argument("--backend", choices=["gateway", "local"], default="gateway")
    parser.add_argument("--chunk-tokens", type=int, default=DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--overlap-tokens", type=int, default=DEFAULT_OVERLAP_TOKENS)
    parser.add_argument("--include-deprecated", action="store_true")
    args = parser.parse_args()

    index = StyleGuideIndex(
        backend=args.backend, chunk_tokens=args.chunk_tokens, overlap_tokens=args.overlap_tokens
    )
    print(f"{len(index.documents)} documents, {len(index.chunks)} chunks, dim {index.matrix.shape[1]}\n")
    results = index.search(args.query, args.k, include_deprecated=args.include_deprecated)
    for c in results:
        print(f"{c.score:.3f}  {c.chunk_id}  {c.title}")
    observation = format_observation(results)
    print(f"\n--- observation ({count_tokens(observation)} tokens) ---\n{observation}")


if __name__ == "__main__":
    main()
