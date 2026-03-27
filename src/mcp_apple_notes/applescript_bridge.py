#!/usr/bin/env python3
"""AppleScript bridge for Apple Notes.

Provides functions to interact with Apple Notes via osascript.
"""

import re
import subprocess
import logging
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

    # Strip control characters that could break AppleScript structure
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", text)

    # Escape quotes by doubling them (AppleScript style)
    return cleaned.replace('"', '""')


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

    return {
        "success": True,
        "note_id": str(result),
        "title": title,
        "folder": folder,
    }
