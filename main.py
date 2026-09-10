"""
NetCutter - Test Tool
----------------------
A tool to instantly cut and restore internet access for one or more
chosen applications (not your whole PC) with a global hotkey (keyboard
key OR mouse button). Built for testing an anti-lag-switch mechanic in
your own game while things like voice chat with your team keep
working normally.

3 modes:
  - Hold   : the target app(s)' connection stays cut while the
             key/button is held down, restored on release
  - Toggle : each press flips the state (cut / restore)
  - Timed  : a press cuts the target app(s) for X seconds, then they
             are restored automatically

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
import os
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

MOUSE_NAMES = {
    "left": "LEFT CLICK",
    "right": "RIGHT CLICK",
    "middle": "MIDDLE CLICK",
    "x": "MOUSE 4",
    "x2": "MOUSE 5",
}


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
        self.search_var = ctk.StringVar()
        self.is_cut = False
        self.capturing = False
        self._key_held = False  # guards against OS key-repeat
        self._last_press_time = 0.0  # guards against duplicate down events

        # path -> display name, for every app currently known (running
        # or manually browsed-to).
        self._app_map = {}
        # paths currently checked by the user (multi-select).
        self.selected_paths = set()
        # path -> (checkbox widget, BooleanVar)
        self._app_checkboxes = {}

        self._build_ui()
        self.overlay = Overlay(self)
        self._register_hotkey()
        self._refresh_app_list()

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
        ]:
            ctk.CTkRadioButton(
                mode_frame,
                text=text,
                variable=self.mode,
                value=value,
                command=self._register_hotkey,
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

        # Hotkey
        hk_frame = ctk.CTkFrame(content, fg_color=PANEL, corner_radius=10)
        hk_frame.pack(padx=20, pady=8, fill="x")
        ctk.CTkLabel(hk_frame, text="Hotkey (keyboard key or mouse button)").pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        self.hotkey_label = ctk.CTkLabel(
            hk_frame, text="F8", font=ctk.CTkFont(size=18, weight="bold")
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
        if self.hotkey["type"] == "keyboard":
            text = self.hotkey["value"].upper()
        else:
            text = MOUSE_NAMES.get(self.hotkey["value"], self.hotkey["value"].upper())
        self.hotkey_label.configure(text=text)

    # ---------------- Hotkey capture (keyboard + mouse) ----------------
    def _capture_hotkey(self):
        if self.capturing:
            return
        self.capturing = True
        self.hotkey_btn.configure(text="Press any key or mouse button...")

        hooks = {}

        def cleanup():
            try:
                keyboard.unhook(hooks["kb"])
            except Exception:
                pass
            try:
                mouse.unhook(hooks["mo"])
            except Exception:
                pass

        def finish():
            self.capturing = False
            cleanup()
            self.after(0, self._update_hotkey_label)
            self.after(0, lambda: self.hotkey_btn.configure(text="Change Hotkey"))
            self._register_hotkey()

        def on_key(event):
            if not self.capturing:
                return
            self.hotkey = {"type": "keyboard", "value": event.name}
            finish()

        def on_mouse(event):
            if not self.capturing:
                return
            if isinstance(event, mouse.ButtonEvent) and event.event_type == mouse.DOWN:
                self.hotkey = {"type": "mouse", "value": event.button}
                finish()

        hooks["kb"] = keyboard.on_press(on_key)
        hooks["mo"] = mouse.hook(on_mouse)

    # ---------------- Hotkey binding ----------------
    def _register_hotkey(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass
        self._key_held = False

        htype = self.hotkey["type"]
        val = self.hotkey["value"]

        if htype == "keyboard":
            keyboard.on_press_key(val, self._on_press)
            keyboard.on_release_key(val, self._on_release)
        else:  # mouse
            mouse.on_button(self._on_press, buttons=(val,), types=(mouse.DOWN,))
            mouse.on_button(self._on_release, buttons=(val,), types=(mouse.UP,))

    def _on_press(self, *_args):
        if self._key_held:
            return

        now = time.monotonic()
        if now - self._last_press_time < MIN_PRESS_INTERVAL:
            return
        self._last_press_time = now

        self._key_held = True

        mode = self.mode.get()
        if mode == "hold":
            self._cut()
        elif mode == "toggle":
            self._toggle()
        elif mode == "timed":
            self._timed_cut()

    def _on_release(self, *_args):
        self._key_held = False
        if self.mode.get() == "hold":
            self._restore()

    # ---------------- Network actions ----------------
    def _cut(self):
        if self.is_cut:
            return
        if not self.selected_paths:
            self.after(0, self._warn_no_app)
            return
        self.is_cut = True
        net.cut_apps(list(self.selected_paths))
        self.after(0, lambda: self._set_status(False))

    def _restore(self):
        if not self.is_cut:
            return
        self.is_cut = False
        net.restore_apps()
        self.after(0, lambda: self._set_status(True))

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
            self.after(0, self._warn_no_app)
            return
        try:
            dur = max(0.1, float(self.duration_var.get().replace(",", ".")))
        except ValueError:
            dur = 5.0
        self._cut()
        threading.Thread(target=self._timed_restore, args=(dur,), daemon=True).start()

    def _timed_restore(self, dur):
        time.sleep(dur)
        self._restore()

    def _set_status(self, connected: bool):
        if connected:
            self.status_label.configure(text="Status: CONNECTED", text_color=GREEN)
        else:
            self.status_label.configure(text="Status: CUT", text_color=RED)
        self.overlay.set_status(connected)

    def on_close(self):
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
