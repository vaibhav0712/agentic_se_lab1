#!/usr/bin/env python3
"""R2 fixed-chunk dense retrieval and R3 hybrid retrieval.

Assumed experiment mapping (the repository does not include the lab-sheet R2/R3
definitions): R2=dense cosine retrieval; R3=BM25 + dense reciprocal-rank fusion.

Examples:
  python run_embedding_r2_r3.py --variant r2 --split dev
  python run_embedding_r2_r3.py --variant r3 --split test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common.llm_client import LLMClient  # noqa: E402
from run_task3 import answer, load_jsonl  # noqa: E402

DEFAULT_MODEL = "kiconnect.qwen3-embedding-8b"
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50


def normalized(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def cache_path(model: str, cache_dir: Path, variant: str, chunk_size: int, overlap: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    return cache_dir / f"corpus_embeddings_{variant}_{safe}_{chunk_size}_{overlap}.npz"


def fixed_chunks(docs: list[dict], chunk_size: int = DEFAULT_CHUNK_SIZE,
                 overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[dict]:
    """Split document text into fixed character windows; retain source doc IDs."""
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Require chunk_size > overlap >= 0")
    step = chunk_size - overlap
    chunks = []
    for doc in docs:
        text = doc.get("text", "")
        if not text:
            text = doc.get("title", "")
        for start in range(0, len(text), step):
            chunk_text = text[start:start + chunk_size]
            if chunk_text:
                chunks.append({"doc_id": doc["doc_id"], "chunk_id": f"{doc['doc_id']}_{start}",
                               "text": chunk_text, "title": doc.get("title", ""), "start": start})
    return chunks


def recursive_units(text: str, limit: int) -> list[str]:
    """Split by headings, paragraphs, then sentences; hard-split oversized units."""
    separators = [r"(?m)(?=^#{1,6}\s)", r"\n\s*\n+", r"(?<=[.!?])\s+"]
    units = [text]
    for separator in separators:
        next_units = []
        for unit in units:
            if len(unit) <= limit:
                next_units.append(unit)
            else:
                next_units.extend(re.split(separator, unit))
        units = [u.strip() for u in next_units if u.strip()]
    result = []
    for unit in units:
        while len(unit) > limit:
            # Prefer a nearby whitespace boundary to avoid cutting words.
            cut = unit.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            result.append(unit[:cut].strip())
            unit = unit[cut:].strip()
        if unit:
            result.append(unit)
    return result


def recursive_chunks(docs: list[dict], chunk_size: int, overlap: int) -> list[dict]:
    """Structure-aware chunks with fixed character overlap between windows."""
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Require chunk_size > overlap >= 0")
    chunks = []
    for doc in docs:
        text = doc.get("text", "") or doc.get("title", "")
        pieces = recursive_units(text, chunk_size)
        current = ""
        windows = []
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= chunk_size:
                current = candidate
            else:
                if current:
                    windows.append(current)
                # Carry the requested character overlap, then append the next
                # structural unit, trimming only when the window would overflow.
                prefix = current[-overlap:] if current and overlap else ""
                current = (prefix + "\n\n" + piece).strip()
                if len(current) > chunk_size:
                    current = current[:chunk_size]
        if current:
            windows.append(current)
        for index, chunk_text in enumerate(windows):
            chunks.append({"doc_id": doc["doc_id"], "chunk_id": f"{doc['doc_id']}_{index}",
                           "text": chunk_text, "title": doc.get("title", ""), "start": index})
    return chunks


def get_chunk_vectors(chunks: list[dict], llm: LLMClient, model: str, cache_dir: Path,
                      force: bool, variant: str, chunk_size: int, overlap: int) -> np.ndarray:
    path = cache_path(model, cache_dir, variant, chunk_size, overlap)
    signature = f"{model}:{chunk_size}:{overlap}:" + ":".join(
        f"{c['chunk_id']}:{len(c['text'])}" for c in chunks)
    if path.exists() and not force:
        with np.load(path, allow_pickle=False) as data:
            if str(data["signature"].item()) == signature:
                vectors = data["vectors"]
                if vectors.shape[0] == len(chunks):
                    print(f"Loaded cached chunk embeddings ({len(chunks)} chunks): {path}")
                    return vectors
    texts = [c["text"] for c in chunks]
    vectors = llm.embed(texts, model=model, batch_size=32)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, vectors=vectors, signature=np.asarray(signature))
    print(f"Cached {len(chunks)} chunk embeddings: {path}")
    return vectors


def dense_rank(query_vector: np.ndarray, doc_vectors: np.ndarray) -> list[int]:
    return np.argsort(-(doc_vectors @ query_vector), kind="stable").tolist()


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("r2", "r3"), required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--cache-dir", type=Path, default=HERE / ".cache")
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    parser.add_argument("--force-embed", action="store_true", help="rebuild the corpus embedding cache")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")

    docs = load_jsonl(HERE / "data" / "corpus.jsonl")
    if args.chunk_size < 1 or args.chunk_overlap < 0 or args.chunk_overlap >= args.chunk_size:
        parser.error("require --chunk-size > --chunk-overlap >= 0")
    questions = load_jsonl(HERE / "data" / f"questions_{args.split}.jsonl")
    llm = LLMClient(embedding_model=args.model, log_path=HERE / "logs" / f"{args.variant}_{args.split}_embeddings.jsonl")
    chunker = fixed_chunks if args.variant == "r2" else recursive_chunks
    chunks = chunker(docs, args.chunk_size, args.chunk_overlap)
    chunk_vectors = normalized(get_chunk_vectors(chunks, llm, args.model, args.cache_dir,
                                                 args.force_embed, args.variant,
                                                 args.chunk_size, args.chunk_overlap))
    query_vectors = normalized(llm.embed([q["question"] for q in questions], model=args.model, batch_size=32))

    doc_index = {doc["doc_id"]: i for i, doc in enumerate(docs)}
    retrieval_rows, answer_rows, predictions = [], [], []
    for question, query_vector in zip(questions, query_vectors):
        chunk_order = dense_rank(query_vector, chunk_vectors)
        # Collapse ranked chunks into a document ranking using each document's
        # best matching chunk; first occurrence preserves highest-scoring chunk.
        best_chunk: dict[str, int] = {}
        for chunk_i in chunk_order:
            best_chunk.setdefault(chunks[chunk_i]["doc_id"], chunk_i)
        dense_order = [doc_index[doc_id] for doc_id in best_chunk]
        order = dense_order
        chosen = [(docs[i], float(chunk_vectors[best_chunk[docs[i]["doc_id"]]] @ query_vector))
                  for i in order[:args.top_k]]
        ids = list(dict.fromkeys(doc["doc_id"] for doc, _ in chosen))
        response, contexts = answer(question["question"], chosen)
        retrieval_rows.append({"id": question["id"], "retrieved_doc_ids": ids})
        answer_rows.append({"id": question["id"], "answer": response, "contexts": contexts})
        if args.split == "test":
            predictions.append({"id": question["id"], "answer": response, "retrieved_doc_ids": ids})

    setting = f"_{args.chunk_size}" if args.variant == "r3" else ""
    prefix = f"{args.variant}{setting}_{args.split}"
    write_jsonl(args.out_dir / f"retrieval_{prefix}.jsonl", retrieval_rows)
    write_jsonl(args.out_dir / f"answers_{prefix}.jsonl", answer_rows)
    if args.split == "test":
        write_jsonl(HERE / f"predictions_{args.variant}{setting}_test.jsonl", predictions)
    print(f"Wrote {len(questions)} {args.variant.upper()} {args.split} predictions; embedding API calls: {llm.usage_summary()}")
    if args.split == "test":
        print(f"Submission candidate: {HERE / f'predictions_{args.variant}_test.jsonl'}")


if __name__ == "__main__":
    main()
