"""Task 2 – a small coding agent that builds the team website.

The model answers with exactly one JSON object per step:
    {"thought": "short reasoning", "action": "<action name>", "args": {}}
For write_site the HTML follows directly after the JSON in a ```html code block
(or is passed as {"html": str}).

Actions: search_style_guide, read_profiles, write_site, finish.
No native function calling; the protocol is parsed and validated here.

Run from the repository root:
    uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html

Options:
    --model           chat model (default: $LAB_CHAT_MODEL)
    --max-steps       step limit per round, a round = initial task or one feedback round (default 25)
    --k               number of chunks per search_style_guide (default 5)
    --max-tokens      max_tokens per model call (default: not set)
    --embeddings      gateway | local (default gateway)
    --log             log file (default: task2_own_agent/logs/run_XX.jsonl, next free number)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
for p in (str(REPO_ROOT), str(AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import rag  # noqa: E402
from common.embed_images import PLACEHOLDER, embed_images  # noqa: E402
from common.llm_client import LLMClient, count_tokens  # noqa: E402
from common.site_checker import check_site, member_names_from_content  # noqa: E402

DEFAULT_MAX_STEPS = 25
HTML_OMITTED = "[HTML omitted from history: superseded by a later write_site]"

ACTIONS = {
    # action name -> (required args with types, optional args with types)
    "search_style_guide": ({"query": str}, {}),
    "read_profiles": ({}, {}),
    "write_site": ({}, {"html": str}),
    "finish": ({"summary": str}, {}),
}

SYSTEM_PROMPT = """You are a coding agent that builds a single-file team website.
You work in steps. In every step you answer with exactly ONE JSON object and nothing else:

{"thought": "short reasoning", "action": "<action name>", "args": {...}}

After each step you receive an OBSERVATION with the result. Then you take the next step.

Available actions:
1. search_style_guide  args: {"query": "<what you want to know>"}
   Searches the team web style guide and returns the most relevant passages with their
   document ids. You do not know this style guide; it is not in your training data.
   Search for every topic before you write the site (e.g. page structure, metadata,
   colors for day and night mode, theme switching and persistence, fonts, typography,
   spacing, layout and breakpoints, components, images, header, navigation, footer,
   tone and content rules). Use short, specific queries, one topic per query.
   Every passage shows its document status. Follow ACTIVE documents.
2. read_profiles  args: {}
   Returns the profile texts of all team members and the list of available image files.
3. write_site  args: {}
   Put the COMPLETE HTML document in a ```html code block directly after the JSON object.
   The page is saved and checked automatically; you receive the checker report.
   Always send the whole file, not a diff.
4. finish  args: {"summary": "<what you built>"}
   Only accepted after the last write_site passed all checks.

Rules:
- Exactly one action per answer. No text before the JSON object.
- Never write base64 image data. Reference images only with placeholders such as
  <img src="{{image:anna.jpg}}" alt="...">, using file names from read_profiles.
  Placeholders are replaced with the embedded images by the program.
- The website is one standalone HTML file: HTML, CSS and JavaScript inline,
  nothing loaded from the internet (no external fonts, scripts, styles or images).
- If the checker reports failures, search the style guide for the failing topic, fix
  the page and call write_site again with the complete corrected file.
- If you get an ERROR observation, fix your answer format and try again.

Example answers:
{"thought": "I need the member data first.", "action": "read_profiles", "args": {}}

{"thought": "Writing the first version.", "action": "write_site", "args": {}}
```html
<!DOCTYPE html>
<html lang="en">...</html>
```
"""

TASK_DESCRIPTION = """Task: build our team website as one standalone file.

Requirements:
- HTML + CSS + JavaScript only, everything in one file, nothing loaded from the internet.
- Introduce the team and every team member, with everyone's picture
  (use the {{image:<file>}} placeholders).
- Responsive (phone and desktop) and a day/night mode toggle.
- A section with the team's shared view on AI and the future of software engineering.
- Funny and creative.
- The site must also follow the team web style guide. Use search_style_guide to look up its rules.

Start by reading the profiles and searching the style guide. Then write the site,
fix all checker failures, and finish."""


# ------------------------------------------------------------------ parsing


class ProtocolError(Exception):
    """Invalid model output; the message is sent back to the model."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(\{.*?\})\s*```", re.DOTALL)
_HTML_OPEN_RE = re.compile(r"```html[^\n]*\n", re.IGNORECASE)


def split_html_block(text: str) -> tuple[str, str | None]:
    """Return (text without the html block, html or None).

    The closing fence is the LAST ``` after the opening one, so backticks inside the
    page's JavaScript don't end the block early.
    """
    m = _HTML_OPEN_RE.search(text)
    if not m:
        return text, None
    rest = text[m.end():]
    close = rest.rfind("```")
    if close == -1:
        raise ProtocolError(
            "The ```html code block is not closed. Your answer was probably cut off. "
            "Send the complete HTML document again, ending with ```."
        )
    return text[: m.start()] + rest[close + 3:], rest[:close].strip()


def parse_reply(text: str) -> tuple[dict, str | None]:
    """Parse one model answer into (action object, html block or None)."""
    if not text or not text.strip():
        raise ProtocolError("Empty answer. Answer with one JSON object.")
    rest, html = split_html_block(text)

    obj = None
    fenced = _JSON_FENCE_RE.search(rest)
    candidates = [fenced.group(1)] if fenced else []
    start = rest.find("{")
    if start != -1:
        candidates.append(rest[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            obj, _ = decoder.raw_decode(candidate)
            break
        except json.JSONDecodeError:
            continue
    if obj is None:
        raise ProtocolError(
            'Your answer is not valid JSON. Answer with exactly one JSON object like '
            '{"thought": "...", "action": "read_profiles", "args": {}}.'
        )
    validate_action(obj)
    return obj, html


def validate_action(obj) -> None:
    if not isinstance(obj, dict):
        raise ProtocolError("The JSON value must be an object with keys thought, action, args.")
    action = obj.get("action")
    if not isinstance(action, str):
        raise ProtocolError('Missing or invalid "action" (must be a string).')
    if action not in ACTIONS:
        raise ProtocolError(f"Unknown action {action!r}. Valid actions: {', '.join(ACTIONS)}.")
    if "thought" in obj and not isinstance(obj["thought"], str):
        raise ProtocolError('"thought" must be a string.')
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError('"args" must be a JSON object, e.g. "args": {}.')

    required, optional = ACTIONS[action]
    expected = {**required, **optional}
    usage = json.dumps({name: t.__name__ for name, t in expected.items()})
    unknown = sorted(set(args) - set(expected))
    if unknown:
        raise ProtocolError(f"Unknown args for {action}: {', '.join(unknown)}. Expected args: {usage}.")
    for name, typ in required.items():
        if name not in args:
            raise ProtocolError(f"Missing arg {name!r} for {action}. Expected args: {usage}.")
    for name, value in args.items():
        if not isinstance(value, expected[name]):
            raise ProtocolError(f"Arg {name!r} for {action} must be a {expected[name].__name__}.")
        if isinstance(value, str) and not value.strip():
            raise ProtocolError(f"Arg {name!r} for {action} must not be empty.")
    obj["args"] = args


# ------------------------------------------------------------------- agent


class Agent:
    def __init__(self, llm: LLMClient, index: rag.StyleGuideIndex, content_dir: Path, out_path: Path,
                 k: int = rag.DEFAULT_K, max_steps: int = DEFAULT_MAX_STEPS, max_tokens: int | None = None,
                 input_fn=input) -> None:
        self.llm = llm
        self.index = index
        self.content_dir = content_dir
        self.images_dir = content_dir / "images"
        self.out_path = out_path
        self.k = k
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.input_fn = input_fn
        self.member_names = member_names_from_content(content_dir)

        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TASK_DESCRIPTION},
        ]
        self.html_message_idx: int | None = None  # the one assistant message whose HTML we keep
        self.last_check_passed = False
        self.last_report = None
        self.step = 0  # global step counter (for logs)

    # ------------------------------------------------------------ main loop

    def run(self) -> bool:
        """Run until the human ends the session (True) or a step limit is hit (False)."""
        round_no = 1
        while True:
            outcome = self._run_round(round_no)
            if outcome != "finished":
                return False
            feedback = self._ask_feedback()
            if not feedback:
                self.llm.log_event({"kind": "end", "step": self.step, "reason": "empty feedback"})
                print("\nNo feedback. Done.")
                return True
            round_no += 1
            self.llm.log_event({"kind": "human_feedback", "step": self.step, "round": round_no,
                                "feedback": feedback})
            self.messages.append({"role": "user", "content":
                                  f"HUMAN FEEDBACK:\n{feedback}\n\nApply this feedback. Then call write_site "
                                  "with the complete updated page; finish is accepted again only after it "
                                  "passes all checks."})
            self.last_check_passed = False  # the feedback has to be applied and checked again

    def _run_round(self, round_no: int) -> str:
        for round_step in range(1, self.max_steps + 1):
            self.step += 1
            print(f"\n=== Round {round_no}, step {round_step}/{self.max_steps} (total {self.step}) ===")
            self.llm.log_event({"kind": "step_start", "step": self.step, "round": round_no,
                                "round_step": round_step})
            try:
                reply = self.llm.chat(self.messages, max_tokens=self.max_tokens)
            except Exception as exc:  # gateway errors after the client's own retries
                print(f"Model call failed: {exc}")
                self.llm.log_event({"kind": "model_error", "step": self.step, "error": str(exc)})
                return "error"
            self.llm.log_event({"kind": "model_reply", "step": self.step, "content": reply})
            self._append_assistant(reply)

            try:
                obj, html = parse_reply(reply)
            except ProtocolError as exc:
                self._observe("invalid", f"ERROR: {exc}")
                continue

            action, args = obj["action"], obj["args"]
            print(f"thought: {obj.get('thought', '')}")
            print(f"action:  {action} {json.dumps({k: v for k, v in args.items() if k != 'html'})}")
            self.llm.log_event({"kind": "action", "step": self.step, "action": action,
                                "thought": obj.get("thought"),
                                "args": {k: v for k, v in args.items() if k != "html"}})

            if action == "search_style_guide":
                self._observe(action, self._search(args["query"]))
            elif action == "read_profiles":
                self._observe(action, self._read_profiles())
            elif action == "write_site":
                self._observe(action, self._write_site(args.get("html") or html))
            elif action == "finish":
                if self.last_check_passed:
                    print(f"\nFINISH accepted: {args['summary']}")
                    self.llm.log_event({"kind": "finish_accepted", "step": self.step, "summary": args["summary"]})
                    return "finished"
                self._observe(action, self._finish_rejected())

        msg = f"Step limit of {self.max_steps} steps reached in round {round_no}. Stopping."
        print(f"\n{msg}")
        self.llm.log_event({"kind": "step_limit", "step": self.step, "round": round_no, "message": msg})
        return "step_limit"

    # -------------------------------------------------------------- actions

    def _search(self, query: str) -> str:
        chunks = self.index.search(query, self.k)
        text = rag.format_observation(chunks, rag.OBSERVATION_TOKEN_BUDGET)
        sources = ", ".join(dict.fromkeys(c.doc_id for c in chunks)) or "none"
        return f"Style guide results for {query!r} (sources: {sources}):\n\n{text}"

    def _read_profiles(self) -> str:
        profiles = sorted(p for p in self.content_dir.glob("*.md") if not p.name.startswith("_"))
        if not profiles:
            return f"ERROR: no profile files found in {self.content_dir}"
        parts = [f"--- {p.name} ---\n{p.read_text(encoding='utf-8').strip()}" for p in profiles]
        images = sorted(p.name for p in self.images_dir.iterdir() if p.is_file()) if self.images_dir.is_dir() else []
        parts.append("Available image files (use as {{image:<file>}}): " + (", ".join(images) or "none"))
        parts.append("Team members: " + ", ".join(self.member_names))
        return "\n\n".join(parts)

    def _write_site(self, html: str | None) -> str:
        if not html or not html.strip():
            return ("ERROR: no HTML found. Put the complete page in a ```html code block directly "
                    "after the JSON object.")
        if re.search(r"data:[\w/+.-]+;base64,", html):
            return ("ERROR: the HTML contains base64 data. Never write base64; use "
                    "{{image:<file>}} placeholders for images.")
        if not PLACEHOLDER.search(html):
            # not an error by itself (checker decides), but a useful hint
            hint = "\nNote: the page contains no {{image:...}} placeholder."
        else:
            hint = ""
        try:
            final_html = embed_images(html, self.images_dir)
        except (FileNotFoundError, ValueError) as exc:
            return f"ERROR: {exc}. The page was not saved."

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(final_html, encoding="utf-8")
        size = self.out_path.stat().st_size
        report = check_site(final_html, mode="full", member_names=self.member_names, file_size=size)
        self.last_report = report
        self.last_check_passed = report.passed
        self.llm.log_event({"kind": "check_report", "step": self.step, "passed": report.passed,
                            "report": report.to_dict()})
        status = "ALL CHECKS PASSED. You may call finish." if report.passed else \
            "Some checks FAILED. Fix them and call write_site again with the complete page."
        return (f"Saved {self.out_path} ({size / 1024:.0f} KB).{hint}\n"
                f"Checker report (failures only):\n{report.summary(only_failures=True)}\n{status}")

    def _finish_rejected(self) -> str:
        if self.last_report is None:
            return "ERROR: finish rejected. You have not written the site yet. Use write_site first."
        if self.last_report.passed:  # passed earlier, but human feedback has not been applied yet
            return ("ERROR: finish rejected. Apply the human feedback and call write_site with the "
                    "updated page first.")
        failing = "\n".join(f"[FAIL] {r.name}: {r.message}" for r in self.last_report.failures())
        return f"ERROR: finish rejected. The last checker report did not pass. Failing checks:\n{failing}"

    # -------------------------------------------------------------- helpers

    def _append_assistant(self, reply: str) -> None:
        """Append the reply; keep the full HTML only in the latest message that contains HTML."""
        self.messages.append({"role": "assistant", "content": reply})
        if _HTML_OPEN_RE.search(reply) or '"html"' in reply:
            if self.html_message_idx is not None:
                old = self.messages[self.html_message_idx]
                old["content"] = _strip_html(old["content"])
            self.html_message_idx = len(self.messages) - 1

    def _observe(self, action: str, observation: str) -> None:
        print(f"observation ({count_tokens(observation)} tokens): "
              + (observation[:300] + " ..." if len(observation) > 300 else observation))
        self.llm.log_event({"kind": "observation", "step": self.step, "action": action,
                            "observation": observation})
        self.messages.append({"role": "user", "content": f"OBSERVATION (step {self.step}, {action}):\n{observation}"})

    def _ask_feedback(self) -> str:
        print("\nThe site is at", self.out_path)
        try:
            return self.input_fn("Feedback for the agent (empty = done): ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


def _strip_html(content: str) -> str:
    """Remove the page from an old assistant message (both protocol variants)."""
    stripped = re.sub(r"```html[^\n]*\n.*?(```|$)", HTML_OMITTED, content, count=1,
                      flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r'("html"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"' + HTML_OMITTED + '"', stripped,
                      flags=re.DOTALL)
    return stripped


# --------------------------------------------------------------------- CLI


def next_log_path(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    numbers = [int(m.group(1)) for p in log_dir.glob("run_*.jsonl") if (m := re.match(r"run_(\d+)$", p.stem))]
    return log_dir / f"run_{(max(numbers) + 1 if numbers else 1):02d}.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Small coding agent that builds the team website.")
    parser.add_argument("--content", type=Path, default=REPO_ROOT / "task1_opencode" / "content")
    parser.add_argument("--out", type=Path, default=AGENT_DIR / "site" / "index.html")
    parser.add_argument("--model", default=None, help="chat model (default: $LAB_CHAT_MODEL)")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="step limit per round")
    parser.add_argument("--k", type=int, default=rag.DEFAULT_K, help="chunks per style guide search")
    parser.add_argument("--max-tokens", type=int, default=None, help="max_tokens per call (default: not set)")
    parser.add_argument("--embeddings", choices=["gateway", "local"], default="gateway")
    parser.add_argument("--log", type=Path, default=None, help="log file (default: logs/run_XX.jsonl)")
    args = parser.parse_args()

    if not args.content.is_dir():
        sys.exit(f"Content directory not found: {args.content}")
    log_path = args.log or next_log_path(AGENT_DIR / "logs")
    llm = LLMClient(model=args.model, log_path=log_path)
    print(f"Log: {log_path}\nModel: {llm.model}")

    print("Building style guide index ...")
    index = rag.get_index(llm=llm, backend=args.embeddings)
    print(f"{len(index.documents)} documents, {len(index.chunks)} chunks")
    llm.log_event({"kind": "config", "model": llm.model, "content": str(args.content), "out": str(args.out),
                   "max_steps": args.max_steps, "k": args.k, "max_tokens": args.max_tokens,
                   "embeddings": args.embeddings, "n_chunks": len(index.chunks)})

    agent = Agent(llm, index, args.content, args.out, k=args.k, max_steps=args.max_steps,
                  max_tokens=args.max_tokens)
    agent.run()
    print(f"\nUsage: {llm.usage_summary()}\nLog: {log_path}")


if __name__ == "__main__":
    main()
