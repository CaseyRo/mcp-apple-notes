# mcp-apple-notes

An MCP server that gives AI assistants access to Apple Notes on a Mac. Reads and searches go straight to the local `NoteStore.sqlite` database (read-only, fast); creating, moving and deleting notes goes through AppleScript. It is for people who keep their notes in Apple Notes and want Claude, or any other MCP client, to find, read and file them.

## Requirements

- macOS with Apple Notes
- Python 3.11 or newer (the repo pins 3.12 in `.python-version`) and [uv](https://docs.astral.sh/uv/)
- FastMCP 4 (`fastmcp>=4.0.10,<5.0.0`, installed by `uv sync`)
- Full Disk Access for the Python interpreter that runs the server (to read `NoteStore.sqlite`)
- Automation permission for Notes.app (for the AppleScript write tools)
- For `create_recipe_note` only: a macOS Shortcut named "Sammler Recipe Note" that accepts a JSON payload

## Install

Install from source. The `mcp-apple-notes` package on PyPI is a stale 0.1.2; releases are git tags only.

```bash
git clone https://github.com/CaseyRo/mcp-apple-notes.git
cd mcp-apple-notes
uv sync
cp .env.example .env   # then set APPLE_NOTES_MCP_API_KEY
```

## Run

```bash
uv run mcp-apple-notes
```

The server listens on `http://127.0.0.1:8010/mcp` (streamable HTTP, stateless). `GET /health` and `GET /healthz` return a small JSON status document without authentication.

### Run under launchd

The server has to run as a native macOS process (not in Docker) because it needs the Notes database and AppleScript. To keep it running, add a LaunchAgent such as `~/Library/LaunchAgents/com.example.mcp-apple-notes.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.mcp-apple-notes</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/mcp-apple-notes/.venv/bin/python</string>
    <string>-m</string><string>mcp_apple_notes</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/mcp-apple-notes</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

Load it with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.mcp-apple-notes.plist`. The working directory matters: settings are read from `.env` there. Grant Full Disk Access and Notes automation to that exact `.venv/bin/python`; macOS keys the grants to the interpreter binary.

## Configuration

Settings come from environment variables or a `.env` file in the working directory.

| Variable | Default | Purpose |
|----------|---------|---------|
| `APPLE_NOTES_MCP_API_KEY` | empty | Bearer token clients must send. Empty disables auth (loopback only). |
| `APPLE_NOTES_MCP_HOST` | `127.0.0.1` | Bind address. |
| `APPLE_NOTES_MCP_PORT` | `8010` | Listen port. |
| `APPLE_NOTES_DB_PATH` | `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` | Notes database to read. |

## Authentication

When `APPLE_NOTES_MCP_API_KEY` is set, every MCP request needs the header `Authorization: Bearer <key>`; the key is compared in constant time. Without a key the server runs unauthenticated, and it refuses to start if the host is anything other than a loopback address. To reach it from another machine, keep it on loopback and put an authenticating tunnel or reverse proxy in front of it.

## Tools

### Read tools (SQLite-backed, <100ms)

These tools query `NoteStore.sqlite` directly for fast, read-only access.

#### `list_folders`

List all folders with note counts and nested paths.

**Returns:** `{ success, folders: [{ folder_id, name, path, note_count, identifier }] }`

#### `list_notes`

List notes with pagination and optional folder filter.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `folder` | str | `null` | Filter by folder name |
| `limit` | int | `50` | Max notes to return |
| `offset` | int | `0` | Pagination offset |
| `sort_by` | str | `"modified"` | Sort by `"modified"`, `"created"`, or `"title"` |

**Returns:** `{ success, notes: [{ note_id, identifier, title, snippet, folder, created, modified, is_pinned, has_checklist }], total, limit, offset }`

#### `get_note`

Get full content of a note by its numeric ID. Body is extracted from the internal protobuf format with an AppleScript HTML-to-Markdown fallback. Includes tags if present.

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | int | The numeric note ID from list/search results |

**Returns:** `{ success, note_id, identifier, title, body, folder, tags, created, modified, is_pinned, has_checklist }`

#### `search_notes`

Full-text keyword search across note titles and bodies using SQLite FTS5 with BM25 ranking. The FTS index is built in-memory on first search and rebuilt automatically when `NoteStore.sqlite` changes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | required | Search keywords |
| `limit` | int | `20` | Max results |

**Returns:** `{ success, results: [{ note_id, identifier, title, snippet, folder, rank, created, modified, is_pinned }], query }`

#### `get_stats`

Aggregate statistics: total notes, folders, pinned count, checklist count, notes per folder, date range.

**Returns:** `{ success, total_notes, total_folders, pinned_notes, notes_with_checklists, notes_per_folder, oldest_note, newest_modification }`

### Tag tools (SQLite-backed)

Tags in Apple Notes are stored as `ICInlineAttachment` entities in the protobuf layer, linked to notes via the `ZNOTE1` foreign key. These tools provide read-only access to tag data.

#### `list_tags`

List all hashtags with usage counts.

**Returns:** `{ success, tags: [{ tag, identifier, note_count }], total }`

#### `search_by_tag`

Find all notes with a specific hashtag.

| Parameter | Type | Description |
|-----------|------|-------------|
| `tag` | str | Hashtag to search for (with or without `#`) |

**Returns:** `{ success, results: [{ note_id, identifier, title, snippet, folder, created, modified, is_pinned }], tag, total }`

### Write tools (AppleScript-backed)

#### `create_note`

Create a new note in Apple Notes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | str | required | The title of the note |
| `body` | str | required | The body content (supports HTML formatting) |
| `folder` | str | `"Notes"` | Target folder (created if it does not exist) |

**Returns:** `{ success, note_id, title, folder }`

#### `create_recipe_note`

Create a recipe note with optional image and video via the "Sammler Recipe Note" macOS Shortcut.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | str | required | Recipe title |
| `body_html` | str | required | HTML-formatted recipe body |
| `image_url` | str | `""` | URL to cover image |
| `video_url` | str | `""` | URL to video (H.264 MP4) |

**Returns:** `{ success, title }`

### Management tools (AppleScript-backed)

#### `move_note`

Move a note to a different folder. Creates the folder if it doesn't exist.

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | int | The numeric note ID |
| `folder` | str | Destination folder name |

**Returns:** `{ success, note_id, folder }`

#### `delete_note`

Move a note to Recently Deleted.

| Parameter | Type | Description |
|-----------|------|-------------|
| `note_id` | int | The numeric note ID |

**Returns:** `{ success, note_id }`

## Architecture

```
                         server.py (FastMCP)
                 ┌───────────┼───────────────┐
                 │           │               │
          notestore.py   applescript      shortcuts
          (SQLite read)  _bridge.py       (recipe)
                 │           │
          ┌──────┴──────┐   ├── create_note()
          │ NoteStore   │   ├── get_note_html()
          │ Reader      │   ├── move_note()
          │             │   └── delete_note()
          ├─ list_folders()
          ├─ list_notes()
          ├─ get_note()  ──fallback──► get_note_html()
          ├─ search_notes()
          ├─ get_stats()
          ├─ list_tags()
          ├─ get_note_tags()
          ├─ search_by_tag()
          │
          └─ FTS5 index (:memory:, rebuilt on mtime change)
```

All read tools query `NoteStore.sqlite` directly in read-only mode (`?mode=ro`). Write and management tools use AppleScript subprocess calls. The `create_recipe_note` tool uses the macOS Shortcuts framework.

### NoteStore.sqlite

All Apple Notes data lives in a single SQLite database at:

```
~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite
```

Key schema details:

- All entities share one table (`ZICCLOUDSYNCINGOBJECT`), differentiated by `Z_ENT`
- Notes = `Z_ENT=12`, Folders = `Z_ENT=15`, Accounts = `Z_ENT=14`, Hashtags = `Z_ENT=8`
- Tags are `ICInlineAttachment` entities (`Z_ENT=9`) with type `com.apple.notes.inlinetextattachment.hashtag`, linked to notes via `ZNOTE1`
- Note body is a gzipped protobuf blob in `ZICNOTEDATA.ZDATA`
- Timestamps use Core Data epoch (seconds since 2001-01-01; add 978307200 for Unix)

## Limitations

### Tag writes are not supported

Tags in Apple Notes are stored as inline attachment entities in the protobuf layer, not in the HTML body. This means:

- **AppleScript `set body`** with `#hashtag` text creates plain text, not a real tag
- **Direct SQLite writes** would corrupt iCloud sync state
- **System Events keystrokes** require accessibility permissions not available to a server process

The only viable path for programmatic tag creation is a custom macOS Shortcut using the native "Add Tags to Notes" action, called via `shortcuts run`. This is not yet implemented.

### Notes are read-only at the body level

The server can read note content but cannot update existing note bodies. Apple Notes uses an undocumented protobuf format (CRDT-based `ZMERGEABLEDATA`) for the canonical note body. AppleScript's `body` property returns a simplified HTML rendering and setting it overwrites formatting.

### Checklist state is not parsed

Notes with checklists are flagged (`has_checklist: true`) but individual checklist item states (done/undone) are not extracted. The checklist state lives in protobuf fields that require deep format understanding.

### Attachment content is not extracted

Attachment metadata (that they exist) is visible, but file contents, thumbnails, and inline image data are not surfaced through the tools.

## Resources and prompts

- Resources: `notes://stats`, `notes://folders`, `notes://tags` (the same data as `get_stats`, `list_folders` and `list_tags`).
- Prompts: `capture_recipe`, `triage_notes`.

## Usage telemetry

A small middleware (`src/mcp_apple_notes/usage.py`) writes one JSON line per tool call to stderr: server name, tool name, duration, outcome and MCP protocol version. It never records arguments or results. Tool failures raise `ToolError`, so they are logged with `outcome: error`.

## Development

```bash
uv sync
uv run pytest
```

The tests use synthetic SQLite fixtures and a patched reader, so they run on Linux without Notes. CI (`.github/workflows/ci.yml`) runs the same suite as the `test` check, which is required before a pull request can merge into `main`.

## Releases

Releases are git tags only. After a change lands on `main`, the release workflow runs the tests and a `pip-audit`, then pushes the next `v*` patch tag. No commit bumps the version in `pyproject.toml`.

## Credits

The read-via-SQLite approach was inspired by research into several community Apple Notes MCP servers:

- **[ailenshen/apple-notes-mcp](https://github.com/AilensHe/apple-notes-mcp)**: SQLite-first reads with HTML-to-Markdown conversion. Directly inspired the `NoteStoreReader` architecture and the protobuf text extraction approach.
- **[sweetrb/apple-notes-mcp](https://github.com/sweetrb/apple-notes-mcp)**: Full CRUD with checklist state parsing from SQLite. Informed the tag entity discovery and stats tool design.
- **[disco-trooper/apple-notes-mcp](https://github.com/nicholasgasior/apple-notes-mcp)**: Hybrid vector + FTS search with BM25 ranking. Inspired the FTS5 in-memory index approach (without the embedding model overhead).
- **[sirmews/apple-notes-mcp](https://github.com/sirmews/apple-notes-mcp)**: Original Python SQLite reader that proved the approach was viable.

NoteStore.sqlite schema documentation from [Swift Forensics](http://www.swiftforensics.com/2018/02/reading-notes-database-on-macos.html) and [Simon Willison's analysis](https://simonwillison.net/2021/Dec/9/notes-on-notesapp/).

## Support

If this server saves you time, you can [buy me a coffee](https://buymeacoffee.com/caseyberlin).

## License

MIT, see [LICENSE](LICENSE).
