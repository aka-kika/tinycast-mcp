# tinycast-mcp

A companion MCP server for [Tinycast](https://github.com/abue-ammar/tinycast), the native macOS
launcher. It lets AI agents — and Tinycast's own built-in AI — read and write the app's
snippets, notes, and MCP server list. No fork, no IPC, no special app mode: it reads and
writes the files Tinycast already watches.

It exists because Tinycast's storage is agent-friendly by design. Snippets are
user-authored Markdown the store file-watches ("an interchange format, not an internal
one," per the app's docs), and notes are plain files re-read on access. Write a file
into `~/Library/Application Support/com.tinycast.app/Snippets/` and it appears in the
palette. This server is just a careful hand for doing that.

## Requirements

- macOS with [Tinycast](https://github.com/abue-ammar/tinycast) installed
- [uv](https://docs.astral.sh/uv/) (one command: `brew install uv`)
- Python 3.10+ (uv resolves the rest — the only dependency is `mcp`, pinned to v1)

## Tools (14)

**Snippets**
- `add_snippet(name, text, keyword?, enabled?, show_confirmation?)`
- `list_snippets()` / `read_snippet(name)` / `update_snippet(...)` / `delete_snippet(name)`

**Notes**
- `add_note(title, content?)` / `list_notes()` / `read_note(title)` /
  `update_note(title, content)` / `delete_note(title)`

**Tinycast self-management**
- `list_mcp_servers()` / `add_mcp_server(name, command_line | http_url, trust?)` /
  `remove_mcp_server(slug)` / `set_mcp_server(slug, enabled?, trust?)`

The self-management group lets Tinycast's own AI edit Tinycast's MCP server list
(the `mcpServers` key in `com.tinycast.app` defaults), writing the exact JSON Swift's
`Codable` synthesis produces — the shape was verified by compiling the app's own struct
definitions and round-tripping them through `JSONEncoder`.

## Use it inside Tinycast's AI

1. Settings → MCP: enable the feature, add a server
   - Command: `uv`
   - Arguments: `run --script /path/to/tinycast-mcp/server.py`
2. Put `mcp-manager.py` in the same folder as `server.py` (the server imports it).
3. Tool calling requires an API-key AI route (BYO key). Apple Intelligence and the
   ChatGPT subscription route don't offer MCP tools.
4. Then, from a chat: "add a note called test saying hello" — done.

## Use it from any other MCP client

Register a stdio server with the command:
`uv run --script /path/to/tinycast-mcp/server.py`
Any client that speaks MCP (Claude, Cursor, etc.) can then write snippets into the
palette and notes into the editor.

## Managing Tinycast's MCP servers from the terminal

`mcp-manager.py` doubles as a CLI for editing Tinycast's server list without the
Settings UI:

```bash
python3 mcp-manager.py list
python3 mcp-manager.py enable-mcp
python3 mcp-manager.py add-stdio tinycast "uv run --script /path/server.py" --trust always
python3 mcp-manager.py trust tinycast always
python3 mcp-manager.py remove tinycast
```

## Notes on the codec

Tinycast's snippet frontmatter parser is deliberately strict: only `name`, `keyword`,
`enabled`, `show_confirmation` (case-insensitive, no aliases), double-quoted strings
with exactly the `\\ \" \n \r \t` escapes, lowercase booleans. This server writes and
reads exactly that canonical form — files written here parse in the app, and files
written by the app parse here.

## Behavior and limits

- Deletes move files to `~/.Trash`. Nothing is hard-deleted.
- The Keychain is never touched; `mcpEnabled` (the consent switch) is never forced on.
- Tinycast reads its MCP server list at launch, so servers added via the
  self-management tools load after the app quits and reopens.
- Snippets hot-reload only while the Snippets feature is enabled in Settings
  (it ships off). Notes are live always.

## License

MIT — this project is a clean-room companion: it shares no code with Tinycast, only
its file formats. Tinycast itself is AGPL-3.0 by abue-ammar.