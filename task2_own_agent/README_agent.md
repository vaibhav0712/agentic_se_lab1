# Task 2 – Our own coding agent

A small coding agent that builds the team website (`site/index.html`) from the profiles in
`task1_opencode/content/`. It follows the UPB Agentic SE Web Style Guide by retrieving the rules it
needs. No frameworks: only the standard library, `numpy`, `tiktoken` and the `common/` helpers.

| File | Purpose |
|---|---|
| `rag.py` | Loads, chunks and embeds the style guide; `search(query, k)` returns the most similar chunks with their document ids |
| `agent.py` | The agent loop: JSON action protocol, actions, step limit, checker gate, human feedback, logging |
| `site/index.html` | The website produced by the agent |
| `logs/run_XX.jsonl` | Complete log of each run |

## How to run

One-time setup, from the repository root:

```bash
uv sync
```

Environment variables (in `.env` or exported in the shell): `UPB_AI_GATEWAY_KEY`,
`UPB_AI_GATEWAY_BASE_URL`, `LAB_CHAT_MODEL`, `LAB_EMBEDDING_MODEL` (see `.env.example`).
You need the university network or VPN.

Run the agent:

```bash
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```

The agent prints every step (thought, action, a short preview of the observation). After an accepted
`finish`, it asks for feedback on the terminal:
- type feedback and press Enter → the agent continues and applies it
- press Enter on an empty line → the run ends

Options:

| Option | Default | Meaning |
|---|---|---|
| `--content` | `task1_opencode/content` | directory with `<firstname>.md` profiles and `images/` |
| `--out` | `task2_own_agent/site/index.html` | output file |
| `--model` | `$LAB_CHAT_MODEL` | chat model, e.g. `kiconnect.qwen3.8-27b` (free fallback) |
| `--max-steps` | `25` | step limit per round (initial task and each feedback round) |
| `--k` | `5` | chunks returned per `search_style_guide` |
| `--max-tokens` | not set | `max_tokens` per model call (not set, so long pages are not cut off) |
| `--embeddings` | `gateway` | `gateway` (`llm.embed`) or `local` (sentence-transformers) |
| `--log` | `task2_own_agent/logs/run_XX.jsonl` | log file; by default the next free run number |

Test the retrieval on its own:

```bash
uv run python task2_own_agent/rag.py "night mode background color" -k 5
```

(options: `--backend`, `--chunk-tokens`, `--overlap-tokens`, `--include-deprecated`)

Check the result:

```bash
uv run python task2_own_agent/check_site.py task2_own_agent/site/index.html --content task1_opencode/content
```

## Design decisions

### Overview

```
          ┌──────────── messages (system prompt, task, history) ────────────┐
          ▼                                                                 │
     LLM (gateway) ──► one JSON object (+ ```html block) ──► parse + validate
                                                                 │
              ┌─────────────────┬────────────────┬──────────────┴──┬──────────────┐
              ▼                 ▼                ▼                 ▼              ▼
     search_style_guide   read_profiles     write_site          finish       invalid output
        rag.search        content/*.md   embed_images + save   accepted only  ERROR observation
     (≤ 1,000 tokens)     + image list   + check_site(full)    if last check  (counts as step)
              │                 │                │             passed
              └─────────────────┴────── OBSERVATION ───────────────┘──► human feedback
                                                                          (empty = end)
```

### Chunking (`rag.py`)
- Each document's header (`# SG-xx: title`, `Status:`, `Category:`) is parsed into metadata: id, title,
  status, category and `replaced_by` for deprecated documents. The header lines are removed from the
  text before chunking.
- The body is split into **fixed-size token windows of 120 tokens with 30 tokens of overlap**, counted
  with tiktoken `cl100k_base`. Windows are built word by word, so they never cut a word in half, and
  line breaks are kept so Markdown lists stay readable. The 55 documents give 99 chunks.
- Why this size: the documents are short (at most ~150 words), so most fit into one or two windows.
  120 tokens is small enough that 5 results fit well under the 1,000-token budget. The overlap keeps a
  rule that sits on a window boundary complete in at least one chunk.
- Each chunk carries a source line, `[SG-13: Night mode palette (version 2) | Status: ACTIVE (version 2)]`.
  It is embedded together with the chunk text, so a window from the middle of a document still knows
  which topic it belongs to, and the model sees the source id of every passage.

### Embedding model
- Default: the gateway embedding model (`$LAB_EMBEDDING_MODEL`, `kiconnect.qwen3-embedding-8b`) via
  `llm.embed(...)`. It is free, needs no extra dependencies, and the calls land in the same log as the
  chat.
- Optional: `--embeddings local` uses sentence-transformers (`all-MiniLM-L6-v2`) for offline use.
- The vectors are L2-normalised and kept in memory as one numpy matrix, so cosine similarity is a
  single matrix–vector product (`matrix @ q`). The index is built once per run (99 chunks, a few
  seconds); there is no disk cache and no vector database.

### Retrieval, k and the context budget
- `search(query, k)` returns the top **k = 5** chunks, each with its text, `doc_id` and score.
- **Deprecated documents are excluded** from search results by default. The guide keeps old versions
  for history (e.g. SG-12 is replaced by SG-13), and returning them would give the model conflicting
  rules. The status and `replaced_by` stay in the chunk metadata, and `--include-deprecated` shows them
  in the `rag.py` CLI.
- k = 5 lets one query cover a topic that is spread over several documents (e.g. colors: palette,
  accent, contrast), while the observation stays small (typically 300–600 tokens).
- **1,000-token budget:** `format_observation()` measures the rendered observation with `count_tokens`
  and adds chunks in rank order until the budget is reached; a chunk that does not fit is cut at a word
  boundary. The source lines are counted too, which is stricter than the rule.
- The system prompt and task description contain no style guide text. The model only sees style guide
  rules through `search_style_guide` observations.

### Prompt design (`agent.py`)
- **System prompt:** the protocol (one JSON object `{"thought", "action", "args"}` per answer), the four
  actions with their args, the rules (exactly one action per answer, never base64, only
  `{{image:<file>}}` placeholders, one standalone file, always send the complete page) and two example
  answers. It tells the model that it does not know the style guide and must search for each topic with
  short, specific queries before writing, and that it should follow ACTIVE documents.
- **Task description:** the Task 1 requirements (standalone file, every member with picture,
  responsive, day/night toggle, section on AI and the future of software engineering, funny), plus
  "follow the style guide, look up its rules with `search_style_guide`".
- **HTML outside the JSON:** for `write_site` the page goes into a ```` ```html ```` block directly after
  the JSON object, so the model does not have to escape thousands of quotes and newlines. `{"html": str}`
  is accepted as well.
- **Observations** are sent as user messages `OBSERVATION (step N, action): ...`. The checker
  observation lists only the failing checks plus the pass count, and says what to do next ("fix them and
  call write_site again" or "you may call finish").
- **History:** only the latest assistant message that contains HTML keeps the full page. Older pages
  are replaced by `[HTML omitted from history: superseded by a later write_site]`, which saves several
  thousand prompt tokens per step after a rewrite. The log still contains every message in full.
- `max_tokens` is not set by default, because a complete page plus the hidden reasoning tokens needs
  many output tokens.

### Actions
| Action | What the code does |
|---|---|
| `search_style_guide {"query"}` | `index.search(query, k)` → `format_observation` (≤ 1,000 tokens), with the list of source ids |
| `read_profiles {}` | all `content/*.md` except `_template.md`, the image file names and the member names |
| `write_site {}` + html block | rejects base64 data, runs `embed_images` (unknown image → error, nothing is saved), saves the file, runs `check_site(..., mode="full")` with the member names from the content directory |
| `finish {"summary"}` | accepted only if the last checker report passed; otherwise the failing checks are returned |

### How invalid output is handled
`parse_reply()` and `validate_action()` turn every problem into a `ProtocolError`. Its message goes back
to the model as an `ERROR: ...` observation, the step is counted, and the loop continues. It never
crashes. The cases:
- empty answer or no parsable JSON (a ```` ```json ```` fence or leading text is tolerated; the HTML block is
  removed before the JSON is searched, so CSS braces don't confuse the parser)
- the JSON is not an object, `action` is missing or unknown, `args` is not an object
- unknown, missing, wrongly typed or empty args (the error shows the expected args, e.g.
  `{"query": "str"}`)
- an unclosed ```` ```html ```` block: the answer was probably cut off, so the model is asked to send the
  complete page again
- `write_site` without HTML, with base64 data, or with an unknown image placeholder
- `finish` before any `write_site`, after a failed check, or after human feedback that has not been
  applied yet
- model call errors after the client's retries end the run with a logged `model_error` instead of a
  stack trace

### Step limit and human in the loop
- The step limit (`--max-steps`, default 25) applies **per round**. Round 1 is the initial task; each
  feedback round gets a fresh budget, so a long first round cannot block the feedback round. When the
  limit is reached, the agent prints `Step limit of N steps reached in round R. Stopping.` and logs a
  `step_limit` event.
- After an accepted `finish`, the user is asked for feedback. Non-empty feedback is appended as a user
  message (`HUMAN FEEDBACK: ...`), and the "last check passed" state is reset. The model must apply the
  feedback with `write_site` and pass the checker again before `finish` is accepted. Empty input
  (or Ctrl-D / Ctrl-C) ends the run.

### Logs
`LLMClient(log_path=logs/run_XX.jsonl)` logs every request and response, with tokens, cost and
`finish_reason`. The agent adds its own events with `llm.log_event`, each with the step number:
`config`, `step_start`, `model_reply`, `action`, `observation`, `check_report`, `finish_accepted`,
`human_feedback`, `step_limit`, `model_error`, `end`.

Example: `logs/run_01.jsonl` (model `openai.gpt-6-luna`) took 23 steps:
- `read_profiles`, then 18 style guide searches
- a first `write_site` that failed one check (`theme mechanism`), one more search about theme
  switching, and a second `write_site` that passed
  25/25 checks
- an accepted `finish`

Total cost about $0.008.

## Known limitations
- **The model only knows the rules it searches for.** If it never asks about a topic, those rules are
  missed. In practice the checker report reveals the gap, but this costs extra steps. A typical run
  spends most of its steps searching (18 of 23 in run_01), which is close to the default limit of 25;
  use `--max-steps` for weaker models.
- **Fixed-size chunks ignore document structure.** A window can start or end in the middle of a list or
  sentence, and a rule can be split across two chunks. The overlap and the source line reduce this but
  don't remove it.
- **Pure dense retrieval.** Exact identifiers (hex colors, CSS class names, storage keys) can rank lower
  than with keyword search (BM25 or hybrid). There is no re-ranking.
- **Deprecated documents are filtered by their status line.** This relies on the `Status: DEPRECATED`
  header format. Contradictions inside ACTIVE documents are not detected.
- **The 1,000-token budget uses `count_tokens` (tiktoken `cl100k_base`).** The gateway models use other
  tokenizers, so the real token count can differ slightly.
- **Every `write_site` resends the complete page** (several thousand output tokens), even for small
  fixes. There is no diff or patch action.
- **Truncated answers are detected only by an unclosed ```` ```html ```` block** and get a retry
  message. `finish_reason` is logged by `LLMClient` but not read by the agent.
- **Lenient JSON extraction.** Text after the first JSON object is ignored, and if a reply contains
  several actions only the first is run.
- **The checker is the only quality gate.** "Funny and creative" and rules the checker doesn't test are
  only as good as the model's output. The human feedback round is meant for that.
- **Single page, single run.** The output file is overwritten by every successful `write_site`, so the
  submitted site always belongs to the latest run's log.
