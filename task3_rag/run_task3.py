#!/usr/bin/env python3
"""Stdlib-only lexical RAG baseline for Task 3.

Usage: python run_task3.py --split dev
       python run_task3.py --split test
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABSTENTION = "The documentation does not contain this information."

# Keep complete identifiers and their useful pieces (e.g. PQ-E4663, E4663,
# PQ, ingress.interval_s, ingress, interval, s) so punctuation variation works.
TOKEN_RE = re.compile(r"[a-z]+|[0-9]+", re.I)
IDENT_RE = re.compile(r"[A-Za-z]+(?:[._-][A-Za-z0-9]+)*|[0-9]+")
STOP = set("a an and are as at be by can does for from how i in is it of on or should tell that the this to was what when where which who why with".split())


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for ident in IDENT_RE.findall(text.lower()):
        out.append(ident)
        out.extend(t for t in TOKEN_RE.findall(ident) if t != ident)
    return out


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class LexicalRetriever:
    def __init__(self, docs: list[dict], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.fields = [tokens(d.get("title", "") + " " + d.get("text", "")) for d in docs]
        self.lengths = [len(x) for x in self.fields]
        self.avgdl = sum(self.lengths) / max(1, len(self.lengths))
        self.df = Counter(term for field in self.fields for term in set(field))
        self.index: dict[str, list[tuple[int, int]]] = {}
        for i, field in enumerate(self.fields):
            for term, freq in Counter(field).items():
                self.index.setdefault(term, []).append((i, freq))

    def search(self, query: str, limit: int = 5) -> list[tuple[dict, float]]:
        qterms = [t for t in tokens(query) if t not in STOP]
        if not qterms:
            qterms = tokens(query)
        scores: Counter[int] = Counter()
        n = len(self.docs)
        for term in set(qterms):
            postings = self.index.get(term, ())
            if not postings:
                continue
            idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, tf in postings:
                denom = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / max(self.avgdl, 1))
                scores[i] += idf * tf * (self.k1 + 1) / denom
        # Exact identifier matches are unusually informative in this corpus.
        query_ids = set(IDENT_RE.findall(query.lower()))
        for i, doc in enumerate(self.docs):
            title = doc.get("title", "").lower()
            text = doc.get("text", "").lower()
            scores[i] += 2.5 * sum(1 for term in query_ids if term in title)
            scores[i] += 1.25 * sum(1 for term in query_ids if term in text and term not in title)
        ranked = sorted(scores, key=lambda i: (-scores[i], self.docs[i]["doc_id"]))
        return [(self.docs[i], scores[i]) for i in ranked[:limit]]


def sentence_units(doc: dict) -> list[str]:
    # Markdown tables/list items can contain the answer; split at sentence ends
    # and line breaks, retaining compact units rather than whole long pages.
    text = doc.get("text", "")
    units = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [re.sub(r"\s+", " ", u).strip(" #|\t") for u in units if len(u.strip()) > 15]


def answer(question: str, retrieved: list[tuple[dict, float]]) -> tuple[str, list[str]]:
    contexts = [doc.get("text", "") for doc, _ in retrieved]
    qterms = {t for t in tokens(question) if t not in STOP and len(t) > 1}
    candidates: list[tuple[float, str]] = []
    for rank, (doc, retrieval_score) in enumerate(retrieved):
        for sent in sentence_units(doc):
            sterms = set(tokens(sent))
            overlap = len(qterms & sterms)
            # Prefer direct lexical evidence and earlier retrieved documents.
            score = overlap / max(1, len(qterms)) + 0.025 * retrieval_score - 0.015 * rank
            if overlap:
                candidates.append((score, sent))
    candidates.sort(key=lambda x: (-x[0], x[1]))
    if not candidates or candidates[0][0] < 0.10:
        return ABSTENTION, contexts
    picked: list[str] = []
    for score, sent in candidates:
        if score < max(0.10, candidates[0][0] * 0.72):
            break
        if sent not in picked:
            picked.append(sent)
        if len(picked) == 2:
            break
    return " ".join(picked), contexts


def run(split: str, top_k: int, out_dir: Path) -> None:
    docs = load_jsonl(HERE / "data" / "corpus.jsonl")
    questions = load_jsonl(HERE / "data" / f"questions_{split}.jsonl")
    retriever = LexicalRetriever(docs)
    out_dir.mkdir(parents=True, exist_ok=True)
    retrieval_path = out_dir / f"retrieval_{split}.jsonl"
    answers_path = out_dir / f"answers_{split}.jsonl"
    prediction_path = HERE / "predictions_test.jsonl"
    retrieval_rows, answer_rows, prediction_rows = [], [], []
    for q in questions:
        ranked = retriever.search(q["question"], top_k)
        doc_ids = list(dict.fromkeys(doc["doc_id"] for doc, _ in ranked))
        generated, contexts = answer(q["question"], ranked)
        retrieval_rows.append({"id": q["id"], "retrieved_doc_ids": doc_ids})
        answer_rows.append({"id": q["id"], "answer": generated, "contexts": contexts})
        if split == "test":
            prediction_rows.append({"id": q["id"], "answer": generated, "retrieved_doc_ids": doc_ids})
    for path, rows in ((retrieval_path, retrieval_rows), (answers_path, answer_rows)):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    if split == "test":
        prediction_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in prediction_rows), encoding="utf-8")
    print(f"Wrote {len(questions)} questions: {retrieval_path}, {answers_path}")
    if split == "test":
        print(f"Submission file: {prediction_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    run(args.split, args.top_k, args.out_dir)


if __name__ == "__main__":
    main()
