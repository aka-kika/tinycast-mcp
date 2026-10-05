#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]  # v1 FastMCP API; mcp 2.x renamed it to MCPServer
# ///
"""
tinycast-mcp — a sidecar MCP server for Tinycast (com.tinycast.app).

Reads and writes the app's user-authored files directly:
  Snippets/  Markdown + strict frontmatter (SnippetMarkdownSerializer-compatible)
  Notes/     plain Markdown files (NotesRepository-compatible)

Tinycast picks up external writes: the Snippets store file-watches while the
feature is enabled, and Notes are re-read on access. Delete = move to Trash.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

TINYCAST_HOME = Path(
    os.environ.get(
        "TINYCAST_HOME",
        Path.home() / "Library" / "Application Support" / "com.tinycast.app",
    )
)
SNIPPETS_DIR = TINYCAST_HOME / "Snippets"
NOTES_DIR = TINYCAST_HOME / "Notes"
TRASH = Path.home() / ".Trash"

mcp = FastMCP("tinycast")


# --------------------------------------------------------------------------- #
# Codec — mirrors Tinycast's SnippetMarkdownSerializer exactly
# --------------------------------------------------------------------------- #

def _escape(value: str) -> str:
    """The app's encodeScalar: double quotes, only \\ \" \\n \\r \\t escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ('\\', '"', "n", "r", "t"):
                out.append({"n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt))
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return _unescape(raw[1:-1])
    raise ValueError(f"Expected a double-quoted string, got: {raw!r}")


def _parse_bool(raw: str) -> bool:
    raw = raw.strip()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"Expected lowercase true/false, got: {raw!r}")


def _slug(name: str) -> str:
    """The app's slug(for:): lowercase, split on non-alphanumerics, join '- '."""
    slug = "-".join(part for part in re.split(r"[^0-9A-Za-z]+", name.lower()) if part)
    return slug or "snippet"


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    """Drop a leading frontmatter block from a snippet body.

    serialize_snippet always writes frontmatter, so a body that already carries
    one would produce a second block. parse_snippet_file only scans up to the
    first closing delimiter, so that stray block would be swallowed into the
    body and duplicated again on the next write. Stripping here makes writes
    idempotent and heals files that already went wrong.
    """
    return _FRONTMATTER_RE.sub("", text, count=1)


def serialize_snippet(name: str, text: str, keyword: Optional[str],
                      enabled: bool, show_confirmation: bool) -> str:
    lines = ["---", f'name: "{_escape(name)}"']
    if keyword is not None:
        lines.append(f'keyword: "{_escape(keyword)}"')
    lines.append(f"enabled: {'true' if enabled else 'false'}")
    lines.append(f"show_confirmation: {'true' if show_confirmation else 'false'}")
    lines.append("---")
    return "\n".join(lines) + "\n" + strip_frontmatter(text)


def parse_snippet_file(path: Path) -> dict:
    """Parse a snippet file the way Tinycast does; tolerant of body-only files."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    first = lines[0].rstrip("\r\n") if lines else ""
    if first != "---":
        return {"name": path.stem, "keyword": None, "enabled": True,
                "show_confirmation": False, "text": content, "file": path.name,
                "has_frontmatter": False}

    closing = next((i for i, ln in enumerate(lines[1:], start=1)
                    if ln.rstrip("\r\n") == "---"), None)
    if closing is None:
        raise ValueError(f"{path.name}: missing closing frontmatter delimiter")

    name, keyword, enabled, confirmation = None, None, True, False
    for ln in lines[1:closing]:
        stripped = ln.strip()
        if not stripped:
            continue
        key, _, raw = stripped.partition(":")
        key = key.strip().lower()
        if key == "name":
            name = _parse_scalar(raw) or None
        elif key == "keyword":
            keyword = _parse_scalar(raw)
        elif key == "enabled":
            enabled = _parse_bool(raw)
        elif key == "show_confirmation":
            confirmation = _parse_bool(raw)
    text = "".join(lines[closing + 1:])
    return {"name": name or path.stem, "keyword": keyword, "enabled": enabled,
            "show_confirmation": confirmation, "text": text, "file": path.name,
            "has_frontmatter": True}


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #

def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _to_trash(path: Path) -> Path:
    TRASH.mkdir(exist_ok=True)
    target = TRASH / path.name
    n = 2
    stem, dot, ext = path.name.rpartition(".")
    while target.exists():
        target = TRASH / f"{stem} {n}.{ext}"
        n += 1
    shutil.move(str(path), str(target))
    return target


def _find_snippet(name: str) -> Path:
    """Match by frontmatter name first, then by filename — like the app resolves."""
    for path in sorted(SNIPPETS_DIR.glob("*.md")):
        try:
            if parse_snippet_file(path)["name"] == name:
                return path
        except ValueError:
            continue
    by_file = SNIPPETS_DIR / (_slug(name) + ".md")
    if by_file.exists():
        return by_file
    raise FileNotFoundError(f"No snippet named {name!r}.")


def _find_note(title: str) -> Path:
    title = title.strip()
    if title.lower().endswith(".md"):
        title = title[:-3]
    candidate = NOTES_DIR / f"{title}.md"
    if candidate.exists():
        return candidate
    matches = [p for p in NOTES_DIR.glob("*.md") if p.stem.lower() == title.lower()]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"No note titled {title!r}.")


def _validate_note_title(raw: str) -> str:
    """Mirrors NotesRepository.validatedTitle."""
    title = raw.strip()
    if title.lower().endswith(".md"):
        title = title[:-3].strip()
    if (not title or title in (".", "..") or title.startswith(".")
            or "/" in title or "\x00" in title):
        raise ValueError(f"{raw!r} can't be used as a note title.")
    return title


def _list_md(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        (p for p in directory.iterdir()
         if p.suffix.lower() == ".md" and p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


# --------------------------------------------------------------------------- #
# Snippet tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def add_snippet(name: str, text: str, keyword: Optional[str] = None,
                enabled: bool = True, show_confirmation: bool = False) -> str:
    """Create a new Tinycast snippet (Markdown with frontmatter).

    Tinycast hot-reloads it while the Snippets feature is enabled. `keyword`
    (e.g. "!notes") enables typed keyword expansion in other apps.
    """
    existing = {p.name for p in SNIPPETS_DIR.glob("*.md")} if SNIPPETS_DIR.exists() else set()
    base = _slug(name)
    path = SNIPPETS_DIR / f"{base}.md"
    n = 2
    while path.name in existing:
        path = SNIPPETS_DIR / f"{base}-{n}.md"
        n += 1
    if not text.endswith("\n") and text:
        text += "\n"
    _atomic_write(path, serialize_snippet(name, text, keyword, enabled, show_confirmation))
    return f"Created snippet {name!r} → Snippets/{path.name}"


@mcp.tool()
def list_snippets() -> str:
    """List all Tinycast snippets: name, keyword, enabled, file."""
    if not SNIPPETS_DIR.exists():
        return "Snippets library doesn't exist yet (feature never enabled). Empty."
    rows = []
    for path in _list_md(SNIPPETS_DIR):
        try:
            s = parse_snippet_file(path)
            rows.append(
                f"- {s['name']} | keyword: {s['keyword'] or '—'} | "
                f"enabled: {s['enabled']} | file: {path.name}"
            )
        except ValueError as err:
            rows.append(f"- [unparseable: {path.name}: {err}]")
    return "\n".join(rows) if rows else "No snippets yet."


@mcp.tool()
def read_snippet(name: str) -> str:
    """Read a snippet's full Markdown source by name."""
    path = _find_snippet(name)
    return path.read_text(encoding="utf-8")


@mcp.tool()
def update_snippet(name: str, text: Optional[str] = None,
                   keyword: Optional[str] = None,
                   enabled: Optional[bool] = None,
                   show_confirmation: Optional[bool] = None) -> str:
    """Update a snippet in place. Only the fields you pass change; others are kept."""
    path = _find_snippet(name)
    current = parse_snippet_file(path)
    if text is not None:
        current["text"] = text if (not text or text.endswith("\n")) else text + "\n"
    if keyword is not None:
        current["keyword"] = keyword
    if enabled is not None:
        current["enabled"] = enabled
    if show_confirmation is not None:
        current["show_confirmation"] = show_confirmation
    _atomic_write(path, serialize_snippet(
        current["name"], current["text"], current["keyword"],
        current["enabled"], current["show_confirmation"]))
    return f"Updated snippet {current['name']!r} ({path.name})."


@mcp.tool()
def delete_snippet(name: str) -> str:
    """Move a snippet to the Trash (recoverable, never hard-deleted)."""
    path = _find_snippet(name)
    target = _to_trash(path)
    return f"Trashed snippet {name!r} → {target}"


# --------------------------------------------------------------------------- #
# Note tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def add_note(title: str, content: str = "") -> str:
    """Create a new Tinycast note (plain Markdown file, title = filename).

    Appears in Tinycast's Notes immediately (notes are re-read live).
    """
    base = _validate_note_title(title)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    candidate = NOTES_DIR / f"{base}.md"
    n = 2
    while candidate.exists():
        candidate = NOTES_DIR / f"{base} {n}.md"
        n += 1
    _atomic_write(candidate, content if content.endswith("\n") or not content else content + "\n")
    return f"Created note {candidate.stem!r} → Notes/{candidate.name}"


@mcp.tool()
def list_notes() -> str:
    """List all Tinycast notes: title + last modified (newest first)."""
    paths = _list_md(NOTES_DIR)
    if not paths:
        return "No notes yet."
    return "\n".join(
        f"- {p.stem} | modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}"
        for p in paths
    )


@mcp.tool()
def read_note(title: str) -> str:
    """Read a note's full content by title."""
    return _find_note(title).read_text(encoding="utf-8")


@mcp.tool()
def update_note(title: str, content: str) -> str:
    """Replace a note's content entirely."""
    path = _find_note(title)
    _atomic_write(path, content if content.endswith("\n") or not content else content + "\n")
    return f"Updated note {path.stem!r} ({len(content)} chars)."


@mcp.tool()
def delete_note(title: str) -> str:
    """Move a note to the Trash (recoverable, never hard-deleted)."""
    path = _find_note(title)
    target = _to_trash(path)
    return f"Trashed note {path.stem!r} → {target}"


# --------------------------------------------------------------------------- #
# Tinycast self-management — let the app's AI edit Tinycast's own MCP list
# --------------------------------------------------------------------------- #

import importlib.util
import shlex

_manager_module = None


def _manager():
    """Load mcp-manager.py (same folder) as a library."""
    global _manager_module
    if _manager_module is None:
        path = Path(__file__).with_name("mcp-manager.py")
        if not path.exists():
            raise FileNotFoundError(
                "mcp-manager.py must sit next to server.py — MCP-server "
                "management tools need it.")
        spec = importlib.util.spec_from_file_location("tc_mcp_manager", path)
        _manager_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_manager_module)
    return _manager_module


@mcp.tool()
def list_mcp_servers() -> str:
    """List the MCP servers configured in Tinycast (name, @slug, enabled, trust, transport)."""
    m = _manager()
    servers = m.read_servers()
    if not servers:
        return "No MCP servers configured in Tinycast."
    rows = []
    for s in servers:
        t = s.get("transport", {})
        if "stdio" in t:
            d = t["stdio"]
            where = " ".join([d.get("command", "?")] + d.get("arguments", []))
        elif "http" in t:
            where = t["http"].get("url", "?")
        else:
            where = "?"
        rows.append(f"- {s.get('name')} [@{s.get('slug')}] enabled={s.get('isEnabled')} "
                    f"trust={s.get('trust')} → {where}")
    return "\n".join(rows)


@mcp.tool()
def add_mcp_server(name: str, command_line: str = "", http_url: str = "",
                   trust: str = "ask") -> str:
    """Add an MCP server to Tinycast itself.

    Give EITHER command_line (a stdio server, e.g. 'uvx some-mcp-server')
    OR http_url (a remote server). trust: 'ask' | 'always' | 'never'.
    NOTE: takes effect after Tinycast quits and reopens (the app reads its
    server list at launch). HTTP header secrets and stdio env values live in
    the login Keychain — set those once in Settings if the server needs them.
    """
    if bool(command_line) == bool(http_url):
        return "Provide exactly one of: command_line (stdio) or http_url."
    if trust not in ("ask", "always", "never"):
        return "trust must be ask, always or never."
    m = _manager()
    servers = m.read_servers()
    existing = {s.get("slug") for s in servers}
    slug = m._slug_for(name, existing)
    if slug in existing:
        return f"A server with slug '{slug}' already exists."
    if command_line:
        parts = shlex.split(command_line)
        if not parts:
            return "Empty command_line."
        transport = {"stdio": {"command": parts[0], "arguments": parts[1:],
                               "environmentKeys": []}}
    else:
        transport = {"http": {"url": http_url, "headerName": "Authorization"}}
    servers.append(m._new_server(name, transport, existing, trust, True))
    m.write_servers(servers)
    where = command_line or http_url
    return (f"Added '{name}' as @{slug} → {where}. "
            "Quit and reopen Tinycast for it to load.")


@mcp.tool()
def remove_mcp_server(slug: str) -> str:
    """Remove an MCP server from Tinycast by its @slug.

    Takes effect after Tinycast quits and reopens. Keychain secrets are left alone."""
    m = _manager()
    servers = m.read_servers()
    kept = [s for s in servers if s.get("slug") != slug]
    if len(kept) == len(servers):
        return f"No server with slug '{slug}'."
    m.write_servers(kept)
    return f"Removed @{slug}. Quit and reopen Tinycast to apply."


@mcp.tool()
def set_mcp_server(slug: str, enabled: Optional[bool] = None,
                   trust: Optional[str] = None) -> str:
    """Enable/disable a server and/or set its trust (ask | always | never) by @slug."""
    if enabled is None and trust is None:
        return "Nothing to change: pass enabled and/or trust."
    if trust is not None and trust not in ("ask", "always", "never"):
        return "trust must be ask, always or never."
    m = _manager()
    servers = m.read_servers()
    hit = False
    for s in servers:
        if s.get("slug") == slug:
            if enabled is not None:
                s["isEnabled"] = enabled
            if trust is not None:
                s["trust"] = trust
            hit = True
    if not hit:
        return f"No server with slug '{slug}'."
    m.write_servers(servers)
    return (f"Updated @{slug}"
            + (f" enabled={enabled}" if enabled is not None else "")
            + (f" trust={trust}" if trust is not None else "")
            + ". Quit and reopen Tinycast to apply.")


if __name__ == "__main__":
    mcp.run()  # stdio
