"""Contained tool implementations for the tinyledger agent exercise."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import ntpath
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from tool_contract import (
    MAX_LIST_ENTRIES,
    MAX_OUTPUT_CHARS,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    ToolRegistryBase,
    ToolResult,
)


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a workspace-relative directory. Results are workspace-relative paths.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "path": _string("Workspace-relative directory to list; defaults to the workspace root."),
                    "pattern": _string("Optional glob pattern to filter returned paths."),
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read numbered lines from a text file inside the workspace.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "path": _string("Workspace-relative path of the file to read", minLength=1),
                    "start_line": _integer("First 1-based line to include; defaults to 1", minimum=1),
                    "end_line": _integer("Last 1-based line to include, inclusive", minimum=1),
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search workspace text files for a regular-expression pattern.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "pattern": _string("Regular expression to search for", minLength=1),
                    "glob": _string("Optional workspace-relative glob selecting files; defaults to all files."),
                    "max_results": _integer(
                        "Maximum number of matching lines to return",
                        minimum=1,
                        maximum=MAX_SEARCH_RESULTS,
                    ),
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run pytest in the workspace and return structured pass/fail details.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "target": _string(
                        "Optional workspace-relative pytest file or node id (for example tests/test_x.py::test_name)."
                    ),
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cli",
            "description": "Run the tinyledger CLI with arguments, using only workspace files and a minimal environment.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array",
                        "description": "Arguments after `python -m tinyledger`; the CSV file must be a workspace-relative path.",
                        "items": _string("One command-line argument"),
                        "minItems": 2,
                    },
                },
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one unique text match in an existing regular file under src/ only.",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "path": _string("Workspace-relative path of an existing file below src/", minLength=1),
                    "old_str": _string("Non-empty exact text that must occur exactly once", minLength=1),
                    "new_str": _string("Replacement text"),
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolRegistry(ToolRegistryBase):
    """Implements file, search, test, CLI, and narrowly scoped edit tools."""

    def schemas(self) -> list[dict[str, Any]]:
        return json.loads(json.dumps(_SCHEMAS))

    def execute(self, name: str, arguments: str) -> ToolResult:
        """Validate raw arguments and execute a tool without leaking exceptions."""
        try:
            if not isinstance(name, str) or not isinstance(arguments, str):
                return self._bound(ToolResult(False, error="Tool name and arguments must be strings."))
            handlers = {
                "list_files": self._list_files,
                "read_file": self._read_file,
                "search_code": self._search_code,
                "run_tests": self._run_tests,
                "run_cli": self._run_cli,
                "edit_file": self._edit_file,
            }
            if name not in handlers:
                return self._bound(ToolResult(False, error=f"Unknown tool: {name}"))
            try:
                value = json.loads(arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                return self._bound(ToolResult(False, error=f"Invalid JSON arguments: {exc}"))
            schema = next(item["function"]["parameters"] for item in _SCHEMAS if item["function"]["name"] == name)
            errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(error.path))
            if errors:
                return self._bound(ToolResult(False, error=f"Invalid arguments for {name}: {errors[0].message}"))

            # The optional path defaults are applied only after schema validation.
            if name == "list_files":
                value.setdefault("path", ".")
            if name == "read_file":
                value.setdefault("start_line", 1)
            result = handlers[name](**value)
            if not isinstance(result, ToolResult):
                return self._bound(ToolResult(False, error="Tool implementation returned an invalid result."))
            return self._bound(result)
        except Exception as exc:  # execute is an untrusted-input boundary by contract
            return self._bound(ToolResult(False, error=f"Tool execution failed: {type(exc).__name__}: {exc}"))

    @property
    def _root(self) -> Path:
        return self.workspace.resolve()

    def _resolve(self, raw_path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("Path must be a non-empty workspace-relative string.")
        candidate = Path(raw_path)
        if candidate.is_absolute() or ntpath.isabs(raw_path) or ".." in candidate.parts or ".." in raw_path.split("\\"):
            raise ValueError("Absolute paths and parent-directory traversal are not allowed.")
        target = self._root / candidate
        resolved = target.resolve(strict=must_exist)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Path resolves outside the workspace.") from exc
        return resolved

    def _minimal_env(self, *, pythonpath: str) -> dict[str, str]:
        # Do not inherit arbitrary variables (API keys, tokens, or user settings).
        return {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self._root),
            "LANG": "C.UTF-8",
            "PYTHONPATH": pythonpath,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _list_files(self, path: str = ".", pattern: str | None = None) -> ToolResult:
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ValueError("Path is not a directory.")
        if pattern is not None and (
            Path(pattern).is_absolute()
            or ntpath.isabs(pattern)
            or ".." in Path(pattern).parts
            or ".." in pattern.split("\\")
        ):
            raise ValueError("Glob patterns must not be absolute or traverse parent directories.")
        paths: list[str] = []
        for item in directory.rglob("*"):
            try:
                actual = item.resolve(strict=True)
                actual.relative_to(self._root)
            except (OSError, ValueError):
                continue
            if not actual.is_file() and not actual.is_dir():
                continue
            rel = item.relative_to(self._root).as_posix()
            if pattern and not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(item.name, pattern):
                continue
            paths.append(rel)
            if len(paths) > MAX_LIST_ENTRIES:
                break
        truncated = len(paths) > MAX_LIST_ENTRIES
        return ToolResult(True, output=sorted(paths[:MAX_LIST_ENTRIES]), truncated=truncated)

    def _read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError("Path is not a regular file.")
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line must be greater than or equal to start_line.")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = start_line - 1
        last = min(len(lines), end_line if end_line is not None else len(lines), first + MAX_READ_LINES)
        selected = [f"{index + 1}: {lines[index]}" for index in range(first, last)]
        truncated = last < len(lines) and (end_line is None or last < end_line)
        return ToolResult(True, output="\n".join(selected), truncated=truncated)

    def _search_code(
        self, pattern: str, glob: str | None = None, max_results: int = MAX_SEARCH_RESULTS
    ) -> ToolResult:
        if glob is not None and (
            Path(glob).is_absolute()
            or ntpath.isabs(glob)
            or ".." in Path(glob).parts
            or ".." in glob.split("\\")
        ):
            raise ValueError("Search glob must not be absolute or traverse parent directories.")
        regex = re.compile(pattern)
        candidates = self._root.glob(glob or "**/*")
        matches: list[str] = []
        truncated = False
        for file in candidates:
            try:
                resolved = file.resolve(strict=True)
                resolved.relative_to(self._root)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = file.relative_to(self._root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{number}: {line}")
                    if len(matches) > max_results:
                        truncated = True
                        return ToolResult(True, output=matches[:max_results], truncated=truncated)
        return ToolResult(True, output=matches, truncated=truncated)

    def _run_tests(self, target: str | None = None) -> ToolResult:
        command = [sys.executable, "-m", "pytest", "-q"]
        if target is not None:
            # Keep pytest's option parser and node-id syntax from becoming a
            # second command interface. Only accept conventional test targets.
            if target.startswith("-") or not re.fullmatch(
                r"tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_./:-]+)?", target
            ):
                raise ValueError("Test target must be a tests/*.py path or a test node id below tests/.")
            path_text, separator, node = target.partition("::")
            test_path = self._resolve(path_text)
            if not test_path.is_file():
                raise ValueError("Test target must name an existing file in the workspace.")
            if test_path.is_symlink():
                raise ValueError("Test target must not be a symlink.")
            relative = test_path.relative_to(self._root).as_posix()
            command.append(relative + (f"::{node}" if separator else ""))

        with tempfile.TemporaryDirectory(prefix=".task4-pytest-", dir=self._root) as report_dir:
            report_path = Path(report_dir) / "report.xml"
            command.insert(3, f"--junitxml={report_path}")
            try:
                completed = subprocess.run(
                    command,
                    cwd=self._root,
                    env=self._minimal_env(pythonpath=str(self._root / "src")),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Tests exceeded the {self.timeout_s:g}s timeout.") from exc
            try:
                report = ET.parse(report_path).getroot() if report_path.exists() else None
            except ET.ParseError:
                report = None

        passed = failed = 0
        failing_tests: list[str] = []
        tracebacks: list[dict[str, str]] = []
        if report is not None:
            for suite in [report] if report.tag == "testsuite" else list(report.iter("testsuite")):
                passed += int(suite.attrib.get("tests", "0")) - int(suite.attrib.get("failures", "0")) - int(suite.attrib.get("errors", "0")) - int(suite.attrib.get("skipped", "0"))
                failed += int(suite.attrib.get("failures", "0")) + int(suite.attrib.get("errors", "0"))
            for case in report.iter("testcase"):
                issue = next((child for child in case if child.tag in {"failure", "error"}), None)
                if issue is not None:
                    test_name = "::".join(part for part in (case.attrib.get("classname", ""), case.attrib.get("name", "")) if part)
                    failing_tests.append(test_name)
                    tracebacks.append({"test": test_name, "traceback": (issue.attrib.get("message", "") + "\n" + (issue.text or "")).strip()[-2000:]})
        summary = {
            "passed": max(0, passed),
            "failed": failed,
            "failing_tests": failing_tests,
            "tracebacks": tracebacks,
            "exit_code": completed.returncode,
        }
        if report is None and completed.stdout:
            summary["pytest_output"] = completed.stdout[-2000:]
        if report is None and completed.stderr:
            summary["pytest_error"] = completed.stderr[-2000:]
        return ToolResult(True, output=summary)

    def _run_cli(self, args: list[str]) -> ToolResult:
        if len(args) < 2 or args[0] not in {"report", "balances", "settle"}:
            raise ValueError("CLI args must start with report, balances, or settle and include a CSV path.")
        if len(args) != 2 and not (len(args) == 4 and args[2] == "--month" and re.fullmatch(r"\d{4}-\d{2}", args[3])):
            raise ValueError("CLI accepts only an optional --month YYYY-MM after the CSV path.")
        if args[1].startswith("-"):
            raise ValueError("CLI CSV path must be a workspace-relative file path, not an option.")
        csv_file = self._resolve(args[1])
        if not csv_file.is_file():
            raise ValueError("CLI CSV argument must be an existing regular file in the workspace.")
        safe_args = [args[0], csv_file.relative_to(self._root).as_posix(), *args[2:]]
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "tinyledger", *safe_args],
                cwd=self._root,
                env=self._minimal_env(pythonpath=str(self._root / "src")),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"CLI exceeded the {self.timeout_s:g}s timeout.") from exc
        return ToolResult(
            True,
            output={"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr},
        )

    def _edit_file(self, path: str, old_str: str, new_str: str) -> ToolResult:
        candidate = Path(path)
        if candidate.is_absolute() or ntpath.isabs(path) or ".." in candidate.parts or ".." in path.split("\\"):
            raise ValueError("Edit paths must be relative and must not traverse parent directories.")
        if not candidate.parts or candidate.parts[0] != "src":
            raise ValueError("edit_file may only change existing files below src/.")

        # Reject a symlink at every component, even if it points back into src/.
        current = self._root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("edit_file does not allow symlinks.")
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError("edit_file requires an existing regular file.")
        original = target.read_text(encoding="utf-8")
        occurrences = original.count(old_str)
        if occurrences != 1:
            raise ValueError(f"old_str must match exactly once; found {occurrences} matches.")
        updated = original.replace(old_str, new_str, 1)
        before = original.splitlines(keepends=True)
        after = updated.splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(before, after, fromfile=f"a/{candidate.as_posix()}", tofile=f"b/{candidate.as_posix()}")
        )
        target.write_text(updated, encoding="utf-8")
        return ToolResult(True, output=diff)

    @staticmethod
    def _bound(result: ToolResult) -> ToolResult:
        """Keep the complete serialized tool response within the contract limit."""
        if len(result.to_content()) <= MAX_OUTPUT_CHARS:
            return result
        result.truncated = True
        if result.error:
            result.error = result.error[: MAX_OUTPUT_CHARS - 100]
        output = result.output
        if isinstance(output, str):
            result.output = output[: max(0, MAX_OUTPUT_CHARS - 100)]
        elif isinstance(output, list):
            # Preserve as many complete entries as fit rather than emitting malformed JSON.
            shortened: list[Any] = []
            for entry in output:
                shortened.append(entry)
                result.output = shortened
                if len(result.to_content()) > MAX_OUTPUT_CHARS - 32:
                    shortened.pop()
                    break
            result.output = shortened
        elif isinstance(output, dict):
            # Shrink large text fields first; then remove trailing details if needed.
            result.output = output
            for key, value in list(output.items()):
                if isinstance(value, str) and len(result.to_content()) > MAX_OUTPUT_CHARS - 32:
                    output[key] = value[: max(0, len(value) - (len(result.to_content()) - MAX_OUTPUT_CHARS + 100))]
                elif isinstance(value, list):
                    while value and len(result.to_content()) > MAX_OUTPUT_CHARS - 32:
                        value.pop()
            if len(result.to_content()) > MAX_OUTPUT_CHARS:
                result.output = {"message": "Output exceeded the tool response limit and was truncated."}
        if len(result.to_content()) > MAX_OUTPUT_CHARS:
            result.output = "Output truncated."
        if len(result.to_content()) > MAX_OUTPUT_CHARS:
            result.error = "Tool response exceeded the output limit."
            result.output = None
            result.truncated = True
        return result
