# Task 3 final-stage status and inspection notes

## Retrieval comparison

Dev metrics at k=5, averaged over the 120 answerable questions:

| Configuration | Context recall | Context precision |
|---|---:|---:|
| R1 lexical BM25 | 0.642 | 0.619 |
| R2 fixed chunks, dense, 500/50 | 0.542 | 0.450 |
| R3 recursive chunks, dense, 250/50 | 0.542 | 0.482 |
| R3 recursive chunks, dense, 500/50 | 0.546 | 0.499 |
| R3 recursive chunks, dense, 1000/50 | 0.571 | 0.490 |
| R4 equal-weight RRF, R1 + R3 500/50 | 0.567 | 0.474 |
| R5 lexical candidate pool + cross-encoder reranking | 0.625 | 0.591 |

R1 is the best measured configuration on both retrieval metrics, and is selected for
`predictions_test.jsonl`. R5 was run with a 50-document lexical candidate pool and model
R5 uses a 50-document R1/BM25 candidate pool reranked with
`cross-encoder/ms-marco-MiniLM-L-6-v2`. While it trails R1 overall, it performs better on
multi-hop (0.367 recall / 0.532 precision) and version questions (0.900 / 0.725), making it
a useful alternative if those categories are especially important.

## Judge status

`judge.py` was run on a deterministic stratified sample of 50 for R1 and R5. Scores:

| Configuration | Faithfulness | Answer relevance | Correctness | Abstained |
|---|---:|---:|---:|---:|
| R1 | 0.990 | 0.682 | 0.070 | 0.000 |
| R5 | 0.970 | 0.723 | 0.080 | 0.000 |

Both systems have low correctness despite high faithfulness, which points to the simple
extractive answerer failing to isolate the requested fact. R5 has slightly higher correctness
(0.080 vs 0.070) and answer relevance (0.723 vs 0.682), but lower faithfulness (0.970 vs
0.990). The outputs contain individual judge decisions for manual checking.

```bash
uv run python task3_rag/judge.py task3_rag/results/answers_dev.jsonl --sample 50 --out task3_rag/results/judge_r1_sample50.jsonl
uv run python task3_rag/judge.py task3_rag/results/answers_r5_dev.jsonl --sample 50 --out task3_rag/results/judge_r5_sample50.jsonl
```

The judge's stratified sample is deterministic. These first 10 IDs from that sample can be
inspected manually for both configurations:

- [ ] dev004 — paraphrased configuration lookup
- [ ] dev008 — identifier lookup
- [ ] dev009 — version-specific question
- [ ] dev011 — paraphrase question
- [ ] dev012 — paraphrase question
- [ ] dev013 — category-specific factual lookup
- [ ] dev025 — multi-hop evidence coverage
- [ ] dev030 — multi-hop evidence coverage
- [ ] dev032 — multi-hop evidence coverage
- [ ] dev033 — version-specific question

These are actual R1 judge decisions on the shared sample; manually check the verdict against
the question, answer, and retrieved context before relying on the automated judge:

| ID | Faithfulness | Relevance | Correctness | Judge reason summary |
|---|---:|---:|---:|---|
| dev004 | 1.0 | 0.581 | 0.0 | Named `acktracker.max_retries` instead of `dlq.max_retries`. |
| dev008 | 1.0 | 0.791 | 0.0 | Did not state the 4.x default for `metrics.interval_s`. |
| dev009 | 1.0 | 0.789 | 0.0 | Missed the default value `none`. |
| dev011 | 1.0 | 0.702 | 0.5 | Described timeout but missed required setting and added unrelated detail. |
| dev012 | 1.0 | 0.702 | 1.0 | Correctly identified `broker.worker_threads`. |
| dev013 | 1.0 | 0.644 | 0.0 | Discussed a different setting; missed requested default. |
| dev025 | 1.0 | 0.580 | 0.0 | Missed permanent fix (add capacity/reduce load). |
| dev030 | 1.0 | 0.620 | 0.0 | Missed recommended DLQ retry fix. |
| dev032 | 1.0 | 0.608 | 0.0 | Missed worker-thread or queue-capacity fix. |
| dev033 | 1.0 | 0.802 | 0.0 | Missed `spool.interval_s` default `60`. |

R5's decisions on the same deterministic sample are in `results/judge_r5_sample50.jsonl`.

`results/r1_failure_inspection.json` contains five concrete R1 retrieval failure cases for analysis.

## Cost, latency, and token evidence

Measured gateway embedding durations from the existing logs; the first value below is the
document/chunk embedding call (indexing), and the second is the 150-question embedding call.
Per-query latency is the latter divided by 150. Cache warm-up and local vector ranking are
not included; R1 BM25 timings were not logged.

| Configuration | Index embedding time | Query embedding time | Mean query embedding latency | Embedding texts/query | Approx tokens/query |
|---|---:|---:|---:|---:|---|
| R1 BM25 | n/a (no vector index) | not logged | not logged | 0 | not available |
| R2 500/50 | 27.795 s | 2.491 s / 150 | 16.6 ms | 1 question | not logged by gateway client |
| R3 250/50 | 59.251 s | 2.609 s / 150 | 17.4 ms | 1 question | not logged by gateway client |
| R3 500/50 | 34.108 s | 2.204 s / 150 | 14.7 ms | 1 question | not logged by gateway client |
| R3 1000/50 | 20.566 s | 3.019 s / 150 | 20.1 ms | 1 question | not logged by gateway client |
| R4 (R3 500/50 dense leg) | cached | 4.901 s / 150 | 32.7 ms | 1 question | not logged by gateway client |
| R5 lexical + cross-encoder | 0.103 s BM25 indexing | 2.824 s (mean, includes model scoring) | 2.824 s | 50 candidate documents | not logged |

Gateway logs reported zero cost and do not record embedding token counts. Per-query token
usage therefore cannot be truthfully estimated from available logs; count input tokens with
the gateway's tokenizer if exact token usage is required. Timings are from runs here and are
not guaranteed representative of uncached network conditions.

## Five R1 failure diagnoses

See `results/r1_failure_inspection.json` for the question, ground truth, relevant groups,
R1 top-5, generated answer, and R4 top-5 for each case. R1 is a retrieval-only result; its
answer-generation failure is evaluated separately. For multi-hop questions, a retrieved
incident without the second required fix document is a retrieval coverage failure. For
paraphrase questions where the answer-bearing document is absent from top-5, it is a retrieval
failure, not a generator failure. If all required documents are present but the answer misses
the fact, classify that separately as generation failure. This classification is supported
by relevance annotations, not by a successful LLM judge run.

## Test predictions

`predictions_test.jsonl` contains 100 rows in the required format (`id`, `answer`, and up to
five unique `retrieved_doc_ids`), using R1 because it has the highest measured overall recall
and precision. To produce a test prediction set with R5, run `uv run python
task3_rag/run_r5.py --split test` to create its retrieval list, then pair those IDs with answers.
