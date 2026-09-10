"""
network_control.py
Blocks/restores network access for one or more specific applications,
instead of cutting the whole system's internet connection - so things
like voice chat (Discord, Teams, etc.) and everything else on the PC
keep working while only the target app(s) are cut off.

Implementation: Windows Firewall rules scoped to each program's .exe
path, added/removed directly through the firewall's COM API
(HNetCfg.FwPolicy2 / HNetCfg.FWRule) instead of spawning the "netsh"
command-line tool. Talking to the firewall in-process avoids the
~50-150ms cost of launching a new netsh.exe (and the cmd.exe shell
wrapping it) on every single add/remove call, which is what made the
previous netsh-based version feel noticeably delayed instead of
instant - especially with multiple apps or rapid hotkey presses.
"""

import ctypes
import threading

try:
    import pythoncom
    import win32com.client
    _COM_AVAILABLE = True
except ImportError:
    _COM_AVAILABLE = False

RULE_OUT_PREFIX = "NetCutter_AppBlockOut"
RULE_IN_PREFIX = "NetCutter_AppBlockIn"

# NET_FW_RULE_DIRECTION_ / NET_FW_ACTION_ / NET_FW_PROFILE2_ constants
# from the Windows Firewall COM API (netfw.h).
NET_FW_RULE_DIR_IN = 1
NET_FW_RULE_DIR_OUT = 2
NET_FW_ACTION_BLOCK = 0
NET_FW_PROFILE2_ALL = 0x7FFFFFFF

# Names of the rules that are currently active, so restore_apps() can
# remove exactly what was added, regardless of what is selected in the
# UI at the time of restoring.
_active_rules = []

# COM objects are single-threaded-apartment by default: every OS
# thread that touches them must call CoInitialize() once for itself
# first. The hotkey callbacks (from the `keyboard`/`mouse` libraries)
# and the timed-restore thread all run on their own threads, so this
# is called defensively at the start of every public function here.
_com_ready = threading.local()


def _ensure_com():
    if not getattr(_com_ready, "done", False):
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
        _com_ready.done = True


def _get_policy():
    _ensure_com()
    return win32com.client.Dispatch("HNetCfg.FwPolicy2")


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _add_block_rule(policy, name: str, program_path: str, direction: int):
    rule = win32com.client.Dispatch("HNetCfg.FWRule")
    rule.Name = name
    rule.ApplicationName = program_path
    rule.Action = NET_FW_ACTION_BLOCK
    rule.Direction = direction
    rule.Enabled = True
    rule.Profiles = NET_FW_PROFILE2_ALL
    rule.EdgeTraversal = False
    policy.Rules.Add(rule)


def _remove_rule(policy, name: str):
    try:
        policy.Rules.Remove(name)
    except Exception:
        # Rule with this name doesn't exist - nothing to remove.
        pass


def cut_apps(program_paths):
    """Blocks all inbound and outbound traffic for each given .exe,
    as close to instantly as the Windows Firewall API allows."""
    global _active_rules
    if not _COM_AVAILABLE:
        return
    policy = _get_policy()
    _active_rules = []
    for i, path in enumerate(program_paths):
        out_name = f"{RULE_OUT_PREFIX}_{i}"
        in_name = f"{RULE_IN_PREFIX}_{i}"
        _add_block_rule(policy, out_name, path, NET_FW_RULE_DIR_OUT)
        _add_block_rule(policy, in_name, path, NET_FW_RULE_DIR_IN)
        _active_rules.append(out_name)
        _active_rules.append(in_name)


def restore_apps():
    """Removes the block rules for every app that is currently cut."""
    global _active_rules
    if not _COM_AVAILABLE:
        return
    policy = _get_policy()
    for name in _active_rules:
        _remove_rule(policy, name)
    _active_rules = []


def force_clean():
    """Removes any leftover rules when the app closes (safety net),
    including rule slots from a previous run that may not have been
    cleaned up (e.g. after a crash)."""
    if not _COM_AVAILABLE:
        return
    restore_apps()
    policy = _get_policy()
    for i in range(32):
        _remove_rule(policy, f"{RULE_OUT_PREFIX}_{i}")
        _remove_rule(policy, f"{RULE_IN_PREFIX}_{i}")
    # Legacy single-app rule names from older versions of this tool.
    _remove_rule(policy, RULE_OUT_PREFIX)
    _remove_rule(policy, RULE_IN_PREFIX)
