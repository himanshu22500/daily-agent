"""Run model work through the local Claude Code CLI (issue #95).

``claude-code:<model>`` model strings (e.g. ``claude-code:sonnet``,
``claude-code:haiku``) run headless ``claude -p`` with the Mac's signed-in
Claude Code, instead of an API key or cloud credentials that expire.

Every call is locked down, because prompts carry untrusted transcript text:

- ``--tools ""``: no tools at all (no shell, no file access, no web);
  ``--strict-mcp-config`` with no config: no MCP servers.
- ``disableAllHooks``: the user's ``SessionEnd`` hook would otherwise start a
  flush that calls ``claude`` again.
- ``--no-session-persistence``: no transcript is written, so the sweep never
  reads its own prompts back as a "session".
- ``--system-prompt`` replaces Claude Code's agent prompt with the task's, and
  the run happens in an empty directory, so no project ``CLAUDE.md`` is loaded.
- ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` are not passed on, so the
  call always uses Claude Code's own sign-in.

Images (issue #107) are sent as base64 image blocks in a single
``--input-format stream-json`` user message, so the model can read them
without being given any tool to open files.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

PREFIX = "claude-code:"
DEFAULT_TIMEOUT_SECONDS = 300.0
# Dropped from the child's environment:
# - set by a parent Claude Code session (e.g. when the SessionEnd hook runs a
#   flush); a child `claude -p` must start as a fresh top-level run;
# - API credentials, which would override Claude Code's own sign-in. The CLI
#   loads `.env` into the environment, and a stale key there turned every
#   background call into "401 Invalid API key" (issue #97). Claude Code's own
#   settings (e.g. `apiKeyHelper`) still apply.
_DROPPED_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


# What Claude accepts as image input.
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


class ClaudeCodeError(RuntimeError):
    """``claude -p`` failed, timed out, or returned no usable output."""


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    structured: Any
    cost_usd: float


@dataclass(frozen=True)
class ImageInput:
    media_type: str  # one of IMAGE_TYPES
    data: bytes


@dataclass
class Spend:
    """Running totals for this process (what Claude Code reports per call)."""

    calls: int = 0
    cost_usd: float = 0.0


SPEND = Spend()


def is_claude_code(model: str) -> bool:
    return isinstance(model, str) and model.startswith(PREFIX)


def model_alias(model: str) -> str:
    alias = model.removeprefix(PREFIX).strip()
    if not alias:
        raise ValueError(f"no model after {PREFIX!r} in {model!r}")
    return alias


def claude_binary() -> str:
    """``DAILY_AGENT_CLAUDE_BIN``, else ``claude`` on PATH, else ~/.local/bin."""
    configured = os.environ.get("DAILY_AGENT_CLAUDE_BIN", "").strip()
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback)


def claude_available() -> bool:
    """Whether the ``claude`` CLI is installed here (the feed server has none)."""
    path = claude_binary()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def inline_refs(schema: dict) -> dict:
    """A JSON schema with local ``$defs`` references expanded in place."""
    defs = schema.get("$defs", {})

    def expand(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return expand(defs[ref.split("/")[-1]])
            return {k: expand(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [expand(v) for v in node]
        return node

    return expand(schema)


def build_args(
    binary: str,
    *,
    model: str,
    system_prompt: str,
    json_schema: dict | None,
    stream: bool = False,
) -> list[str]:
    """``stream``: read one stream-json message (with images) from stdin."""
    io = (
        ["--input-format", "stream-json", "--output-format", "stream-json"]
        + ["--verbose"]  # required by stream-json output with -p
        if stream
        else ["--output-format", "json"]
    )
    args = [
        binary,
        "-p",
        "--model",
        model_alias(model),
        *io,
        "--tools",
        "",
        "--strict-mcp-config",
        "--settings",
        json.dumps({"disableAllHooks": True}),
        "--no-session-persistence",
        "--system-prompt",
        system_prompt,
    ]
    if json_schema is not None:
        args += ["--json-schema", json.dumps(inline_refs(json_schema))]
    return args


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _DROPPED_VARS}


def stream_message(prompt: str, images: Sequence[ImageInput]) -> str:
    """One stream-json user message: the images, then the prompt."""
    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.b64encode(image.data).decode(),
            },
        }
        for image in images
    ]
    content.append({"type": "text", "text": prompt})
    message = {"type": "user", "message": {"role": "user", "content": content}}
    return json.dumps(message) + "\n"


def _result_event(stdout: bytes) -> dict:
    """The ``result`` event: the whole reply (json) or one of its lines (stream)."""
    lines = stdout.decode(errors="replace").strip().splitlines()
    last: Any = None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if last is None:
            last = event
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    if isinstance(last, dict):
        return last
    raise ClaudeCodeError("claude returned no JSON result")


def parse_output(stdout: bytes, *, want_structured: bool) -> ClaudeResult:
    data = _result_event(stdout)
    message = str(data.get("result") or "").strip()
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        status = data.get("api_error_status")
        detail = message.splitlines()[0][:200] if message else "unknown error"
        raise ClaudeCodeError(f"claude error{f' {status}' if status else ''}: {detail}")
    structured = data.get("structured_output")
    if want_structured and structured is None:
        raise ClaudeCodeError("claude returned no structured output")
    try:
        cost = float(data.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return ClaudeResult(text=message, structured=structured, cost_usd=cost)


async def run_claude(
    prompt: str,
    *,
    model: str,
    system_prompt: str,
    json_schema: dict | None = None,
    images: Sequence[ImageInput] = (),
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    binary: str | None = None,
) -> ClaudeResult:
    """One locked-down ``claude -p`` call; the prompt goes in on stdin.

    ``images`` are shown to the model before the prompt.
    """
    for image in images:
        if image.media_type not in IMAGE_TYPES:
            raise ValueError(f"unsupported image type {image.media_type!r}")
    args = build_args(
        binary or claude_binary(),
        model=model,
        system_prompt=system_prompt,
        json_schema=json_schema,
        stream=bool(images),
    )
    stdin = stream_message(prompt, images) if images else prompt
    with tempfile.TemporaryDirectory(prefix="daily-agent-claude-") as workdir:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=child_env(),
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(f"claude CLI not found at {args[0]}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin.encode()), timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ClaudeCodeError(f"claude timed out after {timeout:.0f}s") from exc
    if not stdout.strip():
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise ClaudeCodeError(
            f"claude exited {proc.returncode}: {(detail or ['no output'])[-1][:200]}"
        )
    result = parse_output(stdout, want_structured=json_schema is not None)
    SPEND.calls += 1
    SPEND.cost_usd += result.cost_usd
    return result
