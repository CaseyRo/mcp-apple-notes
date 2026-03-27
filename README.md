# mcp-apple-notes

MCP server for Apple Notes on macOS. Creates notes via AppleScript.

## Install

```bash
pip install mcp-apple-notes
```

Or for development:

```bash
git clone https://github.com/CaseyRo/mcp-apple-notes.git
cd mcp-apple-notes
pip install -e .
```

## Configuration

Copy `.env.example` to `.env` and set your API key:

```bash
APPLE_NOTES_MCP_API_KEY=your-secret-key
```

Optional settings:

```bash
APPLE_NOTES_MCP_HOST=127.0.0.1   # default
APPLE_NOTES_MCP_PORT=8010         # default
```

## Run

```bash
mcp-apple-notes
```

The server starts on `http://127.0.0.1:8010/mcp` using streamable-http transport.

## Tool

### `create-note`

Create a new note in Apple Notes.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | str | required | The title of the note |
| `body` | str | required | The body content (supports HTML formatting) |
| `folder` | str | `"Notes"` | Target folder (created if it does not exist) |

**Returns:** `{ success: true, note_id: "...", title: "...", folder: "..." }`

## Client configuration

### Claude Code / n8n

Use `http://localhost:8010/mcp` with header `Authorization: Bearer <api-key>`.

## Requirements

- macOS with Apple Notes
- Python 3.11+

## License

MIT
