#!/usr/bin/env python3
"""xsess -- one index, four agents.

A bridge across Claude Code sessions (~/.claude/projects/**/*.jsonl), Codex
threads (CODEX_HOME/{sessions,archived_sessions}/**/rollout-*.jsonl), Cursor
agent transcripts (~/.cursor/projects/*/agent-transcripts/**/*.jsonl), and
Kimi Code wires ($KIMI_CODE_HOME/sessions/**/agents/*/wire.jsonl).  All four
stores are parsed into a single SQLite database with an FTS5 index so any
agent can list, search, and read the others' history by a short stable ref
("cc:<id>" Claude, "cx:<id>" Codex, "cr:<id>" Cursor, "km:<id>" Kimi) or by title.

Stdlib only.  Rollout/transcript files are append-only, so syncing is cheap:
each file is remembered by (size, mtime, bytes_indexed) and only the new tail
is parsed.

Usage: see `xsess --help`.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

HOME = Path(os.path.expanduser("~"))
# resolve(): /root/.codex and /root/.claude are symlinks into /workspace, and
# different agent processes carry different spellings of CODEX_HOME /
# CLAUDE_CONFIG_DIR.  Keying files by raw env paths made the same transcript
# count as two (indexed twice, then purged as "disappeared", flip-flopping on
# every differently-env'd xsess run) — canonical roots stop that for good.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                   or (HOME / ".claude")).resolve()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex")).resolve()
CURSOR_HOME = Path(os.environ.get("CURSOR_HOME") or (HOME / ".cursor")).resolve()
KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME")
                 or (HOME / ".kimi-code")).resolve()
DEFAULT_DB = Path(
    os.environ.get("XSESS_DB")
    or (HOME / ".local" / "state" / "session-bridge" / "index.db")
)
# bundles imported from other machines mirror each store's layout under here
# (follows $XSESS_DB like the index itself, so both stay on local disk)
REMOTE_ROOT = DEFAULT_DB.parent / "remote"

AGENT_HOME = {"claude": CLAUDE_HOME, "codex": CODEX_HOME,
              "cursor": CURSOR_HOME, "kimi": KIMI_HOME}
# per-store layouts relative to each agent's home; shared by discovery and by
# bundle/import so a mirrored tree parses exactly like a real one
STORE_GLOBS = {
    "claude": ("projects/*/*.jsonl", "projects/*/*/subagents/*.jsonl"),
    "codex": ("sessions/**/rollout-*.jsonl", "archived_sessions/*.jsonl"),
    "cursor": ("projects/*/agent-transcripts/*/*.jsonl",
               "projects/*/agent-transcripts/*/subagents/*.jsonl"),
    "kimi": ("sessions/*/*/agents/*/wire.jsonl",),
}
SIDECAR_GLOBS = {"claude": ("projects/*/*/subagents/*.meta.json",)}

AGENTS = {
    "cc": "claude", "cx": "codex", "cr": "cursor", "cu": "cursor",
    "km": "kimi", "ki": "kimi",
}
AGENT_PREFIX = {"claude": "cc", "codex": "cx", "cursor": "cr", "kimi": "km"}
ALL_AGENTS = ("claude", "codex", "cursor", "kimi")
_REF_PREFIX_RE = re.compile(
    r"^(cc|cx|cr|cu|km|ki|claude|codex|cursor|kimi)[:/](.*)$", re.I)

# roles worth reading by default (tool traffic and injected context are noise)
CONTENT_ROLES = ("user", "assistant", "summary", "agent_msg")
DEFAULT_SEARCH_ROLES = CONTENT_ROLES  # reasoning/tool need an explicit --role
DEFAULT_SHOW_ROLES = CONTENT_ROLES + ("tool",)
ALL_ROLES = CONTENT_ROLES + ("reasoning", "tool", "tool_out", "meta")

# text blocks the harnesses inject into the transcript as if they were user turns
INJECTED_TAGS = {
    "environment_context", "app-context", "app_context", "recommended_plugins",
    "user_instructions", "permissions", "model_switch", "memories", "skills",
    "ide_context", "plan_mode", "system-reminder", "system_reminder",
    "project_doc", "agents", "personality", "tools", "thread_settings",
    "collaboration_mode", "sandbox", "approval", "local-command-stdout",
    "command-stdout", "cwd", "todo", "budget", "available_skills",
    "local-command-caveat", "subagent_notification", "turn_aborted", "skill",
    "send_user_message_question_reply", "user-prompt-submit-hook",
}

# harness-injected blocks that are not wrapped in a tag
INJECTED_PREFIXES = (
    "# Context from my IDE setup",
    "Caveat: The messages below were generated",
)

CJK_RE = re.compile(
    r"[㐀-䶿一-鿿豈-﫿぀-ヿㇰ-ㇿ가-힯]"
)
TAG_RE = re.compile(r"^<([A-Za-z_][\w.:-]*)")
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CURSOR_TS_TAG = re.compile(r"<timestamp>\s*(.*?)\s*</timestamp>", re.I | re.S)
CURSOR_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.I | re.S)
CURSOR_CMD = re.compile(r"<cursor_commands>.*?</cursor_commands>", re.I | re.S)
CURSOR_CLOCK = re.compile(
    r"([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?"
    r"(?:\s*\(\s*UTC\s*([+-]\d{1,2})\s*\))?",
)
_MONTHS = {
    name.lower(): i
    for i, name in enumerate(
        "January February March April May June July August "
        "September October November December".split(), 1)
}
_MONTHS.update({name[:3]: i for name, i in list(_MONTHS.items())})

MAX_TOOL_CHARS = 600      # tool call / output text kept in the index
MAX_MSG_CHARS = 200_000   # guard against pathological single messages


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def seg(text: str) -> str:
    """Split CJK runs into per-character tokens so FTS5/unicode61 can index them."""
    return CJK_RE.sub(lambda m: " " + m.group(0) + " ", text)


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


_FRAC_RE = re.compile(r"\.(\d{1,9})")


def iso_ms(value) -> int | None:
    """ISO-8601 (possibly nanosecond precision, trailing Z) -> epoch ms."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v * 1000) if v < 1e11 else int(v)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    s = _FRAC_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_ts(ms: int | None, short: bool = False) -> str:
    if not ms:
        return "?"
    dt = datetime.fromtimestamp(ms / 1000).astimezone()
    return dt.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M")


def parse_since(spec: str | None) -> int | None:
    """'7d' / '36h' / '90m' / '2026-09-01' -> epoch ms."""
    if not spec:
        return None
    spec = spec.strip()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", spec, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit] * n
        return int((time.time() - secs) * 1000)
    ms = iso_ms(spec if "T" in spec else spec + "T00:00:00")
    if ms is None:
        raise SystemExit(f"xsess: cannot parse --since {spec!r}")
    return ms


def squash(text: str, limit: int | None = None) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + f" …(+{len(text) - limit}c)"
    return text


def oneline(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def flatten_content(content) -> str:
    """Codex/Claude content blocks (str | list | dict) -> plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "input_text", "output_text", "message", "content"):
            if key in content:
                return flatten_content(content[key])
        return ""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type") or ""
            if btype == "encrypted_content" or "encrypted_content" in block and btype == "":
                continue
            piece = flatten_content(block)
            if piece:
                parts.append(piece)
        return "\n".join(p for p in parts if p)
    return str(content)


def parse_cursor_clock(text: str) -> int | None:
    """Parse Cursor's '<timestamp>Friday, Sep 11, 2026, 1:06 PM (UTC+8)</timestamp>'."""
    if not text:
        return None
    m = CURSOR_CLOCK.search(text)
    if not m:
        return iso_ms(text)
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return iso_ms(text)
    hour = int(m.group(4))
    ampm = (m.group(6) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    off = int(m.group(7) or 8)
    dt = datetime(int(m.group(3)), mon, int(m.group(2)), hour, int(m.group(5)),
                  tzinfo=timezone(timedelta(hours=off)))
    return int(dt.timestamp() * 1000)


def cursor_user_text(text: str) -> tuple[str, int | None]:
    """Prefer <user_query>, drop slash-command wrappers; return (body, ts_ms)."""
    raw = text or ""
    ts = None
    stamped = CURSOR_TS_TAG.search(raw)
    if stamped:
        ts = parse_cursor_clock(stamped.group(1))
    queried = CURSOR_QUERY.search(raw)
    if queried:
        return queried.group(1).strip(), ts
    body = CURSOR_CMD.sub("", raw)
    body = CURSOR_TS_TAG.sub("", body).strip()
    return body, ts


_CURSOR_SLUG_CACHE: dict[str, str] = {}


def decode_cursor_project(slug: str) -> str:
    """Best-effort reverse of Cursor's project-folder encoding (/a/b -> a-b)."""
    if not slug:
        return ""
    cached = _CURSOR_SLUG_CACHE.get(slug)
    if cached is not None:
        return cached
    cur = Path("/")
    rest = slug
    while rest:
        try:
            names = [p.name for p in cur.iterdir()] if cur.is_dir() else []
        except OSError:
            names = []
        match = ""
        for name in names:
            if rest == name or rest.startswith(name + "-"):
                if len(name) > len(match):
                    match = name
        if not match:
            result = str(cur / rest) if cur != Path("/") else "/" + slug
            _CURSOR_SLUG_CACHE[slug] = result
            return result
        cur = cur / match
        rest = rest[len(match):]
        if rest.startswith("-"):
            rest = rest[1:]
        elif rest:
            result = str(cur / rest)
            _CURSOR_SLUG_CACHE[slug] = result
            return result
    result = str(cur)
    _CURSOR_SLUG_CACHE[slug] = result
    return result


def cursor_cwd_from_path(path: Path) -> str:
    parts = path.parts
    try:
        i = parts.index("projects")
        slug = parts[i + 1]
    except (ValueError, IndexError):
        return ""
    return decode_cursor_project(slug)


def is_injected(text: str) -> bool:
    t = (text or "").lstrip()
    if t.startswith(INJECTED_PREFIXES):
        return True
    if not t.startswith("<"):
        return False
    m = TAG_RE.match(t)
    if not m:
        return False
    tag = m.group(1).lower()
    return tag in INJECTED_TAGS or tag.endswith("_context") or tag.endswith("-context")


# ---------------------------------------------------------------------------
# tool-call summarisation (keeps the index small but greppable)
# ---------------------------------------------------------------------------

def fmt_tool(name: str, payload) -> str:
    """Render a tool invocation as one searchable line."""
    name = name or "tool"
    args = payload
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
            except Exception:
                args = payload
        else:
            args = payload
    if isinstance(args, dict):
        for key in ("cmd", "command", "script", "input"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return f"{name}: {squash(val, MAX_TOOL_CHARS)}"
        for key in ("file_path", "path", "url", "pattern", "query", "notebook_path"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                extra = args.get("prompt") or args.get("description") or ""
                tail = f" — {oneline(str(extra), 120)}" if extra else ""
                return f"{name}: {val}{tail}"
        for key in ("description", "prompt", "message", "task_name", "skill"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return f"{name}: {squash(val, MAX_TOOL_CHARS)}"
        try:
            return f"{name}: {squash(json.dumps(args, ensure_ascii=False), MAX_TOOL_CHARS)}"
        except Exception:
            pass
    return f"{name}: {squash(str(args or ''), MAX_TOOL_CHARS)}"


# ---------------------------------------------------------------------------
# parsers -- each returns (meta: dict, msgs: list[(ts, role, name, body)])
# ---------------------------------------------------------------------------

def read_tail(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Read whole lines from `offset`; ignore a trailing partial line."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        blob = fh.read()
    if not blob:
        return [], offset
    end = blob.rfind(b"\n")
    if end == -1:
        return [], offset
    lines = blob[: end + 1].split(b"\n")
    return [ln for ln in lines if ln.strip()], offset + end + 1


def parse_claude(path: Path, offset: int = 0):
    meta: dict = {}
    msgs: list[tuple] = []
    lines, end = read_tail(path, offset)
    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        typ = d.get("type")
        ts = iso_ms(d.get("timestamp"))
        if d.get("sessionId") and not meta.get("sid"):
            meta["sid"] = d["sessionId"]
        for src, dst in (("cwd", "cwd"), ("gitBranch", "branch"),
                         ("version", "cli_version"), ("sessionKind", "kind_hint")):
            if d.get(src):
                meta.setdefault(dst, d[src])
        if d.get("agentId"):
            meta.setdefault("agent_id", d["agentId"])

        if typ == "ai-title" and d.get("aiTitle"):
            meta["title"] = d["aiTitle"]
            meta["title_src"] = "ai-title"
            continue
        if typ == "agent-name" and d.get("agentName"):
            meta.setdefault("title", d["agentName"])
            meta.setdefault("title_src", "agent-name")
            continue
        if typ == "summary" and d.get("summary"):
            msgs.append((ts, "summary", None, squash(str(d["summary"]), MAX_MSG_CHARS)))
            continue
        if typ not in ("user", "assistant", "system"):
            continue

        if typ == "system":
            body = squash(str(d.get("content") or d.get("message") or ""), MAX_TOOL_CHARS)
            if body:
                msgs.append((ts, "meta", d.get("subtype"), body))
            continue

        message = d.get("message") or {}
        content = message.get("content")
        if message.get("model"):
            meta.setdefault("model", message["model"])
        human = (d.get("origin") or {}).get("kind") == "human"
        sidechain = bool(d.get("isSidechain"))

        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = squash(block.get("text") or "", MAX_MSG_CHARS)
                if not text:
                    continue
                if typ == "assistant":
                    role = "assistant"
                elif human or not is_injected(text):
                    role = "user"
                else:
                    role = "meta"
                if role == "user" and sidechain and not human:
                    role = "user"  # subagent prompt: still the driving instruction
                msgs.append((ts, role, None, text))
            elif btype == "thinking":
                text = squash(block.get("thinking") or "", MAX_MSG_CHARS)
                if text:
                    msgs.append((ts, "reasoning", None, text))
            elif btype == "tool_use":
                msgs.append((ts, "tool", block.get("name"),
                             fmt_tool(block.get("name"), block.get("input"))))
            elif btype == "tool_result":
                text = squash(flatten_content(block.get("content")), MAX_TOOL_CHARS)
                if text:
                    msgs.append((ts, "tool_out", None, text))
            elif btype in ("image", "document"):
                msgs.append((ts, "meta", btype, f"[{btype}]"))
    if "subagents" in path.parts:
        # subagent transcripts carry the *parent's* sessionId; key them by agent id
        agent_id = meta.get("agent_id") or re.sub(r"^agent-", "", path.stem)
        meta["parent"] = path.parent.parent.name
        meta["sid"] = agent_id
        side = path.with_suffix(".meta.json")
        if side.exists():
            try:
                info = json.loads(side.read_text())
            except Exception:
                info = {}
            desc = (info.get("description") or "").strip()
            atype = (info.get("agentType") or "").strip()
            if desc:
                meta["title"] = f"{desc} [{atype}]" if atype else desc
                meta["title_src"] = "agent-name"
    if not meta.get("sid"):
        meta["sid"] = path.stem
    return meta, msgs, end


def parse_codex(path: Path, offset: int = 0):
    meta: dict = {}
    msgs: list[tuple] = []
    lines, end = read_tail(path, offset)
    # Codex names rollouts `rollout-<ts>-<thread_id>[_<continuation>].jsonl`, and that
    # first uuid is what its own state DB keys threads (and titles) by.  The `id` inside
    # session_meta is the conversation root, shared by every resume/fork of it.
    in_name = re.findall(UUID_RE, path.name)
    file_sid = in_name[0] if in_name else None
    for raw in lines:
        # cheap prefilter: skip token accounting / rendered-item duplicates
        head = raw[:160]
        if b'"response_item"' not in head and b'"session_meta"' not in head \
           and b'"turn_context"' not in head and b'"compacted"' not in head:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        typ = d.get("type")
        ts = iso_ms(d.get("timestamp"))
        payload = d.get("payload") or {}

        if typ == "session_meta":
            meta["root"] = payload.get("session_id") or payload.get("id")
            meta["sid"] = file_sid or payload.get("id") or meta.get("sid")
            for src, dst in (("cwd", "cwd"), ("originator", "originator"),
                             ("cli_version", "cli_version"), ("thread_source", "kind_hint"),
                             ("agent_nickname", "nickname"), ("agent_path", "agent_path"),
                             ("model_provider", "provider")):
                if payload.get(src):
                    meta[dst] = payload[src]
            git = payload.get("git") or {}
            if isinstance(git, dict) and git.get("branch"):
                meta["branch"] = git["branch"]
            src = payload.get("source")
            if isinstance(src, dict):
                spawn = ((src.get("subagent") or {}).get("thread_spawn") or {})
                if spawn.get("parent_thread_id"):
                    meta["parent"] = spawn["parent_thread_id"]
            if payload.get("parent_thread_id"):
                meta.setdefault("parent", payload["parent_thread_id"])
            if not meta.get("started_ms"):
                meta["started_ms"] = iso_ms(payload.get("timestamp")) or ts
            continue

        if typ == "turn_context":
            for src, dst in (("model", "model"), ("cwd", "cwd"),
                             ("reasoning_effort", "effort")):
                if payload.get(src):
                    meta[dst] = payload[src]
            continue

        if typ == "compacted":
            body = squash(flatten_content(payload.get("message") or payload), MAX_MSG_CHARS)
            if body:
                msgs.append((ts, "summary", "compacted", body))
            continue

        if typ != "response_item":
            continue

        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            text = squash(flatten_content(payload.get("content")), MAX_MSG_CHARS)
            if not text:
                continue
            if role == "assistant":
                msgs.append((ts, "assistant", None, text))
            elif role == "user":
                msgs.append((ts, "meta" if is_injected(text) else "user", None, text))
            else:  # developer / system: harness instructions
                msgs.append((ts, "meta", role, oneline(text, MAX_TOOL_CHARS)))
        elif ptype == "reasoning":
            text = squash(flatten_content(payload.get("summary")), MAX_MSG_CHARS)
            if not text:
                text = squash(flatten_content(payload.get("content")), MAX_MSG_CHARS)
            if text:
                msgs.append((ts, "reasoning", None, text))
        elif ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            name = payload.get("name") or ptype
            args = payload.get("arguments")
            if args is None:
                args = payload.get("input")
            if args is None:
                args = payload.get("action")
            msgs.append((ts, "tool", name, fmt_tool(name, args)))
        elif ptype in ("function_call_output", "custom_tool_call_output",
                       "local_shell_call_output"):
            text = squash(flatten_content(payload.get("output")), MAX_TOOL_CHARS)
            if text:
                msgs.append((ts, "tool_out", None, text))
        elif ptype == "web_search_call":
            action = payload.get("action") or {}
            query = action.get("query") or ""
            msgs.append((ts, "tool", "web_search", f"web_search: {oneline(query, 300)}"))
        elif ptype == "agent_message":
            text = squash(flatten_content(payload.get("content")), MAX_MSG_CHARS)
            label = f"{payload.get('author') or '?'}→{payload.get('recipient') or '?'}"
            if text:
                msgs.append((ts, "agent_msg", label, text))
        elif ptype in ("image_generation_call", "mcp_tool_call"):
            msgs.append((ts, "tool", ptype, fmt_tool(ptype, payload)))
    if not meta.get("sid"):
        meta["sid"] = file_sid or path.stem
    return meta, msgs, end


def parse_cursor(path: Path, offset: int = 0):
    """Cursor agent-transcripts/<sid>/<sid>.jsonl (and .../subagents/<id>.jsonl)."""
    meta: dict = {}
    msgs: list[tuple] = []
    lines, end = read_tail(path, offset)
    last_ts = None
    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        typ = d.get("type")
        if typ == "turn_ended":
            continue
        role = d.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        message = d.get("message") or {}
        if message.get("model"):
            meta.setdefault("model", message["model"])
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                raw_text = block.get("text") or ""
                if role == "user":
                    text, ts = cursor_user_text(raw_text)
                    if ts:
                        last_ts = ts
                    if not text:
                        continue
                    item_role = "meta" if is_injected(text) else "user"
                    msgs.append((last_ts, item_role, None, squash(text, MAX_MSG_CHARS)))
                else:
                    text = squash(raw_text, MAX_MSG_CHARS)
                    if text:
                        msgs.append((last_ts, "assistant" if role == "assistant" else "meta",
                                     None, text))
            elif btype == "thinking":
                text = squash(block.get("thinking") or "", MAX_MSG_CHARS)
                if text:
                    msgs.append((last_ts, "reasoning", None, text))
            elif btype == "tool_use":
                msgs.append((last_ts, "tool", block.get("name"),
                             fmt_tool(block.get("name"), block.get("input"))))
            elif btype == "tool_result":
                text = squash(flatten_content(block.get("content")), MAX_TOOL_CHARS)
                if text:
                    msgs.append((last_ts, "tool_out", None, text))
            elif btype in ("image", "document"):
                msgs.append((last_ts, "meta", btype, f"[{btype}]"))
    if "subagents" in path.parts:
        meta["parent"] = path.parent.parent.name
        meta["sid"] = path.stem
    else:
        meta["sid"] = path.parent.name if path.parent.name != "agent-transcripts" else path.stem
    cwd = cursor_cwd_from_path(path)
    if cwd:
        meta["cwd"] = cwd
    if not meta.get("started_ms"):
        first_ts = next((ts for ts, *_ in msgs if ts), None)
        if first_ts:
            meta["started_ms"] = first_ts
        else:
            try:
                meta["started_ms"] = int(path.stat().st_mtime * 1000)
            except OSError:
                pass
    return meta, msgs, end


def _kimi_session_dir(path: Path) -> Path:
    # .../session_<id>/agents/<agentId>/wire.jsonl
    return path.parent.parent.parent


def _kimi_sid(session_dir: Path) -> str:
    name = session_dir.name
    if name.startswith("session_"):
        return name[len("session_"):]
    return name


def _kimi_state(session_dir: Path) -> dict:
    side = session_dir / "state.json"
    if not side.exists():
        return {}
    try:
        return json.loads(side.read_text())
    except Exception:
        return {}


def parse_kimi(path: Path, offset: int = 0):
    """Kimi Code agents/<id>/wire.jsonl event stream."""
    meta: dict = {}
    msgs: list[tuple] = []
    lines, end = read_tail(path, offset)
    session_dir = _kimi_session_dir(path)
    agent_id = path.parent.name
    sid = _kimi_sid(session_dir)
    if agent_id != "main":
        meta["parent"] = sid
        meta["sid"] = agent_id
    else:
        meta["sid"] = sid
    state = _kimi_state(session_dir)
    if state.get("cwd"):
        meta["cwd"] = state["cwd"]
    title = (state.get("title") or "").strip()
    if title and title.lower() not in ("new session", "untitled"):
        meta["title"] = title
        meta["title_src"] = "kimi_state"
    if state.get("createdAt"):
        meta["started_ms"] = iso_ms(state["createdAt"])
    if state.get("archived"):
        meta["kind_hint"] = "archived"
    for raw in lines:
        head = raw[:140]
        if b"turn.prompt" not in head and b"append_loop_event" not in head \
           and b"profile.bind" not in head and b"llm.request" not in head:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        typ = d.get("type")
        ts = iso_ms(d.get("time")) or iso_ms(d.get("created_at"))
        if typ == "profile.bind":
            if d.get("modelAlias"):
                meta.setdefault("model", d["modelAlias"])
            env = d.get("environmentDisclosure") or {}
            if isinstance(env, dict) and env.get("cwd"):
                meta.setdefault("cwd", env["cwd"])
            continue
        if typ == "llm.request":
            if d.get("modelAlias") or d.get("model"):
                meta["model"] = d.get("modelAlias") or d.get("model")
            continue
        if typ == "turn.prompt":
            origin = d.get("origin") or {}
            text = squash(flatten_content(d.get("input")), MAX_MSG_CHARS)
            if not text:
                continue
            kind = origin.get("kind")
            if kind == "injection" or is_injected(text):
                msgs.append((ts, "meta", kind, text))
            else:
                msgs.append((ts, "user", None, text))
            continue
        if typ != "context.append_loop_event":
            continue
        ev = d.get("event") or {}
        et = ev.get("type")
        if et == "content.part":
            part = ev.get("part") or {}
            ptype = part.get("type")
            if ptype == "think":
                text = squash(part.get("think") or "", MAX_MSG_CHARS)
                if text:
                    msgs.append((ts, "reasoning", None, text))
            elif ptype == "text":
                text = squash(part.get("text") or "", MAX_MSG_CHARS)
                if text:
                    msgs.append((ts, "assistant", None, text))
        elif et == "tool.call":
            name = ev.get("name") or "tool"
            args = ev.get("args")
            if args is None:
                args = ev.get("input")
            msgs.append((ts, "tool", name, fmt_tool(name, args)))
        elif et == "tool.result":
            text = squash(flatten_content(ev.get("output") or ev.get("result")
                                          or ev.get("content")), MAX_TOOL_CHARS)
            if text:
                msgs.append((ts, "tool_out", ev.get("name"), text))
    if not meta.get("sid"):
        meta["sid"] = path.parent.name
    return meta, msgs, end


def parse_file(job):
    """multiprocessing entry point."""
    agent, path_str, offset = job
    path = Path(path_str)
    try:
        if agent == "claude":
            meta, msgs, end = parse_claude(path, offset)
        elif agent == "cursor":
            meta, msgs, end = parse_cursor(path, offset)
        elif agent == "kimi":
            meta, msgs, end = parse_kimi(path, offset)
        else:
            meta, msgs, end = parse_codex(path, offset)
    except FileNotFoundError:
        return None
    except Exception as exc:  # keep the sweep going, report at the end
        return {"agent": agent, "path": path_str, "error": f"{type(exc).__name__}: {exc}"}
    return {"agent": agent, "path": path_str, "meta": meta, "msgs": msgs, "end": end}


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,
  ref TEXT,
  agent TEXT,
  size INTEGER,
  mtime REAL,
  bytes_indexed INTEGER DEFAULT 0,
  max_seq INTEGER DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  ref TEXT PRIMARY KEY,
  agent TEXT, sid TEXT,
  title TEXT DEFAULT '', title_src TEXT DEFAULT '',
  cwd TEXT DEFAULT '', kind TEXT DEFAULT 'main', parent TEXT, root TEXT,
  model TEXT DEFAULT '', branch TEXT DEFAULT '', nickname TEXT DEFAULT '',
  originator TEXT DEFAULT '', archived INTEGER DEFAULT 0,
  host TEXT DEFAULT '',
  started_ms INTEGER, ended_ms INTEGER,
  n_msg INTEGER DEFAULT 0, n_user INTEGER DEFAULT 0,
  first_user TEXT DEFAULT '', path TEXT
);
CREATE INDEX IF NOT EXISTS sessions_time ON sessions(ended_ms DESC);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  ref TEXT, seq INTEGER, ts INTEGER, role TEXT, name TEXT, body TEXT, src TEXT
);
CREATE INDEX IF NOT EXISTS messages_ref ON messages(ref, seq);
CREATE INDEX IF NOT EXISTS messages_src ON messages(src);
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
  body, content='', contentless_delete=1,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""

TITLE_PRIORITY = {"": 0, "first_user": 1, "agent-name": 2, "session_index": 3,
                  "ai-title": 4, "kimi_state": 4, "codex_state": 5, "codex_name": 6}
SCHEMA_VERSION = "3"


def connect(db_path: Path = DEFAULT_DB, create: bool = True) -> sqlite3.Connection:
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    elif not db_path.exists():
        raise SystemExit(f"xsess: no index at {db_path} — run `xsess index` first")
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
    if not create:
        return con
    have = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                       " AND name IN ('messages','sessions')").fetchone()[0]
    version = None
    if have:
        try:
            row = con.execute("SELECT v FROM state WHERE k='schema'").fetchone()
            version = row["v"] if row else None
        except sqlite3.OperationalError:
            version = None
    if have and version != SCHEMA_VERSION:
        print(f"xsess: index schema is stale (v{version} → v{SCHEMA_VERSION}); rebuilding",
              file=sys.stderr)
        for table in ("msg_fts", "messages", "sessions", "files", "state"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.commit()
    con.executescript(SCHEMA)
    con.execute("INSERT INTO state(k, v) VALUES('schema', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (SCHEMA_VERSION,))
    con.commit()
    return con


# ---------------------------------------------------------------------------
# discovery + sync
# ---------------------------------------------------------------------------

def _scan_store(base: Path, agent: str, found: list) -> None:
    if not base.is_dir():
        return
    for pattern in STORE_GLOBS[agent]:
        for path in base.glob(pattern):
            if agent == "cursor" and "subagents" not in pattern \
               and "subagents" in path.parts:
                continue  # the flat cursor pattern must not swallow subagents
            found.append((agent, path))


def discover() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for agent, home in AGENT_HOME.items():
        _scan_store(home, agent, found)
    if REMOTE_ROOT.is_dir():
        # bundles imported from other machines mirror each store's layout
        for host_dir in sorted(REMOTE_ROOT.iterdir()):
            if not host_dir.is_dir():
                continue
            for agent in AGENT_HOME:
                _scan_store(host_dir / agent, agent, found)
    return found


def codex_titles() -> dict[str, dict]:
    """Thread titles/metadata from Codex's own SQLite state + session_index.jsonl."""
    out: dict[str, dict] = {}
    index_file = CODEX_HOME / "session_index.jsonl"
    if index_file.exists():
        for raw in index_file.read_text(errors="replace").splitlines():
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("id") and d.get("thread_name"):
                out[d["id"]] = {"title": d["thread_name"], "title_src": "session_index"}
    for db in codex_state_dbs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, title, name, cwd, archived, first_user_message, preview, "
                "       model, git_branch, thread_source, agent_nickname "
                "FROM threads"
            ).fetchall()
            con.close()
        except Exception:
            continue
        for row in rows:
            entry = out.setdefault(row["id"], {})
            name = (row["name"] or "").strip()
            title = (row["title"] or "").strip()
            if name:
                entry.update(title=name, title_src="codex_name")
            elif title and TITLE_PRIORITY.get(entry.get("title_src", ""), 0) < TITLE_PRIORITY["codex_state"]:
                entry.update(title=title, title_src="codex_state")
            for key, col in (("cwd", "cwd"), ("model", "model"),
                             ("branch", "git_branch"), ("kind_hint", "thread_source"),
                             ("nickname", "agent_nickname")):
                if row[col]:
                    entry.setdefault(key, row[col])
            entry["archived"] = int(row["archived"] or 0)
            if row["first_user_message"]:
                entry.setdefault("first_user", row["first_user_message"])
    return out


def codex_state_dbs() -> list[Path]:
    cands: list[Path] = []
    cfg = CODEX_HOME / "config.toml"
    if cfg.exists():
        m = re.search(r'^\s*sqlite_home\s*=\s*"([^"]+)"', cfg.read_text(errors="replace"),
                      re.M)
        if m:
            cands.append(Path(os.path.expanduser(m.group(1))))
    cands += [HOME / ".local" / "state" / "codex-sqlite", CODEX_HOME]
    dbs: list[Path] = []
    for base in cands:
        if base.is_dir():
            dbs.extend(sorted(base.glob("state_*.sqlite")))
    return dbs


def kind_of(agent: str, path: Path, meta: dict) -> str:
    if "subagents" in path.parts or meta.get("parent"):
        return "subagent"
    if agent == "claude" and meta.get("kind_hint") == "bg":
        return "bg"
    hint = meta.get("kind_hint")
    if hint == "subagent":
        return "subagent"
    return "main"


def sync(con: sqlite3.Connection, full: bool = False, jobs: int | None = None,
         quiet: bool = False, verbose: bool = False) -> dict:
    t0 = time.time()
    known = {r["path"]: r for r in con.execute("SELECT * FROM files")}
    todo: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for agent, path in discover():
        key = str(path)
        seen.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        prev = known.get(key)
        if full or prev is None:
            todo.append((agent, key, 0))
        elif st.st_size > (prev["bytes_indexed"] or 0):
            todo.append((agent, key, prev["bytes_indexed"] or 0))
        elif st.st_size < (prev["bytes_indexed"] or 0):
            # rewritten/truncated: forget the old rows, or the re-parse would
            # append every message again (imports make this path reachable)
            _purge_file(con, key)
            con.execute("DELETE FROM files WHERE path=?", (key,))
            del known[key]
            todo.append((agent, key, 0))
    if full:
        con.execute("DELETE FROM msg_fts")
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM files")
        con.execute("DELETE FROM sessions")
        con.commit()
        known = {}

    stats = {"files": len(seen), "parsed": 0, "msgs": 0, "errors": []}
    if todo:
        if not quiet:
            total_bytes = sum(Path(p).stat().st_size for _, p, o in todo) / 1e6
            print(f"xsess: indexing {len(todo)} file(s), ~{total_bytes:.0f} MB…",
                  file=sys.stderr)
        results = _run_parsers(todo, jobs)
        # store in path order: Codex encodes the start time in the filename, so a
        # resumed thread's files land in the transcript in the order they happened
        results = sorted((r for r in results if r), key=lambda r: r["path"])
        for res in results:
            if res.get("error"):
                stats["errors"].append((res["path"], res["error"]))
                con.execute(
                    "INSERT INTO files(path, agent, error) VALUES(?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET error=excluded.error",
                    (res["path"], res["agent"], res["error"]))
                continue
            n = _store(con, res, known)
            stats["parsed"] += 1
            stats["msgs"] += n
            if verbose:
                print(f"  + {res['path']} (+{n})", file=sys.stderr)
        con.commit()

    _apply_codex_titles(con)
    _apply_remote_meta(con)
    # forget files that disappeared (archived elsewhere, deleted, …)
    for path in [p for p in known if p not in seen]:
        ref = known[path]["ref"]
        _purge_file(con, path)
        con.execute("DELETE FROM files WHERE path=?", (path,))
        if ref and not con.execute("SELECT 1 FROM messages WHERE ref=? LIMIT 1",
                                   (ref,)).fetchone():
            con.execute("DELETE FROM sessions WHERE ref=?", (ref,))
    con.execute("INSERT INTO state(k, v) VALUES('last_sync', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(int(time.time())),))
    con.commit()
    stats["seconds"] = round(time.time() - t0, 2)
    return stats


def _run_parsers(todo, jobs):
    if len(todo) == 1:
        return [parse_file(todo[0])]
    workers = jobs or min(16, (os.cpu_count() or 4))
    if workers <= 1:
        return [parse_file(job) for job in todo]
    import multiprocessing as mp
    todo = sorted(todo, key=lambda j: -Path(j[1]).stat().st_size)
    with mp.Pool(workers) as pool:
        return list(pool.imap_unordered(parse_file, todo, chunksize=1))


def _drop_ids(con, ids):
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        marks = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM msg_fts WHERE rowid IN ({marks})", chunk)
        con.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)


def _purge_file(con, path):
    """Forget one transcript file (a session may span several)."""
    ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE src=?", (path,))]
    _drop_ids(con, ids)


def _purge(con, ref):
    ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE ref=?", (ref,))]
    _drop_ids(con, ids)
    con.execute("DELETE FROM sessions WHERE ref=?", (ref,))


def _refresh_session(con, ref, agent, sid, meta, path, kind, fresh_title, fresh_src):
    """Recompute derived session fields from whatever is currently indexed."""
    agg = con.execute(
        "SELECT COUNT(*) n, SUM(role='user') nu, MIN(ts) a, MAX(ts) b"
        " FROM messages WHERE ref=?", (ref,)).fetchone()
    first_user = con.execute(
        "SELECT body FROM messages WHERE ref=? AND role='user' ORDER BY seq LIMIT 1",
        (ref,)).fetchone()
    first_user = first_user[0] if first_user else ""
    title, title_src = fresh_title, fresh_src
    if not title and first_user:
        title, title_src = oneline(first_user, 70), "first_user"
    row = con.execute("SELECT * FROM sessions WHERE ref=?", (ref,)).fetchone()
    started = meta.get("started_ms") or agg["a"]
    ended = agg["b"] or (row["ended_ms"] if row else None)
    if row:
        if TITLE_PRIORITY.get(title_src, 0) <= TITLE_PRIORITY.get(row["title_src"] or "", 0):
            title, title_src = row["title"], row["title_src"]
        con.execute(
            "UPDATE sessions SET title=?, title_src=?, cwd=?, kind=?, parent=?, root=?,"
            " model=?, branch=?, nickname=?, originator=?, started_ms=?, ended_ms=?,"
            " n_msg=?, n_user=?, first_user=?, path=? WHERE ref=?",
            (title, title_src, meta.get("cwd") or row["cwd"], kind,
             meta.get("parent") or row["parent"], meta.get("root") or row["root"],
             meta.get("model") or row["model"], meta.get("branch") or row["branch"],
             meta.get("nickname") or row["nickname"],
             meta.get("originator") or row["originator"],
             min(x for x in (started, row["started_ms"]) if x) if (started or row["started_ms"]) else None,
             ended, agg["n"] or 0, agg["nu"] or 0, oneline(first_user, 400), path, ref))
    else:
        con.execute(
            "INSERT INTO sessions(ref, agent, sid, title, title_src, cwd, kind, parent,"
            " root, model, branch, nickname, originator, archived, started_ms, ended_ms,"
            " n_msg, n_user, first_user, path)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, agent, sid, title, title_src, meta.get("cwd", ""), kind,
             meta.get("parent"), meta.get("root"), meta.get("model", ""),
             meta.get("branch", ""), meta.get("nickname", ""),
             meta.get("originator", ""), 1 if "archived_sessions" in path else 0,
             started, ended, agg["n"] or 0, agg["nu"] or 0,
             oneline(first_user, 400), path))


def _store(con, res, known) -> int:
    agent, path, meta, msgs, end = (res["agent"], res["path"], res["meta"],
                                   res["msgs"], res["end"])
    prev = known.get(path)
    sid = meta.get("sid") or Path(path).stem
    ref = f"{AGENT_PREFIX[agent]}:{sid}"
    fresh = prev is None or (prev["bytes_indexed"] or 0) == 0
    if fresh and prev is not None:
        _purge_file(con, path)
    # seq is unique per session, not per file: resumed Codex threads span several files
    base_seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE ref=?",
                           (ref,)).fetchone()[0]

    if msgs:
        base_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        rows = [(base_id + i, ref, base_seq + i, ts, role, name, body, path)
                for i, (ts, role, name, body) in enumerate(msgs, start=1)]
        con.executemany(
            "INSERT INTO messages(id, ref, seq, ts, role, name, body, src)"
            " VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO msg_fts(rowid, body) VALUES(?,?)",
                        [(r[0], seg(r[6])) for r in rows])

    st = Path(path).stat()
    kind = kind_of(agent, Path(path), meta)
    _refresh_session(con, ref, agent, sid, meta, path, kind,
                     meta.get("title", ""), meta.get("title_src", ""))
    con.execute(
        "INSERT INTO files(path, ref, agent, size, mtime, bytes_indexed, max_seq, error)"
        " VALUES(?,?,?,?,?,?,?,NULL)"
        " ON CONFLICT(path) DO UPDATE SET ref=excluded.ref, size=excluded.size,"
        " mtime=excluded.mtime, bytes_indexed=excluded.bytes_indexed,"
        " max_seq=excluded.max_seq, error=NULL",
        (path, ref, agent, st.st_size, st.st_mtime, end, base_seq + len(msgs)))
    return len(msgs)


def _apply_codex_titles(con):
    titles = codex_titles()
    if not titles:
        return
    for sid, info in titles.items():
        ref = f"cx:{sid}"
        row = con.execute("SELECT title_src, title, cwd, model FROM sessions WHERE ref=?",
                          (ref,)).fetchone()
        if row is None:
            continue
        new_src = info.get("title_src", "")
        if info.get("title") and \
           TITLE_PRIORITY.get(new_src, 0) > TITLE_PRIORITY.get(row["title_src"] or "", 0):
            con.execute("UPDATE sessions SET title=?, title_src=? WHERE ref=?",
                        (oneline(info["title"], 120), new_src, ref))
        con.execute(
            "UPDATE sessions SET archived=COALESCE(?, archived),"
            " cwd=CASE WHEN cwd='' THEN COALESCE(?, '') ELSE cwd END,"
            " model=CASE WHEN model='' THEN COALESCE(?, '') ELSE model END,"
            " nickname=CASE WHEN nickname='' THEN COALESCE(?, '') ELSE nickname END"
            " WHERE ref=?",
            (info.get("archived"), info.get("cwd"), info.get("model"),
             info.get("nickname"), ref))
    con.commit()


def _apply_remote_meta(con):
    """Titles/metadata + host tag for sessions imported from other machines.

    Each mirror dir carries the manifest of its latest import; re-applying it
    on every sync (cheap) keeps imported sessions titled after a full rebuild.
    """
    for mf in sorted(REMOTE_ROOT.glob("*/manifest.json")):
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        host = data.get("host") or mf.parent.name
        for ref, info in data.get("sessions", {}).items():
            row = con.execute(
                "SELECT title_src, title FROM sessions WHERE ref=?", (ref,)).fetchone()
            if row is None:
                continue
            new_src = info.get("title_src") or ""
            if info.get("title") and \
               TITLE_PRIORITY.get(new_src, 0) > TITLE_PRIORITY.get(row["title_src"] or "", 0):
                con.execute("UPDATE sessions SET title=?, title_src=? WHERE ref=?",
                            (oneline(info["title"], 120), new_src, ref))
            con.execute(
                "UPDATE sessions SET host=?,"
                " cwd=CASE WHEN cwd='' THEN COALESCE(?, '') ELSE cwd END,"
                " model=CASE WHEN model='' THEN COALESCE(?, '') ELSE model END,"
                " nickname=CASE WHEN nickname='' THEN COALESCE(?, '') ELSE nickname END"
                " WHERE ref=?",
                (host, info.get("cwd"), info.get("model"),
                 info.get("nickname"), ref))
    con.commit()


def open_index(args) -> sqlite3.Connection:
    """Open the index for reading, building or repairing it if necessary."""
    first_run = not Path(args.db).exists()
    if first_run:
        print(f"xsess: building the index at {args.db} (one-off)…", file=sys.stderr)
    con = connect(args.db)
    if first_run or not con.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
        sync(con, full=False, quiet=False)
    else:
        autosync(con, not args.no_sync)
    return con


def autosync(con, enabled: bool = True, quiet: bool = True):
    """Cheap freshness check: only newly appended bytes are parsed."""
    if not enabled:
        return
    try:
        sync(con, full=False, quiet=quiet)
    except Exception as exc:
        print(f"xsess: autosync skipped ({type(exc).__name__}: {exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# query helpers
# ---------------------------------------------------------------------------

def fts_query(query: str) -> str:
    tokens = re.findall(r'"[^"]*"|\S+', query)
    out = []
    for tok in tokens:
        if tok in ("AND", "OR", "NOT"):
            out.append(tok)
            continue
        neg = tok.startswith("-") and len(tok) > 1
        if neg:
            tok = tok[1:]
        prefix = tok.endswith("*")
        if prefix:
            tok = tok[:-1]
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            tok = tok[1:-1]
        tok = tok.replace('"', " ").strip()
        if not tok:
            continue
        term = '"' + seg(tok).strip() + '"' + ("*" if prefix else "")
        out.append(("NOT " if neg else "") + term)
    return " ".join(out) if out else '""'


def resolve(con, ref_or_title: str, allow_many: bool = False) -> list[sqlite3.Row]:
    """Accept cc:/cx:/cr:/km: refs, bare ids, unique prefixes, or a title substring."""
    raw = (ref_or_title or "").strip()
    if not raw:
        raise SystemExit("xsess: empty ref")
    agent = None
    body = raw
    m = _REF_PREFIX_RE.match(raw)
    if m:
        key = m.group(1).lower()
        agent = AGENTS.get(key, key)
        body = m.group(2)
    where, args = [], []
    if agent:
        where.append("agent=?")
        args.append(agent)
    exact = list(con.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(where + ['sid=?'])}",
        args + [body]))
    if exact:
        return exact
    pref = list(con.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(where + ['sid LIKE ?'])}"
        " ORDER BY ended_ms DESC", args + [body + "%"]))
    if len(pref) == 1 or (pref and allow_many):
        return pref
    if len(pref) > 1:
        raise SystemExit("xsess: ambiguous ref %r matches %d sessions:\n%s" % (
            raw, len(pref),
            "\n".join(f"  {AGENT_PREFIX[r['agent']]}:{r['sid']}  {r['title']}"
                      for r in pref[:10])))
    hits = list(con.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(where + ['(title LIKE ? OR first_user LIKE ?)'])}"
        " ORDER BY ended_ms DESC LIMIT 20", args + [f"%{body}%", f"%{body}%"]))
    if not hits:
        raise SystemExit(f"xsess: nothing matches {raw!r} (try `xsess search {body}`)")
    if len(hits) > 1 and not allow_many:
        lines = "\n".join(f"  {AGENT_PREFIX[r['agent']]}:{r['sid'][:8]}  {fmt_ts(r['ended_ms'])}"
                          f"  {r['title']}" for r in hits)
        raise SystemExit(f"xsess: {len(hits)} sessions match {body!r}:\n{lines}")
    return hits


def short(row) -> str:
    return f"{AGENT_PREFIX[row['agent']]}:{row['sid']}"


def snippet(body: str, terms: list[str], width: int = 220) -> str:
    text = re.sub(r"\s+", " ", body or "").strip()
    low = text.lower()
    pos = -1
    for term in terms:
        p = low.find(term.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return oneline(text, width)
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    out = text[start:end]
    return ("…" if start else "") + out + ("…" if end < len(text) else "")


def query_terms(query: str) -> list[str]:
    tokens = re.findall(r'"[^"]*"|\S+', query)
    terms = []
    for tok in tokens:
        if tok in ("AND", "OR", "NOT"):
            continue
        tok = tok.strip('-*').strip('"')
        if tok:
            terms.append(tok)
    return terms


def role_filter(spec: str | None, default: tuple) -> list[str]:
    if not spec:
        return list(default)
    if spec in ("all", "*"):
        return list(ALL_ROLES)
    wanted = []
    for part in re.split(r"[,\s]+", spec):
        part = part.strip().lower()
        if not part:
            continue
        alias = {"tool_result": "tool_out", "output": "tool_out", "thinking": "reasoning",
                 "human": "user", "agent": "assistant", "sum": "summary"}.get(part, part)
        if alias not in ALL_ROLES:
            raise SystemExit(f"xsess: unknown role {part!r} (roles: {', '.join(ALL_ROLES)})")
        wanted.append(alias)
    return wanted


def agent_filter(spec: str | None) -> list[str]:
    if not spec or spec in ("both", "all"):
        return list(ALL_AGENTS)
    out = []
    for part in re.split(r"[,\s]+", spec.lower()):
        if part in ("cc", "claude"):
            out.append("claude")
        elif part in ("cx", "codex", "gpt"):
            out.append("codex")
        elif part in ("cr", "cu", "cursor"):
            out.append("cursor")
        elif part in ("km", "ki", "kimi"):
            out.append("kimi")
        elif part:
            raise SystemExit(f"xsess: unknown agent {part!r} (use cc|cx|cr|km)")
    return out or list(ALL_AGENTS)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_index(args):
    con = connect(args.db)
    stats = sync(con, full=args.full, jobs=args.jobs, quiet=False, verbose=args.verbose)
    print(f"xsess: {stats['parsed']} file(s) parsed, {stats['msgs']} message(s) added, "
          f"{stats['files']} transcript(s) known, {stats['seconds']}s")
    if stats["errors"]:
        print(f"  {len(stats['errors'])} file(s) failed:", file=sys.stderr)
        for path, err in stats["errors"][:10]:
            print(f"    {path}: {err}", file=sys.stderr)
    row = con.execute("SELECT COUNT(*) c, "
                      "SUM(CASE WHEN agent='claude' THEN 1 ELSE 0 END) cc, "
                      "SUM(CASE WHEN agent='codex' THEN 1 ELSE 0 END) cx, "
                      "SUM(CASE WHEN agent='cursor' THEN 1 ELSE 0 END) cr, "
                      "SUM(CASE WHEN agent='kimi' THEN 1 ELSE 0 END) km "
                      "FROM sessions").fetchone()
    print(f"  sessions: {row['c']} total ({row['cc']} claude, {row['cx']} codex, "
          f"{row['cr']} cursor, {row['km']} kimi)")


def _list_rows(con, args, limit_default=30):
    where, params = ["1=1"], []
    agents = agent_filter(args.agent)
    where.append("agent IN (%s)" % ",".join("?" * len(agents)))
    params += agents
    if not args.all_kinds:
        where.append("kind != 'subagent'")
    if args.since:
        where.append("COALESCE(ended_ms, started_ms) >= ?")
        params.append(parse_since(args.since))
    if args.cwd:
        where.append("cwd LIKE ?")
        params.append(f"%{args.cwd}%")
    if getattr(args, "title", None):
        where.append("(title LIKE ? OR first_user LIKE ?)")
        params += [f"%{args.title}%", f"%{args.title}%"]
    if not args.include_empty:
        where.append("n_user > 0")
    sql = ("SELECT * FROM sessions WHERE " + " AND ".join(where) +
           " ORDER BY COALESCE(ended_ms, started_ms) DESC LIMIT ?")
    params.append(args.n or limit_default)
    return list(con.execute(sql, params))


def cmd_list(args):
    con = open_index(args)
    rows = _list_rows(con, args)
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
        return
    if not rows:
        print("(no sessions)")
        return
    for r in rows:
        tag = AGENT_PREFIX.get(r["agent"], r["agent"][:2])
        mark = "" if r["kind"] == "main" else f" [{r['kind']}]"
        arch = " [archived]" if r["archived"] else ""
        host = f" @{r['host']}" if r["host"] else ""
        print(f"{tag}:{r['sid'][:8]}  {fmt_ts(r['ended_ms'])}  {r['n_msg']:>5} msg"
              f"  {oneline(r['title'] or r['first_user'], 76)}{mark}{arch}{host}")
        if args.long:
            print(f"           {r['cwd'] or '-'}  {r['model'] or ''}  {r['sid']}"
                  + (f"  @{r['host']}" if r["host"] else ""))


def cmd_search(args):
    con = open_index(args)
    agents = agent_filter(args.agent)
    roles = role_filter(args.role, DEFAULT_SEARCH_ROLES)
    match = fts_query(args.query)
    where = ["s.agent IN (%s)" % ",".join("?" * len(agents)),
             "m.role IN (%s)" % ",".join("?" * len(roles))]
    params: list = [match] + agents + roles
    if not args.all_kinds:
        where.append("s.kind != 'subagent'")
    if args.since:
        where.append("COALESCE(m.ts, s.ended_ms) >= ?")
        params.append(parse_since(args.since))
    if args.session:
        rows = resolve(con, args.session, allow_many=True)
        refs = [short(r) for r in rows]
        where.append("m.ref IN (%s)" % ",".join("?" * len(refs)))
        params += refs
    if args.cwd:
        where.append("s.cwd LIKE ?")
        params.append(f"%{args.cwd}%")
    order = "m.ts DESC" if args.recent else "f.rank"
    sql = (
        "SELECT m.ref, m.seq, m.ts, m.role, m.name, m.body, s.title, s.agent, s.sid,"
        "       s.kind, f.rank AS rank"
        " FROM msg_fts f JOIN messages m ON m.id = f.rowid"
        " JOIN sessions s ON s.ref = m.ref"
        " WHERE msg_fts MATCH ? AND " + " AND ".join(where) +
        f" ORDER BY {order} LIMIT ?")
    params.append(args.n or 20)
    try:
        rows = list(con.execute(sql, params))
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"xsess: bad query ({exc}); match expression was: {match}")
    if args.json:
        print(json.dumps([{k: r[k] for k in r.keys()} for r in rows],
                         ensure_ascii=False, indent=1))
        return
    if not rows:
        print(f"(no hits for {args.query!r})")
        return
    terms = query_terms(args.query)
    for r in rows:
        tag = AGENT_PREFIX.get(r["agent"], r["agent"][:2])
        print(f"{tag}:{r['sid'][:8]} #{r['seq']:<5} {fmt_ts(r['ts'], short=True)}"
              f" [{r['role']}] {oneline(r['title'], 60)}")
        print(f"    {snippet(r['body'], terms, args.width)}")
    last = rows[-1]
    last_ref = f"{AGENT_PREFIX.get(last['agent'], last['agent'][:2])}:{last['sid'][:8]}"
    print(f"-- {len(rows)} hit(s); read one with: "
          f"xsess show {last_ref} --around {last['seq']}")


def cmd_show(args):
    con = open_index(args)
    rows = resolve(con, args.ref, allow_many=args.all_matches)
    for row in rows:
        _show_one(con, row, args)


def _show_one(con, row, args):
    ref = short(row)
    roles = role_filter(args.role, ALL_ROLES if args.full else DEFAULT_SHOW_ROLES)
    where = ["ref=?", "role IN (%s)" % ",".join("?" * len(roles))]
    params: list = [ref] + roles
    if args.around is not None:
        lo, hi = args.around - args.context, args.around + args.context
        where.append("seq BETWEEN ? AND ?")
        params += [lo, hi]
    elif args.range:
        m = re.fullmatch(r"(\d*)[:\-](\d*)", args.range)
        if not m:
            raise SystemExit("xsess: --range wants A:B (either side may be empty)")
        lo = int(m.group(1) or 0)
        hi = int(m.group(2) or 10**9)
        where.append("seq BETWEEN ? AND ?")
        params += [lo, hi]
    if args.grep:
        where.append("body LIKE ?")
        params.append(f"%{args.grep}%")
    order = "seq DESC" if args.tail is not None else "seq ASC"
    limit = args.tail or args.limit if args.tail is not None else args.limit
    sql = ("SELECT * FROM messages WHERE " + " AND ".join(where) +
           f" ORDER BY {order} LIMIT ?")
    params.append(limit)
    msgs = list(con.execute(sql, params))
    if args.tail is not None:
        msgs.reverse()
    if args.json:
        print(json.dumps({"session": dict(row),
                          "messages": [dict(m) for m in msgs]},
                         ensure_ascii=False, indent=1))
        return
    total = con.execute("SELECT COUNT(*) c FROM messages WHERE ref=?", (ref,)).fetchone()["c"]
    print(f"=== {ref}  {row['title']}")
    meta = [row["agent"], row["cwd"] or "-",
            f"{fmt_ts(row['started_ms'])} → {fmt_ts(row['ended_ms'])}",
            f"{total} items", row["model"] or "", row["kind"]]
    if row["archived"]:
        meta.append("archived")
    if row["host"]:
        meta.append(f"@{row['host']}")
    print("    " + " · ".join(x for x in meta if x))
    if row["parent"]:
        print(f"    parent: {AGENT_PREFIX[row['agent']]}:{row['parent'][:8]}")
    print(f"    file: {row['path']}")
    cap = None if args.max_chars == 0 else args.max_chars
    for m in msgs:
        label = m["role"] if not m["name"] else f"{m['role']}:{m['name']}"
        print(f"\n#{m['seq']} [{label}] {fmt_ts(m['ts'], short=True)}")
        body = m["body"] or ""
        if m["role"] in ("tool", "tool_out", "meta") and not args.full:
            print("  " + oneline(body, 300))
        else:
            print(indent(squash(body, cap)))
    if not msgs:
        print("\n(no messages matched)")
    print(f"\n-- {len(msgs)}/{total} items shown; "
          f"more: xsess show {ref} --range {msgs[-1]['seq'] if msgs else 1}: "
          f"| --role all | --full")


def indent(text: str, pad: str = "  ") -> str:
    return "\n".join(pad + line for line in (text or "").splitlines())


def cmd_ref(args):
    con = open_index(args)
    for row in resolve(con, args.ref, allow_many=True):
        ref = short(row)
        host = f"  @{row['host']}" if row["host"] else ""
        print(f"[{ref}] {row['title']} — {row['agent']}{host}, {fmt_ts(row['started_ms'])}, "
              f"{row['n_msg']} items, cwd={row['cwd'] or '-'}  (xsess show {ref})")


def cmd_grep(args):
    """Regex sweep over raw transcripts: finds text the index truncates (tool output)."""
    con = open_index(args)
    where, params = ["1=1"], []
    agents = agent_filter(args.agent)
    where.append("agent IN (%s)" % ",".join("?" * len(agents)))
    params += agents
    since = parse_since(args.since or ("30d" if not args.all_time else None))
    if since:
        where.append("COALESCE(ended_ms, started_ms) >= ?")
        params.append(since)
    rows = list(con.execute(
        "SELECT ref, sid, agent, title, path FROM sessions WHERE " + " AND ".join(where) +
        " ORDER BY ended_ms DESC", params))
    pattern = re.compile(args.pattern, 0 if args.case_sensitive else re.I)
    print(f"xsess: grepping {len(rows)} transcript(s)…", file=sys.stderr)
    hits = 0
    for row in rows:
        try:
            with open(row["path"], "r", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if not pattern.search(line):
                        continue
                    hits += 1
                    tag = AGENT_PREFIX[row["agent"]]
                    print(f"{tag}:{row['sid'][:8]} L{lineno} {oneline(row['title'], 40)}")
                    m = pattern.search(line)
                    start = max(0, m.start() - 80)
                    print("    " + oneline(line[start:m.end() + 140], args.width))
                    if hits >= (args.n or 40):
                        return
        except OSError:
            continue
    if not hits:
        print("(no hits)")


def cmd_stats(args):
    con = open_index(args)
    print(f"index: {args.db} ({args.db.stat().st_size / 1e6:.1f} MB)")
    for agent in ALL_AGENTS:
        row = con.execute(
            "SELECT COUNT(*) n, SUM(n_msg) msgs, MIN(started_ms) a, MAX(ended_ms) b,"
            " SUM(CASE WHEN kind='subagent' THEN 1 ELSE 0 END) sub"
            " FROM sessions WHERE agent=?", (agent,)).fetchone()
        print(f"  {agent:7} {row['n'] or 0:>5} sessions ({row['sub'] or 0} subagent), "
              f"{row['msgs'] or 0:>7} items, {fmt_ts(row['a'])} → {fmt_ts(row['b'])}")
    row = con.execute("SELECT COUNT(*) c FROM messages").fetchone()
    print(f"  indexed items: {row['c']}")
    for r in con.execute(
            "SELECT host, COUNT(*) n, SUM(n_msg) m FROM sessions"
            " WHERE host != '' GROUP BY host"):
        print(f"  @{r['host']}: {r['n']} imported session(s), {r['m'] or 0} items"
              f"  (mirror: {REMOTE_ROOT / r['host']})")
    for r in con.execute("SELECT role, COUNT(*) c FROM messages GROUP BY role"
                         " ORDER BY c DESC"):
        print(f"    {r['role']:10} {r['c']}")
    last = con.execute("SELECT v FROM state WHERE k='last_sync'").fetchone()
    if last:
        print(f"  last sync: {fmt_ts(int(last['v']) * 1000)}")
    bad = list(con.execute("SELECT path, error FROM files WHERE error IS NOT NULL"))
    if bad:
        print(f"  {len(bad)} file(s) with parse errors (see `xsess index -v`)")


def cmd_which(args):
    """Print the session the *caller* is in, when discoverable."""
    con = open_index(args)
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("CLAUDE_SESSION_ID")
           or os.environ.get("CODEX_THREAD_ID")
           or os.environ.get("CODEX_SESSION_ID"))
    if sid:
        for row in resolve(con, sid, allow_many=True):
            print(f"{short(row)}  {row['title']}")
        return
    row = con.execute("SELECT * FROM sessions ORDER BY ended_ms DESC LIMIT 1").fetchone()
    if row:
        print(f"{short(row)}  {row['title']}   (most recently active)")


# ---------------------------------------------------------------------------
# bundles: move sessions between machines
# ---------------------------------------------------------------------------

def _host_tag() -> str:
    return socket.gethostname().split(".")[0] or "unknown"


def _bundle_selection(con, args) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    if args.refs:
        for ref in args.refs:
            rows += resolve(con, ref)
    if args.since:
        agents = agent_filter(getattr(args, "agent", None))
        where = ["kind='main'", "COALESCE(ended_ms, started_ms) >= ?",
                 "agent IN (%s)" % ",".join("?" * len(agents))]
        rows += con.execute(
            "SELECT * FROM sessions WHERE " + " AND ".join(where) +
            " ORDER BY COALESCE(ended_ms, started_ms) DESC",
            [parse_since(args.since)] + agents).fetchall()
    # dedup, and pull in subagent children so a main session travels complete
    by_ref: dict[str, sqlite3.Row] = {}
    queue = list(rows)
    while queue:
        row = queue.pop(0)
        if row["ref"] in by_ref:
            continue
        by_ref[row["ref"]] = row
        queue += con.execute("SELECT * FROM sessions WHERE parent=? AND agent=?",
                             (row["sid"], row["agent"])).fetchall()
    return list(by_ref.values())


def _arcname(agent: str, path: Path) -> str | None:
    """<agent>/<path relative to that store's root>, local or already mirrored."""
    try:
        return f"{agent}/{Path(path).relative_to(AGENT_HOME[agent])}"
    except ValueError:
        pass
    if REMOTE_ROOT.is_dir():
        for host_dir in REMOTE_ROOT.iterdir():
            try:
                return f"{agent}/{Path(path).relative_to(host_dir / agent)}"
            except ValueError:
                continue
    return None


def cmd_bundle(args):
    con = open_index(args)
    rows = _bundle_selection(con, args)
    if not rows:
        raise SystemExit("xsess: nothing to bundle (give refs and/or --since)")
    host = args.host or _host_tag()
    manifest = {"host": host, "created_ms": int(time.time() * 1000), "sessions": {}}
    members: list[tuple[str, Path]] = []
    for row in rows:
        ref, agent = row["ref"], row["agent"]
        arcs = []
        for (path,) in con.execute(
                "SELECT path FROM files WHERE ref=? AND error IS NULL ORDER BY path",
                (ref,)):
            path = Path(path)
            arc = _arcname(agent, path)
            if arc is None:
                print(f"xsess: skipping {path} (outside every known store)",
                      file=sys.stderr)
                continue
            arcs.append(arc)
            members.append((arc, path))
            side = path.with_suffix(".meta.json")  # claude subagent title sidecar
            if agent == "claude" and side.is_file():
                side_arc = _arcname(agent, side)
                if side_arc:
                    arcs.append(side_arc)
                    members.append((side_arc, side))
        manifest["sessions"][ref] = {
            "agent": agent, "sid": row["sid"], "title": row["title"],
            "title_src": row["title_src"], "cwd": row["cwd"], "model": row["model"],
            "kind": row["kind"], "parent": row["parent"], "nickname": row["nickname"],
            "files": arcs,
        }
    if args.out:
        out = open(args.out, "wb")
        where = args.out
    else:
        if sys.stdout.isatty():
            print("xsess: writing binary bundle to stdout (pipe it somewhere or use -o)",
                  file=sys.stderr)
        out = sys.stdout.buffer
        where = "stdout"
    payload = json.dumps(manifest, ensure_ascii=False).encode()
    try:
        with tarfile.open(fileobj=out, mode="w|gz", compresslevel=6) as tf:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
            for arc, path in members:
                tf.add(str(path), arcname=arc, recursive=False)
    finally:
        if args.out:
            out.close()
    total = sum(p.stat().st_size for _, p in members) / 1e6
    print(f"xsess: bundled {len(manifest['sessions'])} session(s) from {host!r}, "
          f"{len(members)} file(s), ~{total:.0f} MB raw → {where}", file=sys.stderr)


def _validate_arc(name: str) -> tuple[str, str] | None:
    """Whitelist one bundle member: <agent>/<path matching that store's layout>."""
    name = name.lstrip("/")
    if "/" not in name:
        return None
    agent, rel = name.split("/", 1)
    if agent not in STORE_GLOBS:
        return None
    rel = rel.lstrip("/")
    pats = STORE_GLOBS[agent] + SIDECAR_GLOBS.get(agent, ())
    if any(fnmatch.fnmatch(rel, pat) for pat in pats):
        return agent, rel
    return None


def _merge_host_manifest(host: str, manifest: dict, skipped_refs: set) -> None:
    """Persist/merge the bundle's session meta for this host (idempotent).

    Called as soon as the manifest is read — before any file is extracted — so
    an interrupted import still leaves a self-healing state: the next sync
    indexes whatever did land and tags it @host.
    """
    mf = REMOTE_ROOT / host / "manifest.json"
    merged: dict = {}
    if mf.exists():
        try:
            merged = json.loads(mf.read_text()).get("sessions", {})
        except Exception:
            merged = {}
    merged = {k: v for k, v in merged.items() if k not in skipped_refs}
    merged.update({k: v for k, v in manifest.get("sessions", {}).items()
                   if k not in skipped_refs})
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps({"host": host, "sessions": merged}, ensure_ascii=False))


def cmd_import(args):
    if args.bundle != "-" and not Path(args.bundle).is_file():
        raise SystemExit(f"xsess: no such bundle: {args.bundle}")
    con = open_index(args)
    if args.bundle == "-":
        src, close = sys.stdin.buffer, False
    else:
        src, close = open(args.bundle, "rb"), True
    host: str | None = None
    manifest: dict = {}
    skip_arcs: set[str] = set()
    skipped: list[tuple[str, str]] = []   # (ref, existing host)
    extracted = 0
    try:
        with tarfile.open(fileobj=src, mode="r|*") as tf:
            for member in tf:
                name = member.name.lstrip("./")
                if name == "manifest.json":
                    manifest = json.loads(tf.extractfile(member).read() or b"{}")
                    host = args.host or manifest.get("host") or "unknown"
                    for ref, info in manifest.get("sessions", {}).items():
                        row = con.execute(
                            "SELECT host, path FROM sessions WHERE ref=?",
                            (ref,)).fetchone()
                        if not row:
                            continue
                        already_remote = bool(row["path"]) and str(row["path"]).startswith(
                            str(REMOTE_ROOT) + os.sep)
                        if row["host"] != host and not (already_remote and row["host"] == ""):
                            # exists on this machine for real (local, or mirrored
                            # under another tag): never duplicate a ref; an
                            # untagged remote path is a half-done import → update
                            skipped.append((ref, row["host"]))
                            skip_arcs.update(info.get("files", []))
                    _merge_host_manifest(host, manifest, {r for r, _ in skipped})
                    continue
                if not member.isfile() or name in skip_arcs:
                    continue
                if host is None:
                    raise SystemExit("xsess: bundle lacks a leading manifest.json — refusing")
                ok = _validate_arc(name)
                if ok is None:
                    print(f"xsess: refusing unexpected member {member.name!r}",
                          file=sys.stderr)
                    continue
                agent, rel = ok
                dest = REMOTE_ROOT / host / agent / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as fh:
                    shutil.copyfileobj(tf.extractfile(member), fh)
                extracted += 1
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SystemExit(
            f"xsess: bundle stream failed mid-import ({type(exc).__name__}: {exc});"
            " extracted files stay mirrored and will be indexed by the next sync"
            " — simply re-run the import to complete it")
    finally:
        if close:
            src.close()
    if not manifest:
        raise SystemExit("xsess: no manifest.json in bundle — not an xsess bundle?")
    assert host is not None
    stats = sync(con)
    fresh = [ref for ref in manifest["sessions"] if ref not in {r for r, _ in skipped}]
    print(f"xsess: imported {len(fresh)} session(s) as @{host} "
          f"({extracted} file(s), {stats['msgs']} new item(s) indexed)")
    for ref in fresh:
        info = manifest["sessions"][ref]
        print(f"  {ref}  {oneline(info.get('title') or '', 70)}")
    for ref, have in skipped:
        why = "already local" if have == "" else f"already mirrored as @{have}"
        print(f"  (skipped {ref}: {why})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="xsess",
        description="Search and read Claude Code, Codex, Cursor, and Kimi sessions from one index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  xsess list -n 15                      recent sessions from all agents
  xsess list -a km --since 3d           recent Kimi threads only
  xsess search "fully-async GRPO"       full-text search (CJK works: 训练 数据)
  xsess search 训练 -a cx --role user   only what the human typed, Codex side
  xsess show cx:01a08c47                read a thread (prefix of the id is enough)
  xsess show "autoresearch" --tail 20    resolve by title, last 20 items
  xsess show km:533ab17c --around 1 -C 3
  xsess grep 'CUDA out of memory' -a cx  regex over raw transcripts (deep, slower)
  xsess ref cx:01a08c47                  one-line citation to paste in the other agent
  xsess bundle cc:9f2c | ssh box 'xsess import -'    share sessions with box
  ssh box 'xsess bundle --since 7d' | xsess import -  pull box's recent sessions
refs: cc:<id> Claude Code, cx:<id> Codex, cr:<id> Cursor, km:<id> Kimi; any unique
id prefix or a distinctive piece of the title also resolves.""")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"index path ({DEFAULT_DB})")
    p.add_argument("--no-sync", action="store_true",
                   help="skip the incremental freshness check")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_scope(sp, n_default=None):
        sp.add_argument("-a", "--agent", help="cc|cx|cr|km (default all)")
        sp.add_argument("--since", help="7d | 36h | 2026-09-01")
        sp.add_argument("--cwd", help="filter by session cwd substring")
        sp.add_argument("-n", type=int, default=n_default, help="max results")
        sp.add_argument("--all-kinds", action="store_true",
                        help="include subagent sessions")
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("index", help="build/refresh the index")
    sp.add_argument("--full", action="store_true", help="rebuild from scratch")
    sp.add_argument("-j", "--jobs", type=int, help="parser processes")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("list", help="recent sessions with titles")
    common_scope(sp, 30)
    sp.add_argument("--title", help="filter by title substring")
    sp.add_argument("-l", "--long", action="store_true", help="show cwd/model/full id")
    sp.add_argument("--include-empty", action="store_true",
                    help="include sessions with no human turn")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("search", help="full-text search across all agents")
    sp.add_argument("query")
    common_scope(sp, 20)
    sp.add_argument("--role", help=f"comma list of {', '.join(ALL_ROLES)} or 'all' "
                                   f"(default {', '.join(DEFAULT_SEARCH_ROLES)})")
    sp.add_argument("--session", help="restrict to one session (ref or title)")
    sp.add_argument("--recent", action="store_true", help="sort by time, not relevance")
    sp.add_argument("--width", type=int, default=220, help="snippet width")
    sp.set_defaults(include_empty=True, func=cmd_search)

    sp = sub.add_parser("show", help="read a session transcript")
    sp.add_argument("ref", help="cc:/cx:/cr:/km: ref, id prefix, or title substring")
    sp.add_argument("--range", help="message range A:B (by #seq)")
    sp.add_argument("--around", type=int, help="center on a message #seq")
    sp.add_argument("-C", "--context", type=int, default=4, help="items around --around")
    sp.add_argument("--role", help="comma list of roles, or 'all'")
    sp.add_argument("--grep", help="only items containing this substring")
    sp.add_argument("--limit", type=int, default=80, help="max items (default 80)")
    sp.add_argument("-t", "--tail", type=int, nargs="?", const=0, metavar="N",
                    help="take the last N items (default --limit)")
    sp.add_argument("--max-chars", type=int, default=4000,
                    help="per-item cap, 0 = unlimited")
    sp.add_argument("--full", action="store_true",
                    help="include reasoning/tool output verbatim")
    sp.add_argument("--all-matches", action="store_true",
                    help="print every session matching an ambiguous ref")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("ref", help="one-line citation for a session")
    sp.add_argument("ref")
    sp.set_defaults(func=cmd_ref)

    sp = sub.add_parser("grep", help="regex sweep over raw transcripts (deep)")
    sp.add_argument("pattern")
    sp.add_argument("-a", "--agent")
    sp.add_argument("--since", help="default 30d")
    sp.add_argument("--all-time", action="store_true")
    sp.add_argument("-n", type=int, default=40)
    sp.add_argument("-s", "--case-sensitive", action="store_true")
    sp.add_argument("--width", type=int, default=240)
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("stats", help="index overview")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("which", help="which session am I?")
    sp.set_defaults(func=cmd_which)

    sp = sub.add_parser("bundle", help="pack sessions into a tar.gz for another machine")
    sp.add_argument("refs", nargs="*", help="refs / id prefixes / titles to include")
    sp.add_argument("--since", help="also include main sessions newer than 7d|36h|date")
    sp.add_argument("-a", "--agent", help="with --since: cc|cx|cr|km (default all)")
    sp.add_argument("--host", help=f"host tag to stamp (default {_host_tag()})")
    sp.add_argument("-o", "--out", help="write to this file instead of stdout")
    sp.set_defaults(func=cmd_bundle)

    sp = sub.add_parser("import", help="unpack a bundle from another machine")
    sp.add_argument("bundle", nargs="?", default="-",
                    help="bundle file, or '-' / omitted for stdin")
    sp.add_argument("--host", help="mirror under this host tag (default: manifest's)")
    sp.set_defaults(func=cmd_import)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
