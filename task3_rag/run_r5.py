#!/usr/bin/env python3
"""R5: BM25 candidate retrieval followed by cross-encoder re-ranking.

Requires the optional sentence-transformers dependency. Defaults to
cross-encoder/ms-marco-MiniLM-L-6-v2, as suggested in README.md.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
from run_task3 import LexicalRetriever, load_jsonl  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--model", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = parser.parse_args()
    if args.candidate_k < args.top_k or args.top_k < 1 or args.batch_size < 1:
        parser.error("require candidate-k >= top-k >= 1")
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise SystemExit("R5 requires sentence-transformers. Install with: uv sync --extra local") from exc

    docs = load_jsonl(HERE / "data" / "corpus.jsonl")
    questions = load_jsonl(HERE / "data" / f"questions_{args.split}.jsonl")
    start = time.perf_counter()
    retriever = LexicalRetriever(docs)
    index_seconds = time.perf_counter() - start
    reranker = CrossEncoder(args.model)
    rows = []
    query_seconds = []
    for n, question in enumerate(questions, 1):
        query_start = time.perf_counter()
        candidates = retriever.search(question["question"], args.candidate_k)
        pairs = [(question["question"], doc.get("title", "") + "\n" + doc.get("text", ""))
                 for doc, _ in candidates]
        scores = reranker.predict(pairs, batch_size=args.batch_size, show_progress_bar=False)
        ranked = sorted(zip(candidates, scores), key=lambda item: (-float(item[1]), item[0][0]["doc_id"]))
        rows.append({"id": question["id"], "retrieved_doc_ids": [doc["doc_id"] for ((doc, _), _) in ranked[:args.top_k]]})
        query_seconds.append(time.perf_counter() - query_start)
        if n % 25 == 0:
            print(f"R5 reranked {n}/{len(questions)} questions")
    out = args.out_dir / f"retrieval_r5_{args.split}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    mean_query_s = sum(query_seconds) / max(1, len(query_seconds))
    print(f"Wrote {len(rows)} R5 results to {out}; model={args.model}; index={index_seconds:.3f}s; "
          f"mean query/rerank={mean_query_s:.3f}s")


if __name__ == "__main__":
    main()
