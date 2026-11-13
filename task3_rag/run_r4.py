#!/usr/bin/env python3
"""R4 hybrid retrieval: equal-weight RRF over R1 BM25 and R3 dense ranks.

Uses R3 recursive chunks (default 500 characters, 50-character overlap),
collapses chunk hits to document ranks, and fuses those with R1 lexical ranks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common.llm_client import LLMClient  # noqa: E402
from run_embedding_r2_r3 import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MODEL,
    dense_rank,
    get_chunk_vectors,
    normalized,
    recursive_chunks,
    write_jsonl,
)
from run_task3 import LexicalRetriever, load_jsonl  # noqa: E402


def reciprocal_rank_fusion(rankings: list[list[str]], rrf_k: int = 60,
                           weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Fuse ranked doc IDs; missing docs contribute zero to that ranking."""
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("One weight is required per ranking")
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (rrf_k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--lexical-weight", type=float, default=1.0)
    parser.add_argument("--dense-weight", type=float, default=1.0)
    parser.add_argument("--cache-dir", type=Path, default=HERE / ".cache")
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    parser.add_argument("--force-embed", action="store_true")
    args = parser.parse_args()
    if args.top_k < 1 or args.rrf_k < 0:
        parser.error("--top-k must be positive and --rrf-k nonnegative")
    if args.chunk_size < 1 or args.chunk_overlap < 0 or args.chunk_overlap >= args.chunk_size:
        parser.error("require --chunk-size > --chunk-overlap >= 0")

    docs = load_jsonl(HERE / "data" / "corpus.jsonl")
    questions = load_jsonl(HERE / "data" / f"questions_{args.split}.jsonl")
    lexical = LexicalRetriever(docs)
    llm = LLMClient(embedding_model=args.model,
                    log_path=HERE / "logs" / f"r4_{args.split}_embeddings.jsonl")
    chunks = recursive_chunks(docs, args.chunk_size, args.chunk_overlap)
    chunk_vectors = normalized(get_chunk_vectors(
        chunks, llm, args.model, args.cache_dir, args.force_embed,
        "r3", args.chunk_size, args.chunk_overlap,
    ))
    query_vectors = normalized(llm.embed([q["question"] for q in questions], model=args.model, batch_size=32))

    rows = []
    for question, query_vector in zip(questions, query_vectors):
        # The dense ranking is over chunks; collapse by first occurrence, which
        # retains each document's highest-scoring chunk rank.
        chunk_order = dense_rank(query_vector, chunk_vectors)
        dense_doc_ids = list(dict.fromkeys(chunks[i]["doc_id"] for i in chunk_order))
        lexical_doc_ids = [doc["doc_id"] for doc, _ in lexical.search(question["question"], len(docs))]
        fused = reciprocal_rank_fusion(
            [lexical_doc_ids, dense_doc_ids], args.rrf_k,
            [args.lexical_weight, args.dense_weight],
        )
        rows.append({"id": question["id"],
                     "retrieved_doc_ids": [doc_id for doc_id, _ in fused[:args.top_k]]})

    out_path = args.out_dir / f"retrieval_r4_{args.split}.jsonl"
    write_jsonl(out_path, rows)
    print(f"Wrote {len(rows)} R4 results to {out_path}")
    print(f"RRF weights: lexical={args.lexical_weight:g}, dense={args.dense_weight:g}; k={args.rrf_k}; "
          f"chunk={args.chunk_size}/{args.chunk_overlap}; {llm.usage_summary()}")


if __name__ == "__main__":
    main()
