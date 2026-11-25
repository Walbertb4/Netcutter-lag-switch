"""
NetCutter - Test Tool
----------------------
A tool to instantly cut and restore internet access for one or more
chosen applications (not your whole PC) with a global hotkey (keyboard
key OR mouse button). Built for testing an anti-lag-switch mechanic in
your own game while things like voice chat with your team keep
working normally.

4 modes:
  - Hold   : the target app(s)' connection stays cut while the
             key/button is held down, restored on release
  - Toggle : each press flips the state (cut / restore)
  - Timed  : a press cuts the target app(s) for X seconds, then they
             are restored automatically
  - Cycle  : a press starts a repeating cut/restore loop with separate
             On/Off durations in milliseconds (e.g. cut for 3000ms,
             restored for 2000ms, repeating) - it keeps looping on its
             own until the hotkey is pressed again to stop it

The hotkey and all durations (Timed seconds, Cycle On/Off ms) are
saved automatically and reloaded the next time the app is launched,
so they don't need to be reconfigured every session.

Must be run as Administrator (required for Windows Firewall rules
and the global keyboard/mouse hook).

Reliability notes:
- Auto-repeat: holding a key/button down normally generates repeated
  "down" events from Windows. This app ignores repeats and only
  reacts to the first press and the final release.
- A 150ms minimum gap between accepted presses guards against
  drivers/software that occasionally send a duplicate down-event for
  a single physical click (common with some gaming mice).
- Elevation mismatch: if your game runs "as Administrator" but this
  tool does not (or vice versa), Windows will block the low-level
  hook from reaching the game due to UIPI. Run both at the same
  privilege level (both admin, ideally).
- Exclusive fullscreen: some games and anti-cheat systems block or
  swallow global input hooks by design, especially in true exclusive
  fullscreen. Switching the game to "Borderless" or "Windowed" mode
  usually fixes this.
"""

import ctypes
import json
import os
import queue
import threading
import time
from tkinter import filedialog, messagebox

import customtkinter as ctk
import keyboard
import mouse

import network_control as net
import process_utils

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

BG = "#0d0d0d"
PANEL = "#1a1a1a"
GREEN = "#3ddc84"
RED = "#ff5555"

# Extended window style constants (Windows) used to stop the overlay
# from ever taking keyboard/OS focus - see Overlay._prevent_activation.
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

# Minimum time between two accepted presses of the SAME hotkey.
MIN_PRESS_INTERVAL = 0.15

# How often the overlay's elapsed-time counter refreshes while a cut
# is active.
TIMER_TICK_MS = 100

# Where saved settings (hotkey + durations) live between sessions.
CONFIG_DIR = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"), "NetCutter"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

MOUSE_NAMES = {
    "right": "RIGHT CLICK",
    "middle": "MIDDLE CLICK",
    "x": "MOUSE 4",
    "x2": "MOUSE 5",
}

MOUSE_VKS = {
    "right": 0x02,
    "middle": 0x04,
    "x": 0x05,
    "x2": 0x06,
}
VK_TO_MOUSE = {vk: btn for btn, vk in MOUSE_VKS.items()}

_MODIFIER_VKS = {
    "shift": (0x10, 0xA0, 0xA1),
    "left shift": (0xA0,),
    "right shift": (0xA1,),
    "ctrl": (0x11, 0xA2, 0xA3),
    "left ctrl": (0xA2,),
    "right ctrl": (0xA3,),
    "alt": (0x12, 0xA4, 0xA5),
    "left alt": (0xA4,),
    "right alt": (0xA5,),
    "alt gr": (0xA5,),
}

# Candidate keyboard VKs for GetAsyncKeyState fallback capture (excludes
# mouse buttons 0x01-0x06, generic shift/ctrl/alt 0x10-0x12 in favor of
# sided 0xA0-0xA5, and packet/reserved codes).
_CAPTURE_KB_VKS = tuple(
    vk
    for vk in range(0x08, 0xFF)
    if vk not in (0x0A, 0x0B, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x5E, 0xE7)
    and not (0x3A <= vk <= 0x40)
    and not (0x88 <= vk <= 0x8F)
    and not (0x92 <= vk <= 0x9F)
    and not (0xB8 <= vk <= 0xB9)
    and not (0xC1 <= vk <= 0xDA)
)


def _official_vk_maps():
    vk_to_name = {0x20: "space", 0x0D: "enter", 0x1B: "esc"}
    name_to_vk = {"space": 0x20, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B}
    try:
        from keyboard import _winkeyboard

        for vk, info in _winkeyboard.official_virtual_keys.items():
            nm = info[0] if isinstance(info, tuple) else info
            if isinstance(nm, str):
                low = nm.lower()
                vk_to_name.setdefault(vk, low)
                name_to_vk.setdefault(low, vk)
    except Exception:
        pass
    name_to_vk.setdefault("left alt", 0xA4)
    name_to_vk.setdefault("right alt", 0xA5)
    vk_to_name[0xA4] = "left alt"
    vk_to_name[0xA5] = "right alt"
    return vk_to_name, name_to_vk


_VK_TO_NAME, _NAME_TO_VK = _official_vk_maps()


def _vk_to_key_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        if vk == 0x49 and (ctypes.windll.user32.VkKeyScanW(ord("ı")) & 0xFF) == 0x49:
            return "ı"
        return chr(vk).lower()
    ch = ctypes.windll.user32.MapVirtualKeyW(vk, 2) & 0xFFFF
    if ch == 0x0130:  # Turkish dotted capital İ -> 'i'
        return "i"
    if ch > 32 and not (ch & 0x8000):
        return chr(ch).lower()
    return _VK_TO_NAME.get(vk, f"vk_{vk}")


def _resolve_keyboard_hotkey(hk: dict):
    """Returns (normalized_name, tuple_of_vks, frozenset_of_scan_codes)."""
    user32 = ctypes.windll.user32
    raw = str(hk.get("value") or "")
    norm = raw.strip().lower()
    if raw == "İ":
        norm = "i"

    vks = []
    saved_vk = hk.get("vk")
    if isinstance(saved_vk, int) and 0 < saved_vk < 256:
        vks.append(saved_vk)

    if norm in _MODIFIER_VKS:
        for v in _MODIFIER_VKS[norm]:
            if v not in vks:
                vks.append(v)
    elif norm.startswith("vk_") and norm[3:].isdigit():
        v = int(norm[3:])
        if v not in vks:
            vks.append(v)
    else:
        if len(raw) == 1:
            for ch in (raw, norm, raw.upper()):
                if len(ch) == 1:
                    res = user32.VkKeyScanW(ord(ch))
                    if res != -1 and (res & 0xFF) and (res & 0xFF) not in vks:
                        vks.append(res & 0xFF)
            if not vks and ("a" <= norm <= "z" or "0" <= norm <= "9"):
                vks.append(ord(norm.upper()))
        if norm in _NAME_TO_VK and _NAME_TO_VK[norm] not in vks:
            vks.append(_NAME_TO_VK[norm])

    scans = set()
    saved_sc = hk.get("scan_code")
    if isinstance(saved_sc, int) and saved_sc > 0:
        scans.add(saved_sc)
    for vk in vks:
        sc = user32.MapVirtualKeyW(vk, 0)
        if sc > 0:
            scans.add(sc)
    if not scans and norm:
        try:
            scans.update(c for c in keyboard.key_to_scan_codes(norm, error_if_missing=False) if c > 0)
        except Exception:
            pass

    return norm, tuple(vks), frozenset(scans)


class Overlay(ctk.CTkToplevel):
    """Small always-on-top, draggable status badge shown over other windows.

    Shows the current CUT ON/OFF state plus a live elapsed-time counter
    that starts at 0 the moment a cut begins and stops the instant the
    connection is restored.

    Note: this can only appear above borderless/windowed applications.
    True exclusive fullscreen (DirectX exclusive mode) takes over the
    entire display and no overlay window from another process can be
    drawn on top of it - that is a Windows/GPU limitation, not
    something this app can bypass. Use Borderless or Windowed mode in
    your game if you want to see the overlay while playing.
    """

    def __init__(self, master):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.90)
        except Exception:
            pass
        self.configure(fg_color=BG)
        self.geometry("+40+40")

        self._container = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8)
        self._container.pack(padx=3, pady=3)

        self.label = ctk.CTkLabel(
            self._container,
            text="CUT: OFF",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=GREEN,
            fg_color=PANEL,
            padx=8,
            pady=10,
        )
        self.label.pack(side="left", padx=(12, 6), pady=4)

        self.timer_label = ctk.CTkLabel(
            self._container,
            text="0.0s",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#888888",
            fg_color=PANEL,
            padx=8,
            pady=10,
        )
        self.timer_label.pack(side="left", padx=(0, 12), pady=4)

        for widget in (self.label, self.timer_label, self._container):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._do_drag)
        self._drag = {"x": 0, "y": 0}

        self._timer_running = False
        self._start_time = 0.0
        self._tick_job = None

        # Stop this window from ever becoming the focused/active
        # window. Without this, an overrideredirect + topmost window
        # can silently steal OS keyboard focus on Windows, which is
        # the most common cause of "I can't type in a field anymore"
        # bugs with overlay widgets. Dragging still works because
        # mouse messages are delivered regardless of activation state.
        self.after(50, self._prevent_activation)

    def _prevent_activation(self):
        try:
            hwnd = self.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception:
            pass

    def _start_drag(self, event):
        self._drag["x"] = event.x_root - self.winfo_x()
        self._drag["y"] = event.y_root - self.winfo_y()

    def _do_drag(self, event):
        x = event.x_root - self._drag["x"]
        y = event.y_root - self._drag["y"]
        self.geometry(f"+{x}+{y}")

    # ---------------- Elapsed-time counter ----------------
    def _start_timer(self):
        self._start_time = time.monotonic()
        self._timer_running = True
        self.timer_label.configure(text="0.0s", text_color="#cccccc")
        self._schedule_tick()

    def _stop_timer(self):
        self._timer_running = False
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

    def _schedule_tick(self):
        self._tick_job = self.after(TIMER_TICK_MS, self._on_tick)

    def _on_tick(self):
        if not self._timer_running:
            return
        elapsed = time.monotonic() - self._start_time
        self.timer_label.configure(text=f"{elapsed:.1f}s")
        self._schedule_tick()

    def set_status(self, connected: bool):
        if connected:
            self.label.configure(text="CUT: OFF", text_color=GREEN)
            self._stop_timer()
        else:
            self.label.configure(text="CUT: ON", text_color=RED)
            self._start_timer()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("NetCutter - Test Tool")
        self.geometry("480x700")
        self.minsize(420, 400)
        self.resizable(True, True)
        self.configure(fg_color=BG)

        self.mode = ctk.StringVar(value="toggle")
        self.hotkey = {"type": "keyboard", "value": "f8"}
        self.duration_var = ctk.StringVar(value="5.0")
        self.cycle_on_var = ctk.StringVar(value="3000")
        self.cycle_off_var = ctk.StringVar(value="2000")
        self.search_var = ctk.StringVar()
        self.is_cut = False
        self.capturing = False
        self._key_held = False  # guards against OS key-repeat
        self._last_press_time = 0.0  # guards against duplicate down events
        self._press_lock = threading.Lock()
        self._ignore_until_release = False
        self._stop_poller = False
        self._hk_name = ""
        self._hk_vks = ()
        self._hk_scan_codes = frozenset()
        self._ui_queue = queue.SimpleQueue()

        # Cycle mode state: a background thread that keeps cutting and
        # restoring on a loop until stopped.
        self._cycle_running = False
        self._cycle_thread = None
        self._cycle_stop_event = threading.Event()

        # path -> display name, for every app currently known (running
        # or manually browsed-to).
        self._app_map = {}
        # paths currently checked by the user (multi-select).
        self.selected_paths = set()
        # path -> (checkbox widget, BooleanVar)
        self._app_checkboxes = {}
        # Entry widgets that should keep their focus highlight only
        # while actually clicked into - see _maybe_clear_focus.
        self._focusable_entries = []

        self._load_config()
        self._sync_vars()
        self.mode.trace_add("write", lambda *_a: self._sync_vars())
        self.duration_var.trace_add("write", lambda *_a: self._sync_vars())
        self.cycle_on_var.trace_add("write", lambda *_a: self._sync_vars())
        self.cycle_off_var.trace_add("write", lambda *_a: self._sync_vars())

        self._build_ui()
        self.overlay = Overlay(self)
        self._register_hotkey()
        threading.Thread(target=self._poll_hotkey_loop, daemon=True).start()
        self.after(30, self._drain_ui_queue)
        self._refresh_app_list()

    def _sync_vars(self):
        self._mode_val = self.mode.get()
        self._duration_str = self.duration_var.get()
        self._cycle_on_str = self.cycle_on_var.get()
        self._cycle_off_str = self.cycle_off_var.get()

    def _drain_ui_queue(self):
        if self._stop_poller:
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        self.after(30, self._drain_ui_queue)

    # ---------------- Saved settings ----------------
    def _load_config(self):
        """Loads a previously saved hotkey/mode/durations, if any,
        so the user doesn't have to reconfigure them every launch."""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        hk = data.get("hotkey")
        if (
            isinstance(hk, dict)
            and hk.get("type") in ("keyboard", "mouse")
            and hk.get("value")
            and not (hk.get("type") == "mouse" and hk.get("value") == "left")
        ):
            self.hotkey = {
                k: hk[k] for k in ("type", "value", "vk", "scan_code") if k in hk
            }

        mode = data.get("mode")
        if mode in ("hold", "toggle", "timed", "cycle"):
            self.mode.set(mode)

        if "duration_s" in data:
            self.duration_var.set(str(data["duration_s"]))
        if "cycle_on_ms" in data:
            self.cycle_on_var.set(str(data["cycle_on_ms"]))
        if "cycle_off_ms" in data:
            self.cycle_off_var.set(str(data["cycle_off_ms"]))

    def _save_config(self):
        data = {
            "hotkey": self.hotkey,
            "mode": getattr(self, "_mode_val", "toggle"),
            "duration_s": getattr(self, "_duration_str", "5.0"),
            "cycle_on_ms": getattr(self, "_cycle_on_str", "3000"),
            "cycle_off_ms": getattr(self, "_cycle_off_str", "2000"),
        }
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ---------------- UI ----------------
    def _build_ui(self):
        # Everything lives inside one scrollable container that fills
        # the whole window. This guarantees the bottom sections (Mode,
        # Hotkey, Status, ...) are always reachable - by scrolling -
        # even if the window is resized smaller than the full content,
        # instead of silently clipping them off.
        content = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        content.pack(fill="both", expand=True)
        self._content = content

        # Clicking anywhere outside a text entry removes keyboard focus
        # from it, so the entry's blue focus highlight doesn't stay
        # visually "stuck" open after the user has clicked elsewhere.
        self.bind_all("<Button-1>", self._maybe_clear_focus, add="+")

        ctk.CTkLabel(
            content, text="NetCutter", font=ctk.CTkFont(size=20, weight="bold")
        ).pack(pady=(20, 0))
        ctk.CTkLabel(
            content,
            text="Per-application connection cutter - test tool",
            font=ctk.CTkFont(size=12),
            text_color="#888888",
        ).pack(pady=(0, 15))

        # Target application(s)
        app_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        app_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(
            app_frame, text="Target Application(s)", font=ctk.CTkFont(weight="bold")
        ).pack(anchor="w", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            app_frame,
            text="Only the checked app(s) have their connection cut -\n"
            "everything else (voice chat, browser, etc.) keeps working.",
            font=ctk.CTkFont(size=10),
            text_color="#888888",
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        self.search_entry = ctk.CTkEntry(
            app_frame,
            textvariable=self.search_var,
            placeholder_text="Search running apps...",
        )
        self.search_entry.pack(padx=12, pady=(0, 8), fill="x")
        self.search_var.trace_add("write", lambda *_args: self._render_app_list())
        self._focusable_entries.append(self.search_entry)

        self.app_scroll = ctk.CTkScrollableFrame(
            app_frame, height=170, fg_color="#111111", corner_radius=8
        )
        self.app_scroll.pack(padx=12, pady=(0, 8), fill="x")

        btn_row = ctk.CTkFrame(app_frame, fg_color=PANEL)
        btn_row.pack(padx=12, pady=(0, 8), fill="x")
        ctk.CTkButton(
            btn_row, text="Refresh List", command=self._refresh_app_list, width=100
        ).pack(side="left", padx=(0, 6), expand=True, fill="x")
        ctk.CTkButton(
            btn_row, text="Browse for .exe...", command=self._browse_app, width=100
        ).pack(side="left", expand=True, fill="x")

        self.target_label = ctk.CTkLabel(
            app_frame,
            text="No application selected",
            font=ctk.CTkFont(size=11),
            text_color=RED,
            wraplength=380,
            justify="left",
        )
        self.target_label.pack(anchor="w", padx=12, pady=(0, 12))

        # Mode
        mode_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        mode_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(
            mode_frame, text="Mode", font=ctk.CTkFont(weight="bold")
        ).pack(anchor="w", padx=12, pady=(12, 4))

        for value, text in [
            ("hold", "Hold - cut while held down, restored on release"),
            ("toggle", "Toggle - each press flips the state"),
            ("timed", "Timed - a press cuts for X seconds, then restores"),
            ("cycle", "Cycle - a press starts an On/Off loop, press again to stop"),
        ]:
            ctk.CTkRadioButton(
                mode_frame,
                text=text,
                variable=self.mode,
                value=value,
                command=self._on_mode_change,
            ).pack(anchor="w", padx=12, pady=6)
        ctk.CTkFrame(mode_frame, height=8, fg_color=PANEL).pack()

        # Duration
        dur_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        dur_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(
            dur_frame, text="Duration (seconds) - Timed mode only"
        ).pack(anchor="w", padx=12, pady=(12, 4))
        self.duration_entry = ctk.CTkEntry(dur_frame, textvariable=self.duration_var)
        self.duration_entry.pack(padx=12, pady=(0, 12), fill="x")
        self.duration_entry.bind("<Return>", lambda e: self.focus_set())
        self.duration_entry.bind("<Escape>", lambda e: self.focus_set())
        self.duration_entry.bind("<FocusOut>", lambda e: self._save_config())
        self._focusable_entries.append(self.duration_entry)

        # Cycle settings
        cycle_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        cycle_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(
            cycle_frame,
            text="Cycle Durations (milliseconds) - Cycle mode only",
            font=ctk.CTkFont(weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            cycle_frame,
            text="Example: On = 3000, Off = 2000 -> cut for 3s, restored for 2s, repeating.",
            font=ctk.CTkFont(size=10),
            text_color="#888888",
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        cycle_row = ctk.CTkFrame(cycle_frame, fg_color=PANEL)
        cycle_row.pack(padx=12, pady=(0, 12), fill="x")

        on_col = ctk.CTkFrame(cycle_row, fg_color=PANEL)
        on_col.pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkLabel(on_col, text="On (ms)", font=ctk.CTkFont(size=11)).pack(
            anchor="w"
        )
        self.cycle_on_entry = ctk.CTkEntry(on_col, textvariable=self.cycle_on_var)
        self.cycle_on_entry.pack(fill="x", pady=(2, 0))
        self.cycle_on_entry.bind("<Return>", lambda e: self.focus_set())
        self.cycle_on_entry.bind("<Escape>", lambda e: self.focus_set())
        self.cycle_on_entry.bind("<FocusOut>", lambda e: self._save_config())
        self._focusable_entries.append(self.cycle_on_entry)

        off_col = ctk.CTkFrame(cycle_row, fg_color=PANEL)
        off_col.pack(side="left", expand=True, fill="x", padx=(6, 0))
        ctk.CTkLabel(off_col, text="Off (ms)", font=ctk.CTkFont(size=11)).pack(
            anchor="w"
        )
        self.cycle_off_entry = ctk.CTkEntry(off_col, textvariable=self.cycle_off_var)
        self.cycle_off_entry.pack(fill="x", pady=(2, 0))
        self.cycle_off_entry.bind("<Return>", lambda e: self.focus_set())
        self.cycle_off_entry.bind("<Escape>", lambda e: self.focus_set())
        self.cycle_off_entry.bind("<FocusOut>", lambda e: self._save_config())
        self._focusable_entries.append(self.cycle_off_entry)

        # Hotkey
        hk_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        hk_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(hk_frame, text="Hotkey (keyboard key or mouse button)").pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        self.hotkey_label = ctk.CTkLabel(
            hk_frame,
            text=self._hotkey_display_text(),
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.hotkey_label.pack(pady=4)
        self.hotkey_btn = ctk.CTkButton(
            hk_frame, text="Change Hotkey", command=self._capture_hotkey
        )
        self.hotkey_btn.pack(padx=12, pady=(4, 4), fill="x")
        ctk.CTkLabel(
            hk_frame,
            text="Click Change, then press any key OR mouse button (e.g. Mouse4/5)",
            font=ctk.CTkFont(size=10),
            text_color="#888888",
        ).pack(padx=12, pady=(0, 12))

        self.status_label = ctk.CTkLabel(
            content,
            text="Status: CONNECTED",
            text_color=GREEN,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.status_label.pack(pady=18)

        if not net.is_admin():
            ctk.CTkLabel(
                content,
                text="WARNING: Not running as Administrator.\nFirewall rules and the hotkey may not work correctly.",
                text_color=RED,
                justify="center",
            ).pack(pady=(0, 10))

    # ---------------- Target application(s) ----------------
    def _refresh_app_list(self):
        """Re-scans running processes (like Task Manager) and merges
        them with anything already known (e.g. manually browsed apps),
        then re-renders the checkbox list."""
        running = process_utils.list_running_apps()  # [(name, path), ...]
        combined = {path: name for name, path in running}
        for path, name in self._app_map.items():
            combined.setdefault(path, name)
        self._app_map = combined
        self._render_app_list()

    def _render_app_list(self):
        """Rebuilds the checkbox list from self._app_map, filtered by
        the search box, without re-scanning running processes. Checked
        state is preserved via self.selected_paths even for apps that
        are temporarily filtered out of view."""
        query = self.search_var.get().strip().lower()

        for widget in self.app_scroll.winfo_children():
            widget.destroy()
        self._app_checkboxes = {}

        items = sorted(self._app_map.items(), key=lambda kv: kv[1].lower())
        if query:
            items = [(path, name) for path, name in items if query in name.lower()]

        if not items:
            msg = "No matching apps" if query else "No running apps found"
            ctk.CTkLabel(
                self.app_scroll, text=msg, text_color="#888888"
            ).pack(anchor="w", padx=6, pady=4)
        else:
            for path, name in items:
                var = ctk.BooleanVar(value=path in self.selected_paths)
                cb = ctk.CTkCheckBox(
                    self.app_scroll,
                    text=name,
                    variable=var,
                    command=lambda p=path, v=var: self._on_app_toggle(p, v),
                )
                cb.pack(anchor="w", padx=6, pady=3, fill="x")
                self._app_checkboxes[path] = (cb, var)

        self._update_target_label()

    def _on_app_toggle(self, path, var):
        if var.get():
            self.selected_paths.add(path)
        else:
            self.selected_paths.discard(path)
        self._update_target_label()

    def _browse_app(self):
        path = filedialog.askopenfilename(
            title="Select the application to target",
            filetypes=[("Executable files", "*.exe"), ("All files", "*.*")],
        )
        if not path:
            return
        path = os.path.normpath(path)
        name = os.path.basename(path)
        self._app_map[path] = name
        self.selected_paths.add(path)
        self._render_app_list()

    def _update_target_label(self):
        if self.selected_paths:
            names = sorted(
                self._app_map.get(p, os.path.basename(p)) for p in self.selected_paths
            )
            self.target_label.configure(
                text=f"Selected ({len(names)}): " + ", ".join(names),
                text_color=GREEN,
            )
        else:
            self.target_label.configure(
                text="No application selected", text_color=RED
            )

    def _update_hotkey_label(self):
        self.hotkey_label.configure(text=self._hotkey_display_text())

    def _hotkey_display_text(self):
        if self.hotkey["type"] == "keyboard":
            return self.hotkey["value"].upper()
        return MOUSE_NAMES.get(self.hotkey["value"], self.hotkey["value"].upper())

    def _maybe_clear_focus(self, event):
        """Drops keyboard focus from a text entry once the user clicks
        anywhere outside of it, so its focus highlight doesn't stay on
        indefinitely."""
        try:
            widget_path = str(event.widget)
        except Exception:
            return
        for entry in self._focusable_entries:
            try:
                if widget_path.startswith(str(entry)):
                    return  # click landed inside one of our entries
            except Exception:
                continue
        try:
            self.focus_set()
        except Exception:
            pass

    def _on_mode_change(self):
        # Switching away from Cycle mode should stop any loop that is
        # currently running, instead of leaving it cutting the target
        # app(s) in the background indefinitely.
        if self.mode.get() != "cycle" and self._cycle_running:
            self._stop_cycle()
        self._register_hotkey()
        self._save_config()

    # ---------------- Hotkey capture (keyboard + mouse) ----------------
    def _capture_hotkey(self):
        if self.capturing:
            return
        self.capturing = True
        self.hotkey_btn.configure(text="Press any key or mouse button...")

        # Suspend active hotkey while capturing so pressing the old key
        # doesn't trigger a cut mid-capture.
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass
        with self._press_lock:
            self._hk_vks = ()
            self._key_held = False

        hooks = {}
        done_lock = threading.Lock()

        def cleanup():
            try:
                keyboard.unhook(hooks["kb"])
            except Exception:
                pass
            try:
                mouse.unhook(hooks["mo"])
            except Exception:
                pass

        def finish(new_hotkey):
            with done_lock:
                if not self.capturing:
                    return
                self.capturing = False
            self.hotkey = new_hotkey
            cleanup()
            self._register_hotkey(from_capture=True)
            self._save_config()
            self._ui_queue.put(self._update_hotkey_label)
            self._ui_queue.put(lambda: self.hotkey_btn.configure(text="Change Hotkey"))

        def on_key(event):
            if not self.capturing:
                return
            sc = getattr(event, "scan_code", 0) or 0
            vk = -sc if sc < 0 else (ctypes.windll.user32.MapVirtualKeyW(sc & 0xFF, 1) if sc > 0 else 0)
            name = getattr(event, "name", None)
            if not name or name == "unknown":
                if vk:
                    name = _vk_to_key_name(vk)
                else:
                    return
            hk = {"type": "keyboard", "value": name}
            if vk:
                hk["vk"] = int(vk)
            if sc > 0:
                hk["scan_code"] = int(sc)
            finish(hk)

        def on_mouse(event):
            if not self.capturing:
                return
            if isinstance(event, mouse.ButtonEvent) and event.event_type == mouse.DOWN:
                if event.button == mouse.LEFT or event.button not in MOUSE_NAMES:
                    return
                finish({"type": "mouse", "value": event.button})

        def poll_capture():
            user32 = ctypes.windll.user32
            all_vks = tuple(VK_TO_MOUSE.keys()) + _CAPTURE_KB_VKS
            initially_down = {vk for vk in all_vks if user32.GetAsyncKeyState(vk) & 0x8000}
            while self.capturing and not self._stop_poller:
                for vk in list(initially_down):
                    if not (user32.GetAsyncKeyState(vk) & 0x8000):
                        initially_down.discard(vk)
                for vk in all_vks:
                    if vk in initially_down:
                        continue
                    if user32.GetAsyncKeyState(vk) & 0x8000:
                        if vk in VK_TO_MOUSE:
                            finish({"type": "mouse", "value": VK_TO_MOUSE[vk]})
                        else:
                            sc = user32.MapVirtualKeyW(vk, 0)
                            hk = {"type": "keyboard", "value": _vk_to_key_name(vk), "vk": int(vk)}
                            if sc > 0:
                                hk["scan_code"] = int(sc)
                            finish(hk)
                        return
                time.sleep(0.01)

        try:
            hooks["kb"] = keyboard.on_press(on_key)
        except Exception:
            pass
        try:
            hooks["mo"] = mouse.hook(on_mouse)
        except Exception:
            pass
        threading.Thread(target=poll_capture, daemon=True).start()

    # ---------------- Hotkey binding ----------------
    def _register_hotkey(self, from_capture=False):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

        if self.hotkey.get("type") == "mouse" and self.hotkey.get("value") not in MOUSE_NAMES:
            self.hotkey = {"type": "keyboard", "value": "f8"}

        htype = self.hotkey["type"]
        val = self.hotkey["value"]

        if htype == "keyboard":
            norm, vks, scans = _resolve_keyboard_hotkey(self.hotkey)
        else:
            norm = val
            vk = MOUSE_VKS.get(val)
            vks = (vk,) if vk else ()
            scans = frozenset()

        user32 = ctypes.windll.user32
        currently_down = any(user32.GetAsyncKeyState(v) & 0x8000 for v in vks)

        with self._press_lock:
            self._key_held = False
            self._hk_name = norm
            self._hk_vks = vks
            self._hk_scan_codes = scans
            self._bind_gen = getattr(self, "_bind_gen", 0) + 1
            self._ignore_until_release = bool(currently_down)

        if htype == "keyboard":
            try:
                keyboard.hook(self._on_key_event)
            except Exception:
                pass
        else:  # mouse (right, middle, x, x2 - never left)
            try:
                mouse.on_button(self._on_press, buttons=(val,), types=(mouse.DOWN,))
                mouse.on_button(self._on_release, buttons=(val,), types=(mouse.UP,))
            except Exception:
                pass

    def _matches_keyboard_hotkey(self, event) -> bool:
        sc = getattr(event, "scan_code", 0) or 0
        if sc > 0 and sc in self._hk_scan_codes:
            return True
        if sc < 0 and (-sc) in self._hk_vks:
            return True
        name = getattr(event, "name", None)
        if name:
            low = "i" if name == "İ" else name.lower()
            if low == self._hk_name:
                return True
        return False

    def _on_key_event(self, event):
        if self.capturing or not self._matches_keyboard_hotkey(event):
            return
        if event.event_type == keyboard.KEY_DOWN:
            self._on_press()
        elif event.event_type == keyboard.KEY_UP:
            self._on_release()

    def _poll_hotkey_loop(self):
        """Fallback GetAsyncKeyState poller so hotkeys and macros keep
        working even if Windows drops the low-level hook or a game
        swallows WH_KEYBOARD_LL events."""
        user32 = ctypes.windll.user32
        was_down = False
        last_gen = -1
        while not self._stop_poller:
            try:
                if self.capturing:
                    was_down = False
                    time.sleep(0.015)
                    continue
                gen = getattr(self, "_bind_gen", 0)
                vks = self._hk_vks
                is_down = bool(vks) and any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)
                if gen != last_gen:
                    last_gen = gen
                    was_down = is_down
                    if not is_down and self._ignore_until_release:
                        with self._press_lock:
                            self._ignore_until_release = False
                elif vks:
                    if is_down and not was_down:
                        was_down = True
                        self._on_press()
                    elif not is_down and was_down:
                        was_down = False
                        self._on_release()
                    elif not is_down and self._ignore_until_release:
                        with self._press_lock:
                            self._ignore_until_release = False
            except Exception:
                pass
            time.sleep(0.01)

    def _on_press(self, *_args):
        with self._press_lock:
            if self._ignore_until_release or self._key_held:
                return

            now = time.monotonic()
            if now - self._last_press_time < MIN_PRESS_INTERVAL:
                return
            self._last_press_time = now
            self._key_held = True

        mode = self._mode_val
        if mode == "hold":
            self._cut()
        elif mode == "toggle":
            self._toggle()
        elif mode == "timed":
            self._timed_cut()
        elif mode == "cycle":
            self._toggle_cycle()

    def _on_release(self, *_args):
        with self._press_lock:
            if self._ignore_until_release:
                self._ignore_until_release = False
                return
            if not self._key_held:
                return
            self._key_held = False
        if self._mode_val == "hold":
            self._restore()

    # ---------------- Network actions ----------------
    def _cut(self):
        if self.is_cut:
            return
        if not self.selected_paths:
            self._ui_queue.put(self._warn_no_app)
            return
        self.is_cut = True
        try:
            net.cut_apps(list(self.selected_paths))
        except Exception as exc:
            self.is_cut = False
            msg = str(exc)
            self._ui_queue.put(lambda m=msg: messagebox.showerror("Firewall Error", m))
            return
        self._ui_queue.put(lambda: self._set_status(False))

    def _restore(self):
        if not self.is_cut:
            return
        self.is_cut = False
        try:
            net.restore_apps()
        except Exception:
            pass
        self._ui_queue.put(lambda: self._set_status(True))

    def _warn_no_app(self):
        messagebox.showwarning(
            "No Application Selected",
            "Please select at least one target application before using the hotkey.",
        )

    def _toggle(self):
        self._restore() if self.is_cut else self._cut()

    def _timed_cut(self):
        if self.is_cut:
            return
        if not self.selected_paths:
            self._ui_queue.put(self._warn_no_app)
            return
        try:
            dur = max(0.1, float(self._duration_str.replace(",", ".")))
        except ValueError:
            dur = 5.0
        self._cut()
        threading.Thread(target=self._timed_restore, args=(dur,), daemon=True).start()

    def _timed_restore(self, dur):
        time.sleep(dur)
        self._restore()

    # ---------------- Cycle mode (repeating On/Off loop) ----------------
    def _toggle_cycle(self):
        if self._cycle_running:
            self._stop_cycle()
        else:
            self._start_cycle()

    def _start_cycle(self):
        if self._cycle_running:
            return
        if not self.selected_paths:
            self._ui_queue.put(self._warn_no_app)
            return

        try:
            on_ms = max(1, int(float(self._cycle_on_str.replace(",", "."))))
        except ValueError:
            on_ms = 3000
        try:
            off_ms = max(1, int(float(self._cycle_off_str.replace(",", "."))))
        except ValueError:
            off_ms = 2000

        self._cycle_stop_event.clear()
        self._cycle_running = True
        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, args=(on_ms / 1000.0, off_ms / 1000.0), daemon=True
        )
        self._cycle_thread.start()

    def _stop_cycle(self):
        if not self._cycle_running:
            return
        self._cycle_running = False
        self._cycle_stop_event.set()

    def _cycle_loop(self, on_s, off_s):
        # Runs on a background thread: alternately cuts for on_s
        # seconds and restores for off_s seconds until _stop_cycle()
        # sets the stop event. Event.wait() is used instead of
        # time.sleep() so a stop request is picked up immediately
        # rather than after the current phase finishes.
        while not self._cycle_stop_event.is_set():
            self._cut()
            if self._cycle_stop_event.wait(on_s):
                break
            self._restore()
            if self._cycle_stop_event.wait(off_s):
                break
        self._restore()
        self._cycle_running = False

    def _set_status(self, connected: bool):
        if connected:
            self.status_label.configure(text="Status: CONNECTED", text_color=GREEN)
        else:
            self.status_label.configure(text="Status: CUT", text_color=RED)
        self.overlay.set_status(connected)

    def on_close(self):
        self._stop_poller = True
        self._stop_cycle()
        self._save_config()
        net.force_clean()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass
        try:
            self.overlay.destroy()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
