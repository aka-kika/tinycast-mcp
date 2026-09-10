#!/usr/bin/env python3
"""
tinycast-mcp-manager — add/remove/inspect MCP servers in Tinycast WITHOUT the Settings UI.

Tinycast keeps its MCP server list in UserDefaults (domain com.tinycast.app,
key "mcpServers") as a JSON-encoded [MCPServer] array — verified against the
app's own Codable synthesis. Secrets (HTTP header values, stdio env values)
live in the login Keychain and are intentionally NOT handled here: this tool
only manages secret-free servers.

IMPORTANT: Tinycast reads the list at launch, so quit Tinycast first, run
this, then reopen. Safe order:
    osascript -e 'quit app "Tinycast"'
    python3 mcp-manager.py enable-mcp
    python3 mcp-manager.py add-stdio tinycast uv run --script /path/server.py
    open -a Tinycast

Every mutation exports the whole defaults domain, edits it, and re-imports it,
keeping a timestamped backup in the same folder.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

DOMAIN = "com.tinycast.app"
KEY_SERVERS = "mcpServers"
KEY_ENABLED = "mcpEnabled"
BACKUP_DIR = Path(tempfile.gettempdir()) / "tinycast-mcp-manager-backups"


def _export_domain() -> dict:
    out = subprocess.run(
        ["defaults", "export", DOMAIN, "-"], capture_output=True, check=True
    ).stdout
    return plistlib.loads(out)


def _import_domain(domain: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{DOMAIN}-{time.strftime('%Y%m%d-%H%M%S')}.plist"
    before = _export_domain()
    with open(backup, "wb") as fh:
        plistlib.dump(before, fh)
    with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as tf:
        plistlib.dump(domain, tf)
        path = tf.name
    subprocess.run(["defaults", "import", DOMAIN, path], check=True,
                   capture_output=True)
    os.unlink(path)
    print(f"(backup of previous state: {backup})")


def _load_servers(domain: dict) -> list[dict]:
    raw = domain.get(KEY_SERVERS)
    if not raw:
        return []
    if not isinstance(raw, bytes):
        raw = str(raw).encode()
    return json.loads(raw.decode("utf-8"))


def _save_servers(domain: dict, servers: list[dict]) -> None:
    if servers:
        domain[KEY_SERVERS] = json.dumps(servers).encode("utf-8")
        _import_domain(domain)
    else:
        # `defaults import` merges, it does not replace — delete the key explicitly.
        _import_domain(domain)
        subprocess.run(["defaults", "delete", DOMAIN, KEY_SERVERS],
                       check=False, capture_output=True)


def _slug_for(name: str, existing: set[str]) -> str:
    slug = re.sub(r"[^0-9a-z]+", "-", name.lower()).strip("-")[:24] or "server"
    if slug not in existing:
        return slug
    for n in range(2, 100):
        cand = f"{slug[:21]}-{n}"
        if cand not in existing:
            return cand
    raise SystemExit("cannot derive a unique slug")


def _new_server(name: str, transport: dict, existing: set[str],
                trust: str, enabled: bool) -> dict:
    return {
        "id": str(uuid.uuid4()).upper(),
        "name": name,
        "slug": _slug_for(name, existing),
        "transport": transport,
        "isEnabled": enabled,
        "trust": trust,
    }


def cmd_list(_args) -> None:
    servers = _load_servers(_export_domain())
    enabled = _export_domain().get(KEY_ENABLED)
    print(f"mcpEnabled: {bool(enabled) if enabled is not None else False}")
    if not servers:
        print("mcpServers: (none)")
        return
    print(f"mcpServers: {len(servers)}")
    for s in servers:
        t = s.get("transport", {})
        if "stdio" in t:
            d = t["stdio"]
            where = " ".join([d.get("command", "?")] + d.get("arguments", []))
        elif "http" in t:
            where = t["http"].get("url", "?")
        else:
            where = "?"
        print(f"  - {s.get('name')}  [@{s.get('slug')}]  enabled={s.get('isEnabled')}  "
              f"trust={s.get('trust')}  → {where}")


def cmd_add_stdio(args) -> None:
    import shlex
    parts = shlex.split(args.commandline)
    if not parts:
        print("Empty commandline.")
        sys.exit(1)
    domain = _export_domain()
    servers = _load_servers(domain)
    existing = {s.get("slug") for s in servers}
    slug = args.slug or _slug_for(args.name, existing)
    if slug in existing:
        print(f"A server with slug '{slug}' already exists.")
        sys.exit(1)
    transport = {"stdio": {
        "command": parts[0],
        "arguments": parts[1:],
        "environmentKeys": [],
    }}
    servers.append(_new_server(args.name, transport, existing, args.trust, not args.disabled))
    _save_servers(domain, servers)
    print(f"Added stdio server '{args.name}' as @{slug} → "
          f"{' '.join([parts[0]] + parts[1:])}")
    print("Quit and reopen Tinycast to load it.")


def cmd_add_http(args) -> None:
    domain = _export_domain()
    servers = _load_servers(domain)
    existing = {s.get("slug") for s in servers}
    slug = args.slug or _slug_for(args.name, existing)
    transport = {"http": {
        "url": args.url,
        "headerName": args.header_name,
    }}
    servers.append(_new_server(args.name, transport, existing | {slug}, args.trust, not args.disabled))
    _save_servers(domain, servers)
    print(f"Added http server '{args.name}' as @{slug} → {args.url}")
    if args.header_name != "Authorization" or True:
        print("NOTE: the Authorization header VALUE lives in the login Keychain — "
              "enter it once in Tinycast Settings, or the connection will fail auth.")


def cmd_remove(args) -> None:
    domain = _export_domain()
    servers = _load_servers(domain)
    kept = [s for s in servers if s.get("slug") != args.slug]
    if len(kept) == len(servers):
        print(f"No server with slug '{args.slug}'.")
        sys.exit(1)
    _save_servers(domain, kept)
    print(f"Removed @{args.slug}. (Its Keychain secret, if any, was left in place.)")
    print("Quit and reopen Tinycast to apply.")


def cmd_toggle(args) -> None:
    domain = _export_domain()
    servers = _load_servers(domain)
    hit = False
    for s in servers:
        if s.get("slug") == args.slug:
            s["isEnabled"] = args.on
            hit = True
    if not hit:
        print(f"No server with slug '{args.slug}'.")
        sys.exit(1)
    _save_servers(domain, servers)
    print(f"@{args.slug} {'enabled' if args.on else 'disabled'}. Quit and reopen Tinycast.")


def cmd_trust(args) -> None:
    domain = _export_domain()
    servers = _load_servers(domain)
    hit = False
    for s in servers:
        if s.get("slug") == args.slug:
            s["trust"] = args.level
            hit = True
    if not hit:
        print(f"No server with slug '{args.slug}'.")
        sys.exit(1)
    _save_servers(domain, servers)
    print(f"@{args.slug} trust → {args.level}. Quit and reopen Tinycast.")


def cmd_enable_mcp(args) -> None:
    domain = _export_domain()
    domain[KEY_ENABLED] = args.on
    _import_domain(domain)
    print(f"mcpEnabled → {args.on}. Quit and reopen Tinycast to apply.")


def read_servers() -> list[dict]:
    """Public helper for other tools (e.g. tinycast's own MCP server)."""
    return _load_servers(_export_domain())


def write_servers(servers: list[dict]) -> None:
    """Public helper: replace the whole server list (keeps a backup)."""
    _save_servers(_export_domain(), servers)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    add = sub.add_parser("add-stdio", help="add a command-based (stdio) server")
    add.add_argument("name")
    add.add_argument(
        "commandline",
        help="the full command as ONE quoted string, e.g. \"uv run --script /path/server.py\"",
    )
    add.add_argument("--slug")
    add.add_argument("--trust", choices=["ask", "always", "never"], default="ask")
    add.add_argument("--disabled", action="store_true")
    add.set_defaults(func=cmd_add_stdio)

    addh = sub.add_parser("add-http", help="add a remote HTTP server")
    addh.add_argument("name")
    addh.add_argument("url")
    addh.add_argument("--header-name", default="Authorization")
    addh.add_argument("--slug")
    addh.add_argument("--trust", choices=["ask", "always", "never"], default="ask")
    addh.add_argument("--disabled", action="store_true")
    addh.set_defaults(func=cmd_add_http)

    rm = sub.add_parser("remove", help="remove a server by slug")
    rm.add_argument("slug")
    rm.set_defaults(func=cmd_remove)

    en = sub.add_parser("enable", help="enable a server by slug")
    en.add_argument("slug")
    en.set_defaults(func=cmd_toggle, on=True)
    dis = sub.add_parser("disable", help="disable a server by slug")
    dis.add_argument("slug")
    dis.set_defaults(func=cmd_toggle, on=False)

    tr = sub.add_parser("trust", help="set trust level: ask | always | never")
    tr.add_argument("slug")
    tr.add_argument("level", choices=["ask", "always", "never"])
    tr.set_defaults(func=cmd_trust)

    em = sub.add_parser("enable-mcp", help="flip the global mcpEnabled flag")
    em.add_argument("--off", dest="on", action="store_false")
    em.set_defaults(func=cmd_enable_mcp, on=True)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()