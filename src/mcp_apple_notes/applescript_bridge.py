#!/usr/bin/env python3
"""AppleScript bridge for Apple Notes.

Provides functions to interact with Apple Notes via osascript.
"""

import re
import subprocess
import logging
import time
from typing import Union

logger = logging.getLogger(__name__)


def run_applescript(script: str) -> Union[str, bool]:
    """Run an AppleScript command and return the result.

    Args:
        script: The AppleScript code to execute

    Returns:
        The result of the AppleScript execution, or False if it failed
    """
    try:
        if "\n" in script:
            result = subprocess.run(
                ["osascript"], input=script, capture_output=True, text=True
            )
        else:
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True
            )

        if result.returncode != 0:
            stderr_output = result.stderr or ""
            logger.error(
                "AppleScript process returned error",
                extra={
                    "returncode": result.returncode,
                    "stderr_length": len(stderr_output),
                    "stderr": stderr_output,
                },
            )
            return False

        return result.stdout.strip()
    except Exception:
        logger.exception("Error running AppleScript")
        return False


def escape_applescript_string(text: str) -> str:
    """Escape special characters in an AppleScript string.

    Strips control characters that could break AppleScript structure,
    then escapes quotes by doubling them.

    Args:
        text: The string to escape

    Returns:
        The escaped string safe for interpolation into AppleScript string literals
    """
    if not text:
        return ""

    # Strip control characters and Unicode line terminators
    cleaned = re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", "", text)

    # Escape quotes by doubling them (AppleScript style)
    return cleaned.replace('"', '""')


def _build_note_url(title: str, folder: str | None = None) -> str:
    """Look up the ZIDENTIFIER of a newly-created note and build a deep-link.

    Retries for up to 3 seconds because Apple Notes may not flush the new row
    to NoteStore.sqlite immediately after the AppleScript/Shortcut create
    completes.

    Returns ``applenotes://showNote?identifier=<UUID>`` on success,
    or ``applenotes://`` if the identifier cannot be found (partial success).
    """
    from .config import get_settings
    from .notestore import NoteStoreReader

    reader = NoteStoreReader(get_settings().db_path_resolved)

    deadline = time.monotonic() + 3.0
    identifier: str | None = None
    while time.monotonic() < deadline:
        identifier = reader.find_note_identifier_by_title(title=title, folder=folder)
        if identifier:
            break
        time.sleep(0.25)

    if identifier:
        return f"applenotes://showNote?identifier={identifier}"

    logger.warning(
        "Could not find ZIDENTIFIER for note '%s' in folder '%s' within 3 s — "
        "returning bare applenotes:// URL",
        title,
        folder,
    )
    return "applenotes://"


def create_note(title: str, body: str, folder: str = "Notes") -> dict:
    """Create a note in Apple Notes.

    Args:
        title: The title of the note
        body: The body content of the note (can contain HTML for formatting)
        folder: The folder to create the note in (default: "Notes")

    Returns:
        dict with success status and note info

    Raises:
        RuntimeError: If the AppleScript execution fails
    """
    escaped_title = escape_applescript_string(title)
    escaped_body = escape_applescript_string(body)
    escaped_folder = escape_applescript_string(folder)

    # Build the HTML body with title as heading
    html_body = f"<h1>{escaped_title}</h1><br>{escaped_body}"

    script = f'''
tell application "Notes"
    -- Ensure the folder exists, create if it doesn't
    set targetFolder to missing value
    try
        set targetFolder to folder "{escaped_folder}" of default account
    end try
    if targetFolder is missing value then
        set targetFolder to make new folder at default account with properties {{name:"{escaped_folder}"}}
    end if

    -- Create the note in the target folder
    set newNote to make new note at targetFolder with properties {{name:"{escaped_title}", body:"{html_body}"}}

    -- Return the note ID
    return id of newNote
end tell
'''

    result = run_applescript(script)

    if result is False:
        raise RuntimeError(
            f"Failed to create note '{title}' in folder '{folder}'. "
            "Ensure Apple Notes is available and scripting is permitted."
        )

    # Parse integer note_id from CoreData URI (x-coredata://UUID/ICNote/pNNN)
    note_uri = str(result)
    note_id: int | str = note_uri
    if "/ICNote/p" in note_uri:
        note_id = int(note_uri.split("/ICNote/p")[1])

    # Build an applenotes://showNote?identifier=<UUID> deep-link by looking up
    # the ZIDENTIFIER from NoteStore.sqlite.  Apple Notes may not flush the row
    # to disk immediately, so we retry for up to 3 seconds.
    url = _build_note_url(title=title, folder=folder)

    return {
        "success": True,
        "note_id": note_id,
        "title": title,
        "folder": folder,
        "url": url,
    }


def _get_coredata_store_id() -> str:
    """Discover the CoreData store UUID from Apple Notes.

    The store ID is needed to construct note URIs for AppleScript.
    Format: x-coredata://<store-id>/ICNote/p<Z_PK>
    """
    result = run_applescript(
        'tell application "Notes" to get id of note 1'
    )
    if result is False:
        raise RuntimeError("Cannot determine CoreData store ID — is Notes running?")
    # Extract UUID from x-coredata://UUID/ICNote/pNNN
    parts = str(result).split("/")
    if len(parts) >= 3:
        return parts[2]
    raise RuntimeError(f"Unexpected note ID format: {result}")


# Cache the store ID after first lookup
_store_id_cache: str | None = None


def _note_uri(note_id: int) -> str:
    """Build a CoreData URI for a note by its Z_PK (SQLite primary key)."""
    global _store_id_cache
    if _store_id_cache is None:
        _store_id_cache = _get_coredata_store_id()
    return f"x-coredata://{_store_id_cache}/ICNote/p{note_id}"


def get_note_html(identifier: str) -> str:
    """Get the HTML body of a note by its ZIDENTIFIER.

    Falls back to searching by identifier if direct lookup fails.

    Args:
        identifier: The note's ZIDENTIFIER (UUID string).

    Returns:
        The HTML body string, or empty string on failure.
    """
    # Try to find the note by iterating (identifier is a UUID, not a CoreData URI)
    escaped_id = escape_applescript_string(identifier)
    script = f'''
tell application "Notes"
    repeat with n in every note
        set noteId to id of n
        if noteId contains "{escaped_id}" then
            return body of n
        end if
    end repeat
    return ""
end tell
'''
    result = run_applescript(script)
    if result is False:
        return ""
    return str(result)


def _invalidate_store_id_cache() -> None:
    """Clear the cached store ID, forcing re-discovery on next call."""
    global _store_id_cache
    _store_id_cache = None


def move_note(note_id: int, target_folder: str) -> dict:
    """Move a note to a different folder via AppleScript.

    Args:
        note_id: The note's Z_PK (numeric ID from SQLite).
        target_folder: Name of the destination folder (created if missing).

    Returns:
        dict with success status.
    """
    uri = _note_uri(note_id)
    escaped_folder = escape_applescript_string(target_folder)

    script = f'''
tell application "Notes"
    set targetFolder to missing value
    try
        set targetFolder to folder "{escaped_folder}" of default account
    end try
    if targetFolder is missing value then
        set targetFolder to make new folder at default account with properties {{name:"{escaped_folder}"}}
    end if

    set theNote to note id "{uri}"
    move theNote to targetFolder
    return "ok"
end tell
'''
    result = run_applescript(script)
    if result is False:
        _invalidate_store_id_cache()
        raise RuntimeError(
            f"Failed to move note {note_id} to folder '{target_folder}'. "
            "Note may not exist or Notes.app permissions may be missing."
        )
    return {"success": True, "note_id": note_id, "folder": target_folder}


def delete_note(note_id: int) -> dict:
    """Delete a note (move to Recently Deleted) via AppleScript.

    Args:
        note_id: The note's Z_PK (numeric ID from SQLite).

    Returns:
        dict with success status.
    """
    uri = _note_uri(note_id)

    script = f'''
tell application "Notes"
    set theNote to note id "{uri}"
    delete theNote
    return "ok"
end tell
'''
    result = run_applescript(script)
    if result is False:
        _invalidate_store_id_cache()
        raise RuntimeError(
            f"Failed to delete note {note_id}. "
            "Note may not exist or Notes.app permissions may be missing."
        )
    return {"success": True, "note_id": note_id}
