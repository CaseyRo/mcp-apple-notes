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

from pydantic import Field

from fastmcp import FastMCP
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
            "MCP server for Apple Notes on macOS. "
            "Read, search, and create notes. "
            "Use list_folders and list_notes to browse, get_note for full content, "
            "search_notes for keyword search, get_stats for overview. "
            "Create notes with create_note or create_recipe_note. "
            "Manage with move_note and delete_note."
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


@mcp.tool(name="create_note")
async def tool_create_note(
    title: str,
    body: str,
    folder: str = "Notes",
) -> dict:
    """[notes] Create a new note in Apple Notes (text only, no media).

    Disambiguation: For Apple Notes (personal/recipes) → apple-notes. For blog drafts/newsletters → writings.

    Args:
        title: The title of the note.
        body: The body content of the note. Can contain HTML for formatting.
        folder: The folder to place the note in. Created automatically if it
                does not exist. Defaults to "Notes".

    Returns:
        A dict with success status, note_id, title, and folder.
    """
    return await asyncio.to_thread(create_note, title=title, body=body, folder=folder)


# ------------------------------------------------------------------
# Read tools (SQLite-backed)
# ------------------------------------------------------------------


@mcp.tool(name="list_folders")
async def tool_list_folders() -> dict:
    """[notes] List all folders in Apple Notes with note counts.

    Returns:
        A dict with a list of folders, each containing name, path, and note_count.
    """
    try:
        folders = await asyncio.to_thread(_reader.list_folders)
        return {"success": True, "folders": folders}
    except Exception as e:
        logger.exception("list_folders failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="list_notes")
async def tool_list_notes(
    folder: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
    sort_by: Literal["modified", "created", "title"] = "modified",
) -> dict:
    """[notes] List notes with pagination and optional folder filter.

    Args:
        folder: Filter by folder name. Omit to list all folders.
        limit: Maximum notes to return (default 50).
        offset: Number of notes to skip for pagination.
        sort_by: Sort order — "modified" (default), "created", or "title".

    Returns:
        A dict with notes list, total count, limit, and offset.
    """
    try:
        result = await asyncio.to_thread(
            _reader.list_notes, folder=folder, limit=limit, offset=offset, sort_by=sort_by
        )
        return {"success": True, **result}
    except Exception as e:
        logger.exception("list_notes failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="list_tags")
async def tool_list_tags() -> dict:
    """[notes] List all hashtags used in Apple Notes with usage counts.

    Returns:
        A dict with a list of all tags, each with name and note_count.
    """
    try:
        tags = await asyncio.to_thread(_reader.list_tags)
        return {"success": True, "tags": tags, "total": len(tags)}
    except Exception as e:
        logger.exception("list_tags failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="search_by_tag")
async def tool_search_by_tag(tag: str, limit: Annotated[int, Field(ge=1, le=500)] = 50) -> dict:
    """[notes] Find all notes with a specific hashtag.

    For hashtag lookup, use this tool instead of search_notes (which searches body text only).

    Args:
        tag: The hashtag to search for (with or without leading #).
        limit: Maximum results to return (default 50).

    Returns:
        A dict with matching notes.
    """
    try:
        results = await asyncio.to_thread(_reader.search_by_tag, tag=tag, limit=limit)
        return {"success": True, "results": results, "tag": tag, "total": len(results)}
    except Exception as e:
        logger.exception("search_by_tag failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="get_note")
async def tool_get_note(note_id: int) -> dict:
    """[notes] Get the full content of a note by its ID.

    The note_id is the numeric ID returned by list_notes or search_notes.
    Returns the note body as plain text (extracted from the internal format),
    with an AppleScript HTML-to-Markdown fallback for richer formatting.

    Args:
        note_id: The numeric note ID (Z_PK from list_notes results).

    Returns:
        A dict with note content, metadata, and formatting.
    """
    try:
        result = await asyncio.to_thread(_reader.get_note, note_id=note_id)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("get_note failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="search_notes")
async def tool_search_notes(query: str, limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
    """[notes] Full-text search across all Apple Notes.

    Searches note titles and body text using keyword matching.
    Results are ranked by relevance with snippet previews.
    For hashtag lookup, use search_by_tag instead.

    Args:
        query: Search query (keywords).
        limit: Maximum results to return (default 20).

    Returns:
        A dict with ranked search results including snippets.
    """
    try:
        results = await asyncio.to_thread(_reader.search_notes, query=query, limit=limit)
        return {"success": True, "results": results, "query": query}
    except Exception as e:
        logger.exception("search_notes failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="get_stats")
async def tool_get_stats() -> dict:
    """[notes] Get Apple Notes statistics and overview.

    Returns aggregate counts: total notes, folders, pinned notes,
    notes with checklists, notes per folder, and date range.

    Returns:
        A dict with aggregate statistics.
    """
    try:
        stats = await asyncio.to_thread(_reader.get_stats)
        return {"success": True, **stats}
    except Exception as e:
        logger.exception("get_stats failed")
        return {"success": False, "error": str(e)}


# ------------------------------------------------------------------
# Management tools (AppleScript-backed)
# ------------------------------------------------------------------


@mcp.tool(name="move_note")
async def tool_move_note(note_id: int, folder: str) -> dict:
    """[notes] Move a note to a different folder.

    Uses the note's numeric ID (returned by list_notes, get_note, or search_notes).
    Creates the target folder if it doesn't exist.

    Args:
        note_id: The numeric note ID (from note results).
        folder: Destination folder name.

    Returns:
        A dict with success status.
    """
    try:
        return await asyncio.to_thread(move_note, note_id=note_id, target_folder=folder)
    except Exception as e:
        logger.exception("move_note failed")
        return {"success": False, "error": str(e)}


@mcp.tool(name="delete_note")
async def tool_delete_note(note_id: int) -> dict:
    """[notes] Delete a note (moves to Recently Deleted).

    Uses the note's numeric ID (returned by list_notes, get_note, or search_notes).

    Args:
        note_id: The numeric note ID (from note results).

    Returns:
        A dict with success status.
    """
    try:
        return await asyncio.to_thread(delete_note, note_id=note_id)
    except Exception as e:
        logger.exception("delete_note failed")
        return {"success": False, "error": str(e)}


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


@mcp.tool(name="create_recipe_note")
async def tool_create_recipe_note(
    title: str,
    body_html: str,
    image_url: str = "",
    video_url: str = "",
) -> dict:
    """[notes] Create a recipe note in Apple Notes with optional image and video.

    Uses the 'Sammler Recipe Note' macOS Shortcut to create a note with
    rich HTML content and media attachments. Media is downloaded from the
    provided URLs, served via a temporary local HTTP server, and the
    Shortcut fetches them using 'Get Contents of URL'.

    Args:
        title: Recipe title (becomes the note name).
        body_html: HTML-formatted recipe body (ingredients, instructions, etc.).
        image_url: HTTP URL to the cover image (downloaded locally first). Empty to skip.
        video_url: HTTP URL to the video (must be H.264 MP4). Empty to skip.

    Returns:
        A dict with success status and title.
    """
    media_files: list[str] = []
    http_server = None

    try:
        # Download media from remote URLs to local temp files
        if image_url:
            img_path = await asyncio.to_thread(_download_to_temp, image_url, ".jpg")
            media_files.append(img_path)

        if video_url:
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

        result = await asyncio.to_thread(_run_shortcut, payload)
        return result

    finally:
        if http_server:
            http_server.terminate()
            http_server.wait(timeout=5)
        for f in media_files:
            try:
                os.unlink(f)
            except OSError:
                pass


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
