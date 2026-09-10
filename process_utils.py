"""
process_utils.py
Helpers for listing currently running applications so the user can
pick which one NetCutter should block, instead of blocking the
whole system's internet connection.
"""

import os

try:
    import psutil
except ImportError:  # psutil is listed in requirements.txt
    psutil = None


def list_running_apps():
    """Returns a list of (display_name, full_exe_path) tuples for
    currently running processes that have a real, readable exe path
    on disk, deduplicated by path and sorted by name."""
    if psutil is None:
        return []

    seen = {}
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            exe = proc.info.get("exe")
            name = proc.info.get("name")
            if not exe or not name:
                continue
            if not os.path.isfile(exe):
                continue
            seen[exe] = name
        except Exception:
            # Some system/protected processes refuse to report their
            # path even when running as Administrator - just skip them.
            continue

    return sorted(
        [(name, path) for path, name in seen.items()],
        key=lambda item: item[0].lower(),
    )
