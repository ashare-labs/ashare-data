"""Recognize local filesystem locations in portable descriptive metadata."""

from pathlib import PurePosixPath, PureWindowsPath
import re


def contains_local_path(value: str) -> bool:
    """Detect absolute POSIX/Windows, home-relative and file-URI locations.

    Non-file URLs are valid provenance. Their path components are not local
    filesystem locations. Relative evidence locators remain portable.
    """
    if re.search(r"(?i)\bfile:", value):
        return True
    text = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+", "", value)
    for token in re.split(r"[\s\"'<>()[\]{},=]+", text):
        if not token or token == "/":
            continue
        if token.startswith("~") or PurePosixPath(token).is_absolute():
            return True
        windows = PureWindowsPath(token)
        if windows.drive or windows.is_absolute() or token.startswith("\\\\"):
            return True
    return False
