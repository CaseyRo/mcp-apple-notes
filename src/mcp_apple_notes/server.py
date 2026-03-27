#!/usr/bin/env python3
"""
Apple Notes MCP Server.

A minimal MCP server that exposes a single tool for creating notes
in Apple Notes via AppleScript.
"""

import asyncio
import hmac
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

from .config import get_settings
from .applescript_bridge import create_note

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
            "Create notes in any folder using the create-note tool."
        ),
    }

    if settings.has_api_key:
        kwargs["auth"] = BearerTokenVerifier(settings.apple_notes_mcp_api_key)
        logger.info("Bearer token authentication enabled")
    else:
        logger.warning(
            "No APPLE_NOTES_MCP_API_KEY configured — server is running without authentication"
        )

    return FastMCP(**kwargs)


mcp = _create_server()


@mcp.tool(name="create-note")
async def tool_create_note(
    title: str,
    body: str,
    folder: str = "Notes",
) -> dict:
    """Create a new note in Apple Notes (text only, no media).

    Args:
        title: The title of the note.
        body: The body content of the note. Can contain HTML for formatting.
        folder: The folder to place the note in. Created automatically if it
                does not exist. Defaults to "Notes".

    Returns:
        A dict with success status, note_id, title, and folder.
    """
    return await asyncio.to_thread(create_note, title=title, body=body, folder=folder)


def _download_to_temp(url: str, suffix: str) -> str:
    """Download a URL to a temp file and return the local path.

    For Instagram/video URLs, uses yt-dlp with H.264 format preference.
    For regular HTTP URLs (images), uses urllib.
    """
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
        [sys.executable, "-m", "http.server", str(port), "--directory", serve_dir],
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


@mcp.tool(name="create-recipe-note")
async def tool_create_recipe_note(
    title: str,
    body_html: str,
    image_url: str = "",
    video_url: str = "",
) -> dict:
    """Create a recipe note in Apple Notes with optional image and video.

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
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            path="/mcp",
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        sys.exit(0)
    except Exception:
        logger.exception("Error running server")
        sys.exit(1)


if __name__ == "__main__":
    main()
