#!/usr/bin/env python3
"""
BlueOrbit Tracker
=================
Monitors keyboard and mouse activity, then sends one heartbeat tick
to the BlueOrbit server every minute.

  Status = "working"  — if there was any keyboard/mouse activity
                         within the last IDLE_THRESHOLD seconds.
  Status = "idle"     — if no activity for IDLE_THRESHOLD seconds.

All dates/times are recorded in Japan Standard Time (JST, UTC+9).
"""

import ctypes
import ctypes.wintypes
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta, date as _date

import subprocess

import requests

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except ImportError:
    TRAY_OK = False

# ── Constants ────────────────────────────────────────────────────────
TICK_INTERVAL  = 60     # seconds between heartbeats
IDLE_THRESHOLD = 300    # seconds of no input before "idle" (5 min)
JST            = timezone(timedelta(hours=9))  # Japan Standard Time
_PROCESS_QUERY_LIMITED = 0x1000

# ── Shared state ─────────────────────────────────────────────────────
_status         = "working"
_active_app     = ""
_tray           = [None]
_mutex_handle   = None   # keeps the singleton mutex alive for the process lifetime
_timeline_root  = [None] # reference to the open timeline window (if any)


# ── Single-instance guard ─────────────────────────────────────────────
def _acquire_single_instance() -> bool:
    """Create a named kernel mutex. Returns False if another instance already holds it."""
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Global\\BlueOrbitTrackerSingleton_v1")
    return ctypes.windll.kernel32.GetLastError() != 183  # 183 = ERROR_ALREADY_EXISTS


# ── Windows API helpers (no global hooks, AV-safe) ───────────────────
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds() -> float:
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    now_ms = ctypes.windll.kernel32.GetTickCount64()
    return max(0, now_ms - lii.dwTime) / 1000.0


def _get_foreground_app() -> str:
    """Return the executable name of the currently focused window."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED, False, pid.value)
        if not h:
            return ""
        buf  = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return os.path.basename(buf.value) if buf.value else ""
    except Exception:
        return ""


# ── Auto-update ──────────────────────────────────────────────────────
def _version_gt(a: str, b: str) -> bool:
    """Return True if version string a is newer than b (e.g. "1.2.0" > "1.1.0")."""
    try:
        return tuple(int(x) for x in str(a).split(".")) > tuple(int(x) for x in str(b).split("."))
    except Exception:
        return False


def _check_update(cfg: dict):
    """Hit /tracker/version and prompt the user if a newer build is available."""
    try:
        r = requests.get(
            cfg["serverUrl"].rstrip("/") + "/tracker/version",
            headers={"x-tracker-key": cfg["apiKey"]},
            timeout=10,
        )
        r.raise_for_status()
        data       = r.json()
        server_ver = data.get("version", "")
        dl_url     = data.get("url", "")
        cur_ver    = _bundled_defaults().get("version", "0.0.0")
        _log(f"Version check: current={cur_ver}  latest={server_ver or '—'}")
        if server_ver and dl_url and _version_gt(server_ver, cur_ver):
            _log(f"Update available: {cur_ver} → {server_ver}")
            _prompt_and_update(server_ver, cur_ver, dl_url)
    except Exception as e:
        _log(f"Update check skipped: {e}")


def _prompt_and_update(new_ver: str, cur_ver: str, url: str):
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    ok = messagebox.askyesno(
        "BlueOrbit Tracker — Update Available",
        f"Version {new_ver} is available  (you have {cur_ver}).\n\n"
        "Download and install now?\nThe app will restart automatically.",
        icon="info",
    )
    root.destroy()
    if ok:
        _do_update(url, new_ver)


def _do_update(url: str, new_ver: str):
    """Download the new exe then use PowerShell to swap files after we exit."""
    if not getattr(sys, "frozen", False):
        _log("Not running as a frozen exe — skipping self-update.")
        return

    import tkinter as tk
    from tkinter import messagebox

    current_exe = sys.executable
    new_exe     = current_exe + ".update"

    # Show a simple "downloading…" window while the download runs.
    dlg = tk.Tk()
    dlg.title("BlueOrbit Tracker — Updating")
    dlg.resizable(False, False)
    w, h = 380, 72
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    dlg.attributes("-topmost", True)
    tk.Label(dlg, text=f"Downloading version {new_ver}, please wait…",
             font=("Segoe UI", 10), padx=20).pack(expand=True)
    dlg.update()

    try:
        r = requests.get(url, stream=True, timeout=180)
        r.raise_for_status()
        with open(new_exe, "wb") as f:
            for chunk in r.iter_content(chunk_size=131072):
                f.write(chunk)
                dlg.update()          # keep window responsive

        dlg.destroy()
        _log(f"Download complete → {new_exe}")

        # PowerShell swaps the file after we've fully exited (4-second grace).
        # Single-quoted paths are literal in PowerShell (handles spaces correctly).
        ps_cmd = (
            f"Start-Sleep -Seconds 4; "
            f"Move-Item -Force '{new_exe}' '{current_exe}'; "
            f"Start-Process '{current_exe}'"
        )
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-Command", ps_cmd],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        )
        _log("Restart scheduled. Exiting…")
        icon = _tray[0]
        if icon:
            try: icon.stop()
            except Exception: pass
        os._exit(0)

    except Exception as e:
        try: dlg.destroy()
        except Exception: pass
        try:
            if os.path.exists(new_exe): os.remove(new_exe)
        except Exception: pass
        _log(f"Update failed: {e}")
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Update Failed", f"Could not download update:\n{e}")
        root.destroy()


def _update_checker(cfg: dict):
    """Background thread: check for update 10 s after start, then every 60 s."""
    time.sleep(10)
    while True:
        _check_update(cfg)
        time.sleep(60)


# ── Config ───────────────────────────────────────────────────────────
def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_defaults() -> dict:
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "config.json")
    else:
        bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(bundled, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _show_setup_dialog(path: str) -> dict:
    import tkinter as tk
    from tkinter import messagebox

    defaults = _bundled_defaults()
    result   = {}

    root = tk.Tk()
    root.title("BlueOrbit Tracker — Setup")
    root.resizable(False, False)

    root.update_idletasks()
    w, h = 420, 290
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    PAD = {"padx": 18, "pady": 5}

    tk.Label(root, text="BlueOrbit Tracker — Setup",
             font=("Segoe UI", 13, "bold")).pack(pady=(18, 2))
    tk.Label(root, text="Enter your name to get started.",
             font=("Segoe UI", 9), fg="#555").pack(pady=(0, 12))

    fields = {}
    rows = [
        ("Server URL", "serverUrl", defaults.get("serverUrl", "https://api.blueorbit.solar/api"), False),
        ("API Key",    "apiKey",    defaults.get("apiKey",    ""),                                 True),
        ("Your Name",  "userName",  "",                                                            False),
    ]

    for label, key, default, secret in rows:
        frame = tk.Frame(root)
        frame.pack(fill="x", **PAD)
        tk.Label(frame, text=label, font=("Segoe UI", 9, "bold"),
                 width=11, anchor="w").pack(side="left")
        var   = tk.StringVar(value=default)
        show  = "*" if secret else ""
        entry = tk.Entry(frame, textvariable=var, show=show,
                         font=("Segoe UI", 9), width=32)
        entry.pack(side="left", fill="x", expand=True)
        fields[key] = var

    def on_save():
        cfg = {k: v.get().strip() for k, v in fields.items()}
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            messagebox.showerror("Missing fields",
                                 "Please fill in: " + ", ".join(missing))
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            result.update(cfg)
            root.destroy()
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def on_cancel():
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=(6, 18))
    tk.Button(btn_frame, text="Cancel", width=10,
              command=on_cancel).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Save & Start", width=12,
              font=("Segoe UI", 9, "bold"), bg="#2563eb", fg="white",
              activebackground="#1d4ed8", activeforeground="white",
              bd=0, cursor="hand2",
              command=on_save).pack(side="left", padx=6)

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()

    if not result:
        sys.exit(0)
    return result


def load_config() -> dict:
    path = os.path.join(_app_dir(), "config.json")

    if not os.path.exists(path):
        return _show_setup_dialog(path)

    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        _die(f"config.json is not valid JSON:\n{e}")
        return {}

    for key in ("serverUrl", "apiKey", "userName"):
        if not cfg.get(key):
            return _show_setup_dialog(path)

    return cfg


def _die(msg: str):
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, msg, "BlueOrbit Tracker — Error", 0x10)
    else:
        print(f"[FATAL] {msg}", flush=True)
    sys.exit(1)


# ── Server communication ─────────────────────────────────────────────
def send_tick(cfg: dict, status: str, app: str = ""):
    now_utc = datetime.now(timezone.utc)
    now_jst = datetime.now(JST)
    body = {
        "userName":  cfg["userName"],
        "date":      now_jst.strftime("%Y-%m-%d"),  # JST date for correct day grouping
        "ts":        now_utc.isoformat(),             # UTC for accurate timestamp storage
        "status":    status,
        "activeApp": app,
    }
    url = cfg["serverUrl"].rstrip("/") + "/tracker/push"
    _log(f"→ POST {url}  user={cfg['userName']}  status={status}  app={app or '—'}")
    try:
        r = requests.post(
            url,
            json=body,
            headers={"x-tracker-key": cfg["apiKey"]},
            timeout=10,
        )
        r.raise_for_status()
        _log(f"✓ tick accepted ({r.status_code})")
    except requests.exceptions.ConnectionError:
        _log(f"✗ connection error — server unreachable ({url})")
    except requests.exceptions.Timeout:
        _log("✗ request timed out — will retry next tick")
    except requests.exceptions.HTTPError as e:
        _log(f"✗ HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        _log(f"✗ unexpected error: {e}")


# ── Tick loop (background thread) ────────────────────────────────────
def tick_loop(cfg: dict):
    global _status, _active_app
    while True:
        time.sleep(TICK_INTERVAL)

        idle_secs = _get_idle_seconds()
        status    = "idle" if idle_secs >= IDLE_THRESHOLD else "working"
        _status   = status
        app       = _get_foreground_app() if status == "working" else ""
        _active_app = app

        _log(f"{'●' if status == 'working' else '○'} {status.upper()}  "
             f"(idle {int(idle_secs)}s)  app={app or '—'}")
        send_tick(cfg, status, app)

        icon = _tray[0]
        if icon and TRAY_OK:
            try:
                icon.icon  = _make_icon(status)
                icon.title = f"BlueOrbit — {status.capitalize()}"
            except Exception:
                pass


# ── Personal timeline window ─────────────────────────────────────────
def _fetch_my_ticks(cfg: dict, date_str: str) -> list:
    url = cfg["serverUrl"].rstrip("/") + "/tracker/my-ticks"
    r = requests.get(
        url,
        params={"userName": cfg["userName"], "date": date_str},
        headers={"x-tracker-key": cfg["apiKey"]},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _group_ticks(ticks: list, date_str: str) -> list:
    TICK_S = 60
    GAP_S  = int(2.5 * TICK_S)

    if not ticks:
        return []

    def parse(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    sorted_t = sorted(ticks, key=lambda t: t["ts"])
    segs = []
    cur = {"status": sorted_t[0]["status"], "start": sorted_t[0]["ts"],
           "last": sorted_t[0]["ts"], "count": 1}

    for t in sorted_t[1:]:
        gap = (parse(t["ts"]) - parse(cur["last"])).total_seconds()
        if t["status"] == cur["status"] and gap <= GAP_S:
            cur["last"] = t["ts"]
            cur["count"] += 1
        else:
            segs.append({**cur, "end": (parse(cur["last"]) + timedelta(seconds=TICK_S)).isoformat()})
            cur = {"status": t["status"], "start": t["ts"], "last": t["ts"], "count": 1}

    today_jst = datetime.now(JST).strftime("%Y-%m-%d")
    age_s     = (datetime.now(timezone.utc) - parse(cur["last"])).total_seconds()
    ongoing   = (date_str == today_jst) and (age_s < GAP_S)
    segs.append({**cur, "end": None if ongoing else (parse(cur["last"]) + timedelta(seconds=TICK_S)).isoformat()})
    return segs


def _show_timeline(cfg: dict):
    existing = _timeline_root[0]
    if existing is not None:
        try:
            def _bring_front():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
            existing.after(0, _bring_front)
            return
        except Exception:
            _timeline_root[0] = None
    threading.Thread(target=_timeline_main, args=(cfg,), daemon=True).start()


def _timeline_main(cfg: dict):
    import tkinter as tk
    from tkinter import ttk

    # ── Colors ────────────────────────────────────────────
    WHITE   = "#ffffff"          # header card + table rows
    BG_WIN  = "#edf2ff"          # window background (light blue)
    BG_TL   = "#e4edff"          # timeline section + footer
    BG_IDLE = "#fff0f3"          # idle row tint
    BORDER  = "#b4ccf5"          # outer window border ring
    BORDER2 = "#d4e4ff"          # inner section dividers
    TEXT    = "#0f172a"
    TEXT2   = "#374151"
    MUTED   = "#94a3b8"
    MUTED2  = "#a0b4d6"
    BLUE    = "#2563eb"
    BLUE_L  = "#eff6ff"
    BLUE_B  = "#bfdbfe"
    RED     = "#ef4444"
    RED_L   = "#fef2f2"
    RED_B   = "#fecaca"
    ORANGE  = "#f97316"

    TICK_S = 60

    state    = {"date": datetime.now(JST).date(), "segs": []}
    drag_d   = {"x": 0, "y": 0}

    acc_color        = BLUE if _status == "working" else (RED if _status == "idle" else MUTED)
    status_lbl_text  = _status.capitalize() if _status in ("working", "idle") else "Offline"

    # ── Borderless window ─────────────────────────────────
    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg=BORDER)            # visible as 1-px border ring
    root.minsize(720, 480)
    root.resizable(True, True)
    w, h = 840, 570
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    _timeline_root[0] = root

    # Container (1-px inset = border ring)
    C = tk.Frame(root, bg=BG_WIN)
    C.pack(fill="both", expand=True, padx=1, pady=1)

    # ── Drag logic (bound to header area) ─────────────────
    def _drag_start(e):
        drag_d["x"] = e.x_root - root.winfo_x()
        drag_d["y"] = e.y_root - root.winfo_y()
    def _drag_move(e):
        root.geometry(f"+{e.x_root - drag_d['x']}+{e.y_root - drag_d['y']}")

    # ── 3-px accent bar (also drag handle) ────────────────
    acc_bar = tk.Frame(C, bg=acc_color, height=3, cursor="fleur")
    acc_bar.pack(fill="x")
    acc_bar.bind("<ButtonPress-1>",   _drag_start)
    acc_bar.bind("<B1-Motion>",       _drag_move)

    # ── Header ────────────────────────────────────────────
    hdr = tk.Frame(C, bg=WHITE, padx=18, pady=13, cursor="fleur")
    hdr.pack(fill="x")
    hdr.bind("<ButtonPress-1>", _drag_start)
    hdr.bind("<B1-Motion>",     _drag_move)

    # Avatar canvas
    initials = "".join(p[0].upper() for p in cfg["userName"].split()[:2] if p) or "?"
    avt = tk.Canvas(hdr, width=44, height=44, bg=WHITE, highlightthickness=0, cursor="fleur")  # hdr is WHITE
    avt.pack(side="left", padx=(0, 12))
    avt.create_rectangle(0, 0, 44, 44, fill=BLUE, outline="")
    avt.create_text(22, 22, text=initials, fill="white", font=("Segoe UI", 14, "bold"))
    avt.create_oval(30, 30, 44, 44, fill="white", outline="white", width=3)
    avt.create_oval(32, 32, 42, 42, fill=acc_color, outline="")
    avt.bind("<ButtonPress-1>", _drag_start)
    avt.bind("<B1-Motion>",     _drag_move)

    # Name + badge
    nf = tk.Frame(hdr, bg=WHITE, cursor="fleur")
    nf.pack(side="left", fill="y")
    nf.bind("<ButtonPress-1>", _drag_start)
    nf.bind("<B1-Motion>",     _drag_move)
    name_lbl = tk.Label(nf, text=cfg["userName"], font=("Segoe UI", 15, "bold"),
                        bg=WHITE, fg=TEXT, cursor="fleur")
    name_lbl.pack(anchor="w")
    name_lbl.bind("<ButtonPress-1>", _drag_start)
    name_lbl.bind("<B1-Motion>",     _drag_move)
    badge_f = tk.Frame(nf, bg=acc_color, padx=8, pady=2)
    badge_f.pack(anchor="w", pady=(4, 0))
    tk.Label(badge_f, text=f"● {status_lbl_text}",
             font=("Segoe UI", 8, "bold"), bg=acc_color, fg="white").pack()

    ver = _bundled_defaults().get("version", "")
    tk.Label(hdr, text=f"v{ver}" if ver else "",
             font=("Segoe UI", 8), bg=WHITE, fg=MUTED).pack(side="left", padx=(12, 0))

    tk.Frame(hdr, bg=WHITE).pack(side="left", fill="x", expand=True)  # flex spacer

    # Stats chips
    work_val = tk.StringVar(value="0m")
    idle_val = tk.StringVar(value="0m")

    def _chip(parent, var, sublabel, bg, fg, bc):
        outer = tk.Frame(parent, bg=bc, padx=1, pady=1)
        outer.pack(side="left", padx=(0, 7))
        inner = tk.Frame(outer, bg=bg, padx=14, pady=7)
        inner.pack()
        tk.Label(inner, textvariable=var, font=("Segoe UI", 14, "bold"),
                 bg=bg, fg=fg).pack()
        tk.Label(inner, text=sublabel, font=("Segoe UI", 7, "bold"),
                 bg=bg, fg=fg).pack()

    _chip(hdr, work_val, "WORKING", BLUE_L, BLUE, BLUE_B)
    _chip(hdr, idle_val, "IDLE",    RED_L,  RED,  RED_B)

    # Close + Minimize buttons
    def _close():
        _timeline_root[0] = None
        root.destroy()

    def _minimize():
        root.withdraw()   # hide; tray double-click restores via deiconify()

    root.protocol("WM_DELETE_WINDOW", _close)

    for txt, cmd, hov_bg, hov_fg in [("—", _minimize, BORDER2, TEXT2), ("✕", _close, RED_L, RED)]:
        b = tk.Button(hdr, text=txt, command=cmd,
                      font=("Segoe UI", 11), bg=WHITE, fg=MUTED,
                      activebackground=hov_bg, activeforeground=hov_fg,
                      relief="flat", bd=0, cursor="hand2",
                      width=2, highlightthickness=0)
        b.pack(side="left", padx=(0, 2))

    # ── Divider ────────────────────────────────────────────
    tk.Frame(C, bg=BORDER2, height=1).pack(fill="x")

    # ── Timeline section ──────────────────────────────────
    tl_sec = tk.Frame(C, bg=BG_TL, padx=18, pady=10)
    tl_sec.pack(fill="x")

    tl_top = tk.Frame(tl_sec, bg=BG_TL)
    tl_top.pack(fill="x", pady=(0, 7))
    tk.Label(tl_top, text="▷  DAY TIMELINE",
             font=("Segoe UI", 8, "bold"), bg=BG_TL, fg=MUTED2).pack(side="left")
    leg = tk.Frame(tl_top, bg=BG_TL)
    leg.pack(side="right")
    for sym, lbl, col in [("■", "Working", BLUE), ("■", "Idle", RED), ("—", "Now", ORANGE)]:
        tk.Label(leg, text=sym, font=("Segoe UI", 9), bg=BG_TL, fg=col).pack(side="left", padx=(8, 1))
        tk.Label(leg, text=lbl, font=("Segoe UI", 8), bg=BG_TL, fg=MUTED).pack(side="left")

    # Canvas: 44px bar + 24px for ticks + labels
    cvs = tk.Canvas(tl_sec, height=68, bg=BG_TL, highlightthickness=0)
    cvs.pack(fill="x")

    # ── Divider ────────────────────────────────────────────
    tk.Frame(C, bg=BORDER2, height=1).pack(fill="x")

    # ── Segments table ────────────────────────────────────
    tbl_f = tk.Frame(C, bg=BG_WIN)
    tbl_f.pack(fill="both", expand=True)

    cols   = ("#", "Status", "Start", "End", "Duration")
    widths = [44, 110, 80, 80, 90]

    sty = ttk.Style()
    sty.theme_use("default")
    sty.configure("L.Treeview",
                  background=WHITE, foreground=TEXT2,
                  fieldbackground=WHITE, rowheight=34,
                  font=("Segoe UI", 10), borderwidth=0, relief="flat")
    sty.configure("L.Treeview.Heading",
                  background=BG_TL, foreground=MUTED,
                  font=("Segoe UI", 9, "bold"), relief="flat", borderwidth=0)
    sty.map("L.Treeview",
            background=[("selected", BLUE_L)],
            foreground=[("selected", BLUE)])
    # Slim modern scrollbar
    sty.configure("Slim.Vertical.TScrollbar",
                  background="#b4ccf5", troughcolor=BG_WIN,
                  borderwidth=0, relief="flat",
                  arrowcolor=BG_WIN, arrowsize=0, width=6)
    sty.map("Slim.Vertical.TScrollbar",
            background=[("active", BLUE), ("!active", "#b4ccf5")])

    tree = ttk.Treeview(tbl_f, columns=cols, show="headings",
                        height=14, style="L.Treeview", selectmode="browse")
    for col, cw in zip(cols, widths):
        tree.heading(col, text=col)
        tree.column(col, width=cw, minwidth=cw,
                    anchor="center" if col == "#" else "w")

    vsb = ttk.Scrollbar(tbl_f, orient="vertical",
                        command=tree.yview, style="Slim.Vertical.TScrollbar")
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True, padx=(18, 0))
    vsb.pack(side="right", fill="y", padx=(0, 6), pady=4)

    # ── Divider ────────────────────────────────────────────
    tk.Frame(C, bg=BORDER, height=1).pack(fill="x")

    # ── Footer ────────────────────────────────────────────
    foot = tk.Frame(C, bg=BG_TL, padx=14, pady=10)
    foot.pack(fill="x")

    def _nav_btn(txt, cmd):
        b = tk.Button(foot, text=txt, command=cmd,
                      font=("Segoe UI", 14), bg=WHITE, fg=TEXT2,
                      activebackground=BLUE_L, activeforeground=BLUE,
                      relief="flat", bd=0, cursor="hand2", width=2,
                      highlightbackground=BORDER2, highlightthickness=1)
        b.pack(side="left", padx=(0, 4))
        return b

    prev_btn = _nav_btn("‹", lambda: _change_date(-1))

    date_lbl = tk.Label(foot, text="", font=("Segoe UI", 10, "bold"),
                        bg=WHITE, fg=TEXT, padx=10, pady=5,
                        highlightbackground=BORDER2, highlightthickness=1)
    date_lbl.pack(side="left", padx=(0, 4))

    next_btn = _nav_btn("›", lambda: _change_date(+1))

    today_btn = tk.Button(foot, text="Today", command=lambda: None,
                          font=("Segoe UI", 9, "bold"), bg=BLUE_L, fg=BLUE,
                          activebackground=BLUE_B, activeforeground=BLUE,
                          relief="flat", bd=0, cursor="hand2", padx=10, pady=5,
                          highlightbackground=BLUE_B, highlightthickness=1)

    # ── Helpers ───────────────────────────────────────────
    def _fmt_time(iso):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).strftime("%H:%M")
        except Exception:
            return "—"

    def _fmt_dur(seconds):
        t = max(0, int(seconds // 60))
        h, m = divmod(t, 60)
        if h and m: return f"{h}h {m}m"
        return f"{h}h" if h else (f"{m}m" if m else "0m")

    def _fill_gaps(segs):
        if len(segs) <= 1:
            return segs
        result = []
        for i, seg in enumerate(segs):
            result.append(seg)
            if i < len(segs) - 1 and seg.get("end"):
                try:
                    gap_s = (
                        datetime.fromisoformat(segs[i+1]["start"].replace("Z", "+00:00")) -
                        datetime.fromisoformat(seg["end"].replace("Z", "+00:00"))
                    ).total_seconds()
                    if gap_s >= TICK_S * 1.5:
                        result.append({"status": "idle", "start": seg["end"],
                                       "end": segs[i+1]["start"], "isGap": True})
                except Exception:
                    pass
        return result

    def _draw(segs):
        cvs.update_idletasks()
        cvs.delete("all")
        W   = max(cvs.winfo_width(), 1)
        BAR = 44   # bar occupies y=0..BAR
        PAD = 5    # vertical padding for activity segments inside the bar

        # Bar track — white with a soft border
        cvs.create_rectangle(0, 0, W, BAR, fill=WHITE, outline=BORDER2, width=1)

        if not segs:
            cvs.create_text(W // 2, BAR // 2, text="No data for this day",
                            font=("Segoe UI", 9), fill=MUTED)

        for seg in segs:
            try:
                s_dt  = datetime.fromisoformat(seg["start"].replace("Z", "+00:00")).astimezone(JST)
                e_iso = seg.get("end")
                e_dt  = datetime.fromisoformat(e_iso.replace("Z", "+00:00")).astimezone(JST) \
                        if e_iso else datetime.now(JST)
                s_min = s_dt.hour * 60 + s_dt.minute + s_dt.second / 60
                e_min = e_dt.hour * 60 + e_dt.minute + e_dt.second / 60
                col   = BLUE if seg["status"] == "working" else RED
                x1 = max(1, s_min / 1440 * W)
                x2 = min(W - 1, max(x1 + 4, e_min / 1440 * W))
                cvs.create_rectangle(x1, PAD, x2, BAR - PAD, fill=col, outline="")
            except Exception:
                pass

        # Tick marks — major at 0/6/12/18/24, minor elsewhere
        for h in range(25):
            x     = h / 24 * W
            major = h % 6 == 0
            tick_h = 8 if major else 4
            cvs.create_line(x, BAR, x, BAR + tick_h,
                            fill=MUTED if major else MUTED2, width=1)
            if major:
                cvs.create_text(x, BAR + tick_h + 3, text=f"{h:02d}",
                                font=("Segoe UI", 8, "bold"), fill=MUTED, anchor="n")

        # "Now" marker — vertical line + dot centered inside bar
        if state["date"] == datetime.now(JST).date():
            now_jst = datetime.now(JST)
            nx  = (now_jst.hour * 60 + now_jst.minute + now_jst.second / 60) / 1440 * W
            mid = BAR // 2
            cvs.create_line(nx, 1, nx, BAR - 1, fill=ORANGE, width=2)
            cvs.create_oval(nx - 5, mid - 5, nx + 5, mid + 5,
                            fill=ORANGE, outline=WHITE, width=2)

    def _load():
        d        = state["date"]
        ds       = d.strftime("%Y-%m-%d")
        is_today = d == datetime.now(JST).date()

        date_lbl.config(text=ds)
        next_btn.config(state="disabled" if is_today else "normal",
                        fg=MUTED2 if is_today else TEXT2)
        if is_today:
            today_btn.pack_forget()
        else:
            today_btn.pack(side="left", padx=(0, 4))

        for row in tree.get_children():
            tree.delete(row)
        cvs.delete("all")
        work_val.set("…")
        idle_val.set("…")

        def _fetch():
            try:
                ticks = _fetch_my_ticks(cfg, ds)
                raw   = _group_ticks(ticks, ds)
                root.after(0, lambda: _render(ticks, raw))
            except Exception:
                root.after(0, lambda: _render([], []))

        threading.Thread(target=_fetch, daemon=True).start()

    def _render(ticks, raw_segs):
        segs   = _fill_gaps(raw_segs)
        work_s = idle_s = 0

        for i, seg in enumerate(segs):
            try:
                s     = datetime.fromisoformat(seg["start"].replace("Z", "+00:00"))
                e_iso = seg.get("end")
                if e_iso:
                    e     = datetime.fromisoformat(e_iso.replace("Z", "+00:00"))
                    dur_s = max(0, (e - s).total_seconds())
                else:
                    dur_s = max(0, (datetime.now(timezone.utc) - s).total_seconds())

                if seg["status"] == "working":
                    work_s += dur_s
                else:
                    idle_s += dur_s

                end_str = _fmt_time(e_iso) if e_iso else "ongoing"
                lbl     = "Inactive" if seg.get("isGap") else \
                          ("Working" if seg["status"] == "working" else "Idle")
                tag     = "w" if seg["status"] == "working" else "i"
                tree.insert("", "end",
                            values=(i + 1, lbl, _fmt_time(seg["start"]), end_str, _fmt_dur(dur_s)),
                            tags=(tag,))
            except Exception:
                pass

        tree.tag_configure("w", foreground=BLUE, background=WHITE)
        tree.tag_configure("i", foreground=RED,  background=BG_IDLE)

        work_val.set(_fmt_dur(work_s) if work_s else "0m")
        idle_val.set(_fmt_dur(idle_s) if idle_s else "0m")
        state["segs"] = segs
        root.after(50, lambda: _draw(segs))

    def _change_date(delta):
        state["date"] = state["date"] + timedelta(days=delta)
        _load()

    def _go_today():
        state["date"] = datetime.now(JST).date()
        _load()

    today_btn.config(command=_go_today)

    def _auto_refresh():
        if state["date"] == datetime.now(JST).date():
            _load()
        root.after(60_000, _auto_refresh)
    root.after(60_000, _auto_refresh)

    cvs.bind("<Configure>", lambda e: _draw(state.get("segs", [])))

    root.after(120, _load)
    root.mainloop()


# ── System-tray helpers ──────────────────────────────────────────────
def _make_icon(status: str) -> "Image.Image":
    S    = 64
    img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if status == "working":
        bg = (37, 99, 235)      # blue
    elif status == "idle":
        bg = (239, 68, 68)      # red
    else:
        bg = (100, 116, 139)    # slate-500

    # ── Rounded-square background (Pillow-compatible) ──
    def _rrect(x0, y0, x1, y1, r, fill):
        draw.rectangle([x0+r, y0, x1-r, y1], fill=fill)
        draw.rectangle([x0, y0+r, x1, y1-r], fill=fill)
        draw.ellipse([x0, y0, x0+2*r, y0+2*r], fill=fill)
        draw.ellipse([x1-2*r, y0, x1, y0+2*r], fill=fill)
        draw.ellipse([x0, y1-2*r, x0+2*r, y1], fill=fill)
        draw.ellipse([x1-2*r, y1-2*r, x1, y1], fill=fill)

    _rrect(4, 4, S-4, S-4, r=12, fill=bg)

    W = (255, 255, 255, 220)   # white with slight transparency

    if status == "working":
        # Three ascending bars (activity indicator)
        for x0, y0, x1 in [(13, 42, 22), (27, 30, 36), (41, 20, 50)]:
            _rrect(x0, y0, x1, 52, r=3, fill=W)

    elif status == "idle":
        # Pause symbol — two vertical rectangles
        _rrect(16, 17, 27, 47, r=3, fill=W)
        _rrect(37, 17, 48, 47, r=3, fill=W)

    else:
        # Offline — horizontal dash
        _rrect(14, 27, 50, 37, r=4, fill=W)

    return img


def _confirm_dialog(title: str, message: str) -> bool:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    answer = messagebox.askyesno(title, message)
    root.destroy()
    return answer


def _start_tray(cfg: dict):
    def _quit(icon, _item):
        icon.stop()
        os._exit(1)  # non-zero → Task Scheduler treats as failure → restarts in ~1 min

    def _open_timeline(icon, _item):
        _show_timeline(cfg)

    # default=True makes this the double-click action on Windows
    icon = pystray.Icon(
        name  = "BlueOrbit Tracker",
        icon  = _make_icon(_status),
        title = "BlueOrbit Tracker",
        menu  = pystray.Menu(
            pystray.MenuItem("My Timeline", _open_timeline, default=True),
            pystray.MenuItem("Quit",        _quit),
        ),
    )
    _tray[0] = icon
    icon.run()


# ── Windows auto-startup (Task Scheduler with auto-restart) ──────────
_RUN_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME  = "BlueOrbitTracker"
_TASK_NAME = "BlueOrbitTracker"


def register_startup():
    """Register a Task Scheduler task that auto-restarts the tracker if killed."""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    exe = sys.executable.replace("'", "''")  # escape single quotes for PowerShell
    ps_cmd = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '--watchdog'; "
        "$t = New-ScheduledTaskTrigger -AtLogOn; "
        "$s = New-ScheduledTaskSettingsSet "
        "  -ExecutionTimeLimit (New-TimeSpan -Hours 0) "
        "  -RestartCount 999 "
        "  -RestartInterval (New-TimeSpan -Minutes 1) "
        "  -StartWhenAvailable "
        "  -MultipleInstances IgnoreNew; "
        f"Register-ScheduledTask -TaskName '{_TASK_NAME}' "
        "  -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    try:
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-Command", ps_cmd],
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30,
        )
        _log("Registered Task Scheduler task (auto-restart on kill).")
    except Exception as e:
        _log(f"Could not register scheduled task: {e}")
    # Remove legacy registry Run key if present
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            try: winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError: pass
    except Exception:
        pass


def unregister_startup():
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-Command",
             f"Unregister-ScheduledTask -TaskName '{_TASK_NAME}'"
             " -Confirm:$false -ErrorAction SilentlyContinue"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=30,
        )
    except Exception:
        pass
    # Also remove legacy registry Run key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            try: winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError: pass
    except Exception:
        pass


# ── Logging helper ───────────────────────────────────────────────────
_log_path: str | None = None


def _init_log():
    global _log_path
    _log_path = os.path.join(_app_dir(), "tracker.log")


def _log(msg: str):
    ts   = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── Entry point ──────────────────────────────────────────────────────
def main():
    # ── Prevent multiple instances ────────────────────────
    # --watchdog is passed by the Task Scheduler task; exit silently so
    # Task Scheduler sees success (code 0) and does NOT trigger a restart.
    watchdog_mode = "--watchdog" in sys.argv
    if not _acquire_single_instance():
        if not watchdog_mode:
            ctypes.windll.user32.MessageBoxW(
                0,
                "BlueOrbit Tracker is already running.\n\nCheck the system tray.",
                "BlueOrbit Tracker",
                0x40,   # MB_ICONINFORMATION
            )
        sys.exit(0)

    _init_log()
    cfg = load_config()
    register_startup()

    print("=" * 52, flush=True)
    print("  BlueOrbit Tracker", flush=True)
    print(f"  User  : {cfg['userName']}", flush=True)
    print(f"  Server: {cfg['serverUrl']}", flush=True)
    print(f"  Tick  : every {TICK_INTERVAL}s", flush=True)
    print(f"  Idle  : after {IDLE_THRESHOLD}s of inactivity", flush=True)
    print(f"  TZ    : JST (UTC+9)", flush=True)
    print("=" * 52, flush=True)

    threading.Thread(target=send_tick, args=(cfg, "working", _get_foreground_app()), daemon=True).start()
    _log("Sending first heartbeat now…")

    threading.Thread(target=tick_loop, args=(cfg,), daemon=True).start()
    _log(f"Next heartbeat in {TICK_INTERVAL}s…")

    threading.Thread(target=_update_checker, args=(cfg,), daemon=True).start()
    _log("Update checker started (first check in 30 s).")

    if TRAY_OK:
        _log("Tray active. Double-click icon to open timeline.")
        _start_tray(cfg)
    else:
        _log("Tray not available (install pystray + Pillow).")
        _log("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _log("Stopped.")


if __name__ == "__main__":
    main()
