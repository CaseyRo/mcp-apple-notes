#!/usr/bin/env python3
"""
Apple Notes MCP Server.

Read, search, and create notes in Apple Notes via NoteStore SQLite
and AppleScript.
"""

import asyncio
import hmac
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier

from .config import get_settings
from .applescript_bridge import create_note, move_note, delete_note
from .notestore import NoteStoreReader

logger = logging.getLogger("mcp_apple_notes")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class BearerTokenVerifier(TokenVerifier):
    """Validates incoming requests against a static API key.

    Uses constant-time comparison to prevent timing attacks.
    """

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._api_key):
            logger.warning("Rejected request with invalid API key")
            return None

        return AccessToken(
            token=token,
            client_id="apple-notes-mcp-client",
            scopes=["all"],
        )


def _create_server() -> FastMCP:
    """Create and configure the FastMCP server instance."""
    settings = get_settings()

    kwargs: dict = {
        "name": "Apple Notes",
        "instructions": (
            "MCP server for Apple Notes on macOS. Reads come straight from "
            "NoteStore.sqlite (read-only, sub-100ms); writes go through "
            "AppleScript / Shortcuts.\n\n"
            "Orientation first — prefer the ambient resources over a tool call:\n"
            "  notes://stats    overview counts + notes-per-folder\n"
            "  notes://folders  the folder tree with note counts\n"
            "  notes://tags     every hashtag with usage counts\n\n"
            "Picking a tool:\n"
            "  - Browse: list_folders, then list_notes(folder=...) (paginated).\n"
            "  - Keyword lookup in titles/bodies: search_notes (FTS5, BM25-ranked).\n"
            "  - Hashtag lookup: search_by_tag (NOT search_notes — bodies don't\n"
            "    contain the literal #tag).\n"
            "  - Read one note in full: get_note(note_id) using a numeric note_id\n"
            "    from any list/search result.\n"
            "  - Create plain text: create_note. Create a rich recipe note with\n"
            "    image/video attachments: create_recipe_note.\n"
            "  - Reorganize: move_note. Remove: delete_note (-> Recently Deleted).\n\n"
            "All note_id values are the numeric Z_PK from NoteStore; identifiers "
            "are UUIDs used for applenotes:// deep-links. Read tools are "
            "side-effect-free; delete_note is the only destructive tool."
        ),
    }

    if settings.has_api_key:
        kwargs["auth"] = BearerTokenVerifier(settings.apple_notes_mcp_api_key.get_secret_value())
        logger.info("Bearer token authentication enabled")
    else:
        if settings.apple_notes_mcp_host not in ("127.0.0.1", "localhost", "::1"):
            logger.critical(
                "APPLE_NOTES_MCP_API_KEY is not set and host is %s — "
                "refusing to start an unauthenticated server on a non-loopback address. "
                "Set APPLE_NOTES_MCP_API_KEY or bind to 127.0.0.1.",
                settings.apple_notes_mcp_host,
            )
            sys.exit(1)
        logger.warning(
            "No APPLE_NOTES_MCP_API_KEY configured — server is running without "
            "authentication on loopback only"
        )

    return FastMCP(**kwargs)


mcp = _create_server()
_reader = NoteStoreReader(get_settings().db_path_resolved)


# --- Structured output models -------------------------------------------
# These mirror the dict shapes the NoteStoreReader already returns, so the
# top-level fields clients depend on are preserved while fastmcp now advertises
# an output schema. extra="allow" keeps the models forward-compatible if the
# reader gains fields. Optional fields default to None so partial reader rows
# (e.g. missing identifier on a stale FTS hit) never raise.


class _Model(BaseModel):
    model_config = {"extra": "allow"}


class FolderInfo(_Model):
    """A single Apple Notes folder with its note count and nesting path."""

    folder_id: int
    name: str
    path: str
    note_count: int
    identifier: str | None = None


class FolderList(_Model):
    """All non-trash folders in Apple Notes."""

    folders: list[FolderInfo]


class NoteSummary(_Model):
    """Lightweight note record (no body) as returned by listings."""

    note_id: int
    identifier: str | None = None
    title: str
    snippet: str = ""
    folder: str = ""
    created: str | None = None
    modified: str | None = None
    is_pinned: bool = False
    has_checklist: bool = False


class NoteList(_Model):
    """A paginated page of note summaries."""

    notes: list[NoteSummary]
    total: int
    limit: int
    offset: int


class NoteDetail(_Model):
    """A single note with its full extracted body and tags."""

    note_id: int
    identifier: str | None = None
    title: str
    body: str
    body_source: str
    folder: str = ""
    tags: list[str] = Field(default_factory=list)
    created: str | None = None
    modified: str | None = None
    is_pinned: bool = False
    has_checklist: bool = False


class TagInfo(_Model):
    """A hashtag with its usage count."""

    tag: str
    identifier: str | None = None
    note_count: int


class TagList(_Model):
    """All hashtags used across Apple Notes."""

    tags: list[TagInfo]
    total: int


class TagSearchResult(_Model):
    """Notes carrying a given hashtag."""

    results: list[NoteSummary]
    tag: str
    total: int


class SearchHit(NoteSummary):
    """A full-text search hit, adding the BM25 relevance rank."""

    rank: float | None = None


class SearchResult(_Model):
    """Ranked full-text search results for a query."""

    results: list[SearchHit]
    query: str


class FolderCount(_Model):
    """Per-folder note count used in stats."""

    folder: str
    count: int


class StatsResult(_Model):
    """Aggregate Apple Notes statistics and overview."""

    total_notes: int
    total_folders: int
    pinned_notes: int
    notes_with_checklists: int
    notes_per_folder: list[FolderCount]
    oldest_note: str | None = None
    newest_modification: str | None = None


class WriteResult(_Model):
    """Result of a create/move/delete operation.

    Preserves the historical ``{success, note_id, ...}`` envelope so existing
    clients and the Cloudflare portal keep working; ``url`` is the
    ``applenotes://`` deep-link when available.
    """

    success: bool
    note_id: int | str | None = None
    title: str | None = None
    folder: str | None = None
    url: str | None = None


# --- Health endpoint -----------------------------------------------------
from datetime import datetime, timezone as _tz  # noqa: E402
from starlette.requests import Request as _SReq  # noqa: E402
from starlette.responses import JSONResponse as _SResp  # noqa: E402

try:
    from mcp_apple_notes import __version__ as _version
except ImportError:
    _version = "0.1.0"

_start_time = datetime.now(_tz.utc)


@mcp.custom_route("/health", methods=["GET"])
async def _health(request: _SReq) -> _SResp:
    return _SResp({
        "status": "healthy",
        "service": "mcp-apple-notes",
        "version": _version,
        "upstream_reachable": True,
        "uptime_seconds": int((datetime.now(_tz.utc) - _start_time).total_seconds()),
    })


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request: _SReq) -> _SResp:
    return await _health(request)


@mcp.tool(
    name="create_note",
    title="Create Note",
    tags={"write"},
    annotations={
        "title": "Create Note",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def tool_create_note(
    title: str,
    body: str,
    folder: str = "Notes",
    ctx: Context | None = None,
) -> WriteResult:
    """[notes] Create a new note in Apple Notes (text only, no media).

    Disambiguation: For Apple Notes (personal/recipes) → apple-notes. For blog drafts/newsletters → writings.

    Args:
        title: The title of the note.
        body: The body content of the note. Can contain HTML for formatting.
        folder: The folder to place the note in. Created automatically if it
                does not exist. Defaults to "Notes".

    Returns:
        A WriteResult with success status, note_id, title, folder, and an
        applenotes:// deep-link url.
    """
    if ctx is not None:
        await ctx.info(f"Creating note {title!r} in folder {folder!r}")
    try:
        result = await asyncio.to_thread(create_note, title=title, body=body, folder=folder)
    except Exception as e:
        logger.exception("create_note failed")
        raise ToolError(f"Failed to create note {title!r}: {e}") from e
    return WriteResult(**result)


# ------------------------------------------------------------------
# Read tools (SQLite-backed)
# ------------------------------------------------------------------


@mcp.tool(
    name="list_folders",
    title="List Folders",
    tags={"read"},
    annotations={
        "title": "List Folders",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_list_folders() -> FolderList:
    """[notes] List all folders in Apple Notes with note counts.

    Returns:
        A FolderList; each folder has name, path, note_count, and identifier.
    """
    try:
        folders = await asyncio.to_thread(_reader.list_folders)
        return FolderList(folders=folders)
    except Exception as e:
        logger.exception("list_folders failed")
        raise ToolError(f"Failed to list folders: {e}") from e


@mcp.tool(
    name="list_notes",
    title="List Notes",
    tags={"read"},
    annotations={
        "title": "List Notes",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_list_notes(
    folder: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    sort_by: Literal["modified", "created", "title"] = "modified",
) -> NoteList:
    """[notes] List notes with pagination and optional folder filter.

    Args:
        folder: Filter by folder name. Omit to list all folders.
        limit: Maximum notes to return (default 50).
        offset: Number of notes to skip for pagination.
        sort_by: Sort order — "modified" (default), "created", or "title".

    Returns:
        A NoteList with notes (summaries), total count, limit, and offset.
    """
    try:
        result = await asyncio.to_thread(
            _reader.list_notes, folder=folder, limit=limit, offset=offset, sort_by=sort_by
        )
        return NoteList(**result)
    except Exception as e:
        logger.exception("list_notes failed")
        raise ToolError(f"Failed to list notes: {e}") from e


@mcp.tool(
    name="list_tags",
    title="List Tags",
    tags={"read"},
    annotations={
        "title": "List Tags",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_list_tags() -> TagList:
    """[notes] List all hashtags used in Apple Notes with usage counts.

    Returns:
        A TagList with all tags, each with tag name, identifier, and note_count.
    """
    try:
        tags = await asyncio.to_thread(_reader.list_tags)
        return TagList(tags=tags, total=len(tags))
    except Exception as e:
        logger.exception("list_tags failed")
        raise ToolError(f"Failed to list tags: {e}") from e


@mcp.tool(
    name="search_by_tag",
    title="Search by Tag",
    tags={"read"},
    annotations={
        "title": "Search by Tag",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_search_by_tag(
    tag: str, limit: Annotated[int, Field(ge=1, le=500)] = 50
) -> TagSearchResult:
    """[notes] Find all notes with a specific hashtag.

    For hashtag lookup, use this tool instead of search_notes (which searches body text only).

    Args:
        tag: The hashtag to search for (with or without leading #).
        limit: Maximum results to return (default 50).

    Returns:
        A TagSearchResult with matching note summaries.
    """
    try:
        results = await asyncio.to_thread(_reader.search_by_tag, tag=tag, limit=limit)
        return TagSearchResult(results=results, tag=tag, total=len(results))
    except Exception as e:
        logger.exception("search_by_tag failed")
        raise ToolError(f"Failed to search by tag {tag!r}: {e}") from e


@mcp.tool(
    name="get_note",
    title="Get Note",
    tags={"read"},
    annotations={
        "title": "Get Note",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_get_note(note_id: int) -> NoteDetail:
    """[notes] Get the full content of a note by its ID.

    The note_id is the numeric ID returned by list_notes or search_notes.
    Returns the note body as plain text (extracted from the internal format),
    with an AppleScript HTML-to-Markdown fallback for richer formatting.

    Args:
        note_id: The numeric note ID (Z_PK from list_notes results).

    Returns:
        A NoteDetail with the full body, tags, and metadata.
    """
    try:
        result = await asyncio.to_thread(_reader.get_note, note_id=note_id)
        return NoteDetail(**result)
    except ValueError as e:
        # Note not found — surface as an actionable tool error.
        raise ToolError(str(e)) from e
    except Exception as e:
        logger.exception("get_note failed")
        raise ToolError(f"Failed to get note {note_id}: {e}") from e


@mcp.tool(
    name="search_notes",
    title="Search Notes",
    tags={"read"},
    annotations={
        "title": "Search Notes",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_search_notes(
    query: str, limit: Annotated[int, Field(ge=1, le=100)] = 20
) -> SearchResult:
    """[notes] Full-text search across all Apple Notes.

    Searches note titles and body text using keyword matching.
    Results are ranked by relevance with snippet previews.
    For hashtag lookup, use search_by_tag instead.

    Args:
        query: Search query (keywords).
        limit: Maximum results to return (default 20).

    Returns:
        A SearchResult with ranked hits (BM25 rank) including snippets.
    """
    try:
        results = await asyncio.to_thread(_reader.search_notes, query=query, limit=limit)
        return SearchResult(results=results, query=query)
    except Exception as e:
        logger.exception("search_notes failed")
        raise ToolError(f"Failed to search notes for {query!r}: {e}") from e


@mcp.tool(
    name="get_stats",
    title="Get Stats",
    tags={"read"},
    annotations={
        "title": "Get Stats",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def tool_get_stats() -> StatsResult:
    """[notes] Get Apple Notes statistics and overview.

    Returns aggregate counts: total notes, folders, pinned notes,
    notes with checklists, notes per folder, and date range.

    Returns:
        A StatsResult with aggregate statistics.
    """
    try:
        stats = await asyncio.to_thread(_reader.get_stats)
        return StatsResult(**stats)
    except Exception as e:
        logger.exception("get_stats failed")
        raise ToolError(f"Failed to get stats: {e}") from e


# ------------------------------------------------------------------
# Management tools (AppleScript-backed)
# ------------------------------------------------------------------


@mcp.tool(
    name="move_note",
    title="Move Note",
    tags={"write"},
    annotations={
        "title": "Move Note",
        "readOnlyHint": False,
        "destructiveHint": False,
        # Re-running a move to the same folder is a no-op, so it is idempotent.
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def tool_move_note(
    note_id: int, folder: str, ctx: Context | None = None
) -> WriteResult:
    """[notes] Move a note to a different folder.

    Uses the note's numeric ID (returned by list_notes, get_note, or search_notes).
    Creates the target folder if it doesn't exist.

    Args:
        note_id: The numeric note ID (from note results).
        folder: Destination folder name.

    Returns:
        A WriteResult with success status, note_id, and target folder.
    """
    if ctx is not None:
        await ctx.info(f"Moving note {note_id} to folder {folder!r}")
    try:
        result = await asyncio.to_thread(move_note, note_id=note_id, target_folder=folder)
    except Exception as e:
        logger.exception("move_note failed")
        raise ToolError(f"Failed to move note {note_id} to {folder!r}: {e}") from e
    return WriteResult(**result)


@mcp.tool(
    name="delete_note",
    title="Delete Note",
    tags={"write", "destructive"},
    annotations={
        "title": "Delete Note",
        "readOnlyHint": False,
        "destructiveHint": True,
        # Notes app dedupes the move; a second delete just no-ops on the trash.
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def tool_delete_note(note_id: int, ctx: Context | None = None) -> WriteResult:
    """[notes] Delete a note (moves to Recently Deleted).

    Uses the note's numeric ID (returned by list_notes, get_note, or search_notes).
    Recoverable from Apple Notes' "Recently Deleted" folder for ~30 days.

    Args:
        note_id: The numeric note ID (from note results).

    Returns:
        A WriteResult with success status and note_id.
    """
    if ctx is not None:
        await ctx.info(f"Deleting note {note_id} (-> Recently Deleted)")
    try:
        result = await asyncio.to_thread(delete_note, note_id=note_id)
    except Exception as e:
        logger.exception("delete_note failed")
        raise ToolError(f"Failed to delete note {note_id}: {e}") from e
    return WriteResult(**result)


def _validate_url(url: str) -> None:
    """Validate that a URL uses an allowed scheme and host."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme!r}")
    hostname = (parsed.hostname or "").lower()
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or hostname.startswith(
        "169.254."
    ):
        raise ValueError("Disallowed URL host")


def _download_to_temp(url: str, suffix: str) -> str:
    """Download a URL to a temp file and return the local path.

    For Instagram/video URLs, uses yt-dlp with H.264 format preference.
    For regular HTTP URLs (images), uses urllib.
    """
    _validate_url(url)
    import tempfile

    media_dir = Path(tempfile.gettempdir()) / "sammler-notes-media"
    media_dir.mkdir(exist_ok=True)

    filename = f"media-{os.getpid()}-{id(url)}{suffix}"
    filepath = media_dir / filename

    # Detect if this is a video URL that needs yt-dlp
    is_video_url = any(x in url for x in ["instagram.com/reel", "instagram.com/p", "youtube.com", "tiktok.com"])

    if is_video_url and suffix in (".mp4", ".mov", ".webm"):
        logger.info("Downloading video via yt-dlp: %s", url[:80])
        result = subprocess.run(
            ["yt-dlp", "-o", str(filepath), "--force-overwrites",
             "--merge-output-format", "mp4", url],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error("yt-dlp failed: %s", result.stderr[:200])
            raise RuntimeError(f"yt-dlp download failed: {result.stderr[:100]}")

        # Check codec — Apple Notes needs H.264, yt-dlp may produce VP9
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode == 0 and "vp9" in probe.stdout.lower():
            logger.info("Converting VP9 to H.264 for Apple Notes compatibility")
            h264_path = filepath.with_suffix(".h264.mp4")
            conv = subprocess.run(
                ["ffmpeg", "-y", "-i", str(filepath), "-c:v", "libx264",
                 "-c:a", "aac", "-movflags", "+faststart", str(h264_path)],
                capture_output=True, text=True, timeout=120,
            )
            if conv.returncode == 0:
                os.unlink(str(filepath))
                os.rename(str(h264_path), str(filepath))
                logger.info("Converted to H.264: %d bytes", filepath.stat().st_size)
            else:
                logger.warning("H.264 conversion failed, using original: %s", conv.stderr[:100])
    else:
        import urllib.request
        logger.info("Downloading %s to %s", url[:80], filepath)
        urllib.request.urlretrieve(url, str(filepath))

    logger.info("Downloaded %d bytes", filepath.stat().st_size)
    return str(filepath)


def _start_media_server(serve_dir: str, port: int = 18765) -> subprocess.Popen:
    """Start a tiny HTTP server in serve_dir, returns the process."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", serve_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import time
    time.sleep(0.5)
    return proc


def _run_shortcut(payload: dict) -> dict:
    """Run the 'Sammler Recipe Note' macOS Shortcut with a JSON payload."""
    payload_json = json.dumps(payload)
    logger.info("Running Shortcut 'Sammler Recipe Note' for: %s", payload.get("title", "?"))

    result = subprocess.run(
        ["shortcuts", "run", "Sammler Recipe Note"],
        input=payload_json,
        capture_output=True,
        text=True,
        timeout=180,
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or "Unknown shortcut error"
        logger.error("Shortcut failed: %s", error_msg)
        return {"success": False, "error": error_msg}

    logger.info("Shortcut completed successfully")
    return {"success": True, "title": payload.get("title", "")}


@mcp.tool(
    name="create_recipe_note",
    title="Create Recipe Note",
    tags={"write"},
    annotations={
        "title": "Create Recipe Note",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        # Downloads remote media (yt-dlp/HTTP) and runs a macOS Shortcut.
        "openWorldHint": True,
    },
)
async def tool_create_recipe_note(
    title: str,
    body_html: str,
    image_url: str = "",
    video_url: str = "",
    ctx: Context | None = None,
) -> WriteResult:
    """[notes] Create a recipe note in Apple Notes with optional image and video.

    Uses the 'Sammler Recipe Note' macOS Shortcut to create a note with
    rich HTML content and media attachments. Media is downloaded from the
    provided URLs, served via a temporary local HTTP server, and the
    Shortcut fetches them using 'Get Contents of URL'.

    This is a multi-stage, potentially minutes-long job (download → optional
    ffmpeg transcode → Shortcut run); progress is reported via Context when one
    is available.

    Args:
        title: Recipe title (becomes the note name).
        body_html: HTML-formatted recipe body (ingredients, instructions, etc.).
        image_url: HTTP URL to the cover image (downloaded locally first). Empty to skip.
        video_url: HTTP URL to the video (must be H.264 MP4). Empty to skip.

    Returns:
        A WriteResult with success status, title, and an applenotes:// deep-link.
    """
    # Elicit on fully ambiguous input — a recipe note with no media is valid,
    # but it is worth confirming the caller really wants a text-only note when
    # both media URLs are empty (the common cause is a dropped/forgotten URL).
    if ctx is not None and not image_url and not video_url:
        try:
            answer = await ctx.elicit(
                "No image_url or video_url provided — create a text-only recipe "
                "note? Reply 'yes' to proceed, or provide a media URL.",
                response_type=str,
            )
            if getattr(answer, "action", "accept") == "decline":
                raise ToolError("Recipe note creation declined — no media supplied.")
        except ToolError:
            raise
        except Exception:
            # Client doesn't support elicitation — fall through and create the
            # text-only note rather than failing.
            logger.debug("Elicitation unavailable; proceeding with text-only recipe note")

    media_files: list[str] = []
    http_server = None
    total_stages = 1 + (1 if image_url else 0) + (1 if video_url else 0)
    stage = 0

    async def _progress(message: str) -> None:
        nonlocal stage
        stage += 1
        if ctx is not None:
            await ctx.info(message)
            await ctx.report_progress(progress=stage, total=total_stages)

    try:
        # Download media from remote URLs to local temp files
        if image_url:
            await _progress("Downloading cover image")
            img_path = await asyncio.to_thread(_download_to_temp, image_url, ".jpg")
            media_files.append(img_path)

        if video_url:
            await _progress("Downloading video (yt-dlp; may transcode VP9 -> H.264)")
            vid_path = await asyncio.to_thread(_download_to_temp, video_url, ".mp4")
            media_files.append(vid_path)

        # Build shortcut payload — only include keys when we have files
        payload: dict = {"title": title, "bodyHtml": body_html}

        if media_files:
            # Start temp HTTP server serving the download directory
            serve_dir = str(Path(media_files[0]).parent)
            http_server = await asyncio.to_thread(_start_media_server, serve_dir, 18765)

            if image_url:
                payload["imagePath"] = f"http://127.0.0.1:18765/{Path(media_files[0]).name}"
            if video_url:
                vid_file = media_files[-1]
                payload["videoPath"] = f"http://127.0.0.1:18765/{Path(vid_file).name}"

        await _progress("Running 'Sammler Recipe Note' Shortcut")
        result = await asyncio.to_thread(_run_shortcut, payload)
        if result.get("success"):
            # Augment the shortcut result with an applenotes:// deep-link.
            # _run_shortcut has no access to the note's ZIDENTIFIER; look it up
            # from NoteStore.sqlite now that the Shortcut has completed.
            from .applescript_bridge import _build_note_url
            url = await asyncio.to_thread(_build_note_url, title, "Recipes")
            result["url"] = url
        else:
            raise ToolError(
                f"Shortcut failed for recipe {title!r}: {result.get('error', 'unknown error')}"
            )
        return WriteResult(**result)

    finally:
        if http_server:
            http_server.terminate()
            http_server.wait(timeout=5)
        for f in media_files:
            try:
                os.unlink(f)
            except OSError:
                pass


# ------------------------------------------------------------------
# Resources — ambient reference data the client can pin as context
# instead of spending a tool round-trip on orientation queries.
# ------------------------------------------------------------------


@mcp.resource(
    "notes://stats",
    name="Apple Notes overview",
    title="Apple Notes overview",
    description="Aggregate counts (notes, folders, pinned, checklists), "
    "notes-per-folder, and date range. Same data as the get_stats tool, "
    "exposed as pinnable context.",
    mime_type="application/json",
    tags={"read"},
)
async def resource_stats() -> StatsResult:
    """Apple Notes aggregate statistics as a resource."""
    stats = await asyncio.to_thread(_reader.get_stats)
    return StatsResult(**stats)


@mcp.resource(
    "notes://folders",
    name="Apple Notes folders",
    title="Apple Notes folders",
    description="The folder tree with note counts and nesting paths. "
    "Same data as the list_folders tool.",
    mime_type="application/json",
    tags={"read"},
)
async def resource_folders() -> FolderList:
    """The Apple Notes folder tree as a resource."""
    folders = await asyncio.to_thread(_reader.list_folders)
    return FolderList(folders=folders)


@mcp.resource(
    "notes://tags",
    name="Apple Notes tags",
    title="Apple Notes tags",
    description="Every hashtag used across Apple Notes with usage counts. "
    "Same data as the list_tags tool — a cheap map of how the notes are "
    "organized before searching.",
    mime_type="application/json",
    tags={"read"},
)
async def resource_tags() -> TagList:
    """The Apple Notes hashtag taxonomy as a resource."""
    tags = await asyncio.to_thread(_reader.list_tags)
    return TagList(tags=tags, total=len(tags))


# ------------------------------------------------------------------
# Prompts — guided multi-step workflows for this server's signature jobs.
# ------------------------------------------------------------------


@mcp.prompt(
    name="capture_recipe",
    title="Capture a recipe into Apple Notes",
    tags={"write"},
)
def prompt_capture_recipe(
    source: str = "",
    title: str = "",
) -> str:
    """Guide the model through capturing a recipe (optionally from a URL/reel)
    into the Recipes folder with image and video attachments.

    Args:
        source: The recipe source — a URL (e.g. an Instagram reel) or pasted text.
        title: Optional desired note title; leave blank to derive one.
    """
    target = f"the source: {source}" if source else "the recipe the user provides"
    title_hint = (
        f'Use the title "{title}".'
        if title
        else "Derive a short, descriptive title from the recipe."
    )
    return (
        "You are capturing a recipe into Apple Notes using this server.\n\n"
        f"1. Gather the recipe from {target}. If it is a media URL "
        "(Instagram reel, YouTube, TikTok), keep the original URL — "
        "create_recipe_note can download the video and a cover image.\n"
        "2. Structure the body as clean HTML: an <h2> for Ingredients with a "
        "<ul> list, then an <h2> for Instructions with an <ol> list. Keep it "
        "concise and faithful to the source.\n"
        f"3. {title_hint}\n"
        "4. Call create_recipe_note with title, body_html, and (when available) "
        "image_url and/or video_url. The note lands in the Recipes folder and "
        "you get back an applenotes:// deep-link.\n"
        "5. Report the deep-link so the user can open the note."
    )


@mcp.prompt(
    name="triage_notes",
    title="Triage and organize loose notes",
    tags={"read"},
)
def prompt_triage_notes(folder: str = "Notes") -> str:
    """Guide the model through reviewing recent notes in a folder and proposing
    a tidy-up (move/merge/delete), confirming destructive steps first.

    Args:
        folder: The folder to triage (default "Notes" — the inbox).
    """
    return (
        f"Help the user triage the '{folder}' folder in Apple Notes.\n\n"
        "1. Read notes://stats and notes://folders for orientation (no tool call "
        "needed — they are resources).\n"
        f"2. Call list_notes(folder=\"{folder}\", sort_by=\"modified\") to see "
        "the most recently touched notes.\n"
        "3. For ambiguous items, call get_note(note_id) to read the full body "
        "before deciding.\n"
        "4. Propose a concrete plan: which notes to move (and to which folder), "
        "which to keep, and which look stale/duplicate.\n"
        "5. Apply moves with move_note. Only call delete_note after the user "
        "explicitly confirms — it is the one destructive tool (recoverable from "
        "Recently Deleted for ~30 days).\n"
        "6. Summarize what changed."
    )


def main():
    """Main entry point for the Apple Notes MCP Server."""
    settings = get_settings()
    host = settings.apple_notes_mcp_host
    port = settings.apple_notes_mcp_port

    logger.info("Starting Apple Notes MCP Server on %s:%d", host, port)

    try:
        # stateless_http=True per openspec mcp-stateless-transport — eliminates
        # orphaned SSE sessions after idle disconnects.
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            path="/mcp",
            stateless_http=True,
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        sys.exit(0)
    except Exception:
        logger.exception("Error running server")
        sys.exit(1)


if __name__ == "__main__":
    main()
