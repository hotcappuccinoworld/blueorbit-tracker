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
_status     = "working"
_active_app = ""
_tray       = [None]


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
    threading.Thread(target=_timeline_main, args=(cfg,), daemon=True).start()


def _timeline_main(cfg: dict):
    import tkinter as tk
    from tkinter import ttk

    BG      = "#0f172a"
    BG2     = "#1e293b"
    BORDER  = "#334155"
    TEXT    = "#f1f5f9"
    MUTED   = "#94a3b8"
    BLUE    = "#2563eb"
    RED     = "#ef4444"
    BLUE_BG = "#1e3a8a"

    state = {"date": datetime.now(JST).date()}

    root = tk.Tk()
    root.title(f"My Timeline — {cfg['userName']}")
    root.configure(bg=BG)
    root.minsize(700, 460)
    root.resizable(True, True)
    root.update_idletasks()
    w, h = 760, 540
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ── Header ────────────────────────────────────────────
    hdr = tk.Frame(root, bg=BG, padx=18, pady=14)
    hdr.pack(fill="x")

    tk.Label(hdr, text="My Timeline",
             font=("Segoe UI", 15, "bold"), bg=BG, fg=TEXT).pack(side="left")
    tk.Label(hdr, text=f"  {cfg['userName']}  (JST)",
             font=("Segoe UI", 11), bg=BG, fg=MUTED).pack(side="left", pady=2)

    nav = tk.Frame(hdr, bg=BG)
    nav.pack(side="right")

    date_var = tk.StringVar()
    tk.Label(nav, textvariable=date_var,
             font=("Segoe UI", 9), bg=BG, fg=MUTED).pack(side="right", padx=(10, 0))

    def _nav_btn(parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd,
                      font=("Segoe UI", 9), bg=BG2, fg=TEXT,
                      activebackground=BORDER, activeforeground=TEXT,
                      relief="flat", padx=9, pady=3, cursor="hand2", bd=0)
        b.pack(side="left", padx=2)
        return b

    _nav_btn(nav, "← Prev",  lambda: _change_date(-1))
    today_btn = _nav_btn(nav, "Today", lambda: None)  # command set below
    _nav_btn(nav, "Next →",  lambda: _change_date(+1))

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    # ── Status bar ────────────────────────────────────────
    status_var = tk.StringVar(value="Loading…")
    tk.Label(root, textvariable=status_var,
             font=("Segoe UI", 9), bg=BG, fg=MUTED,
             anchor="w", padx=18, pady=5).pack(fill="x")

    # ── Timeline canvas ───────────────────────────────────
    cvs_frame = tk.Frame(root, bg=BG, padx=18, pady=0)
    cvs_frame.pack(fill="x")

    cvs = tk.Canvas(cvs_frame, height=70, bg=BG, highlightthickness=0)
    cvs.pack(fill="x")

    # ── Summary row ───────────────────────────────────────
    sum_frame = tk.Frame(root, bg=BG, padx=18, pady=6)
    sum_frame.pack(fill="x")

    work_chip = tk.Label(sum_frame, font=("Segoe UI", 10, "bold"),
                         bg=BLUE_BG, fg="#93c5fd", padx=10, pady=3, relief="flat")
    idle_chip = tk.Label(sum_frame, font=("Segoe UI", 10, "bold"),
                         bg="#450a0a", fg="#fca5a5", padx=10, pady=3, relief="flat")

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=18)

    # ── Segments table ────────────────────────────────────
    tbl_frame = tk.Frame(root, bg=BG, padx=18, pady=10)
    tbl_frame.pack(fill="both", expand=True)

    cols   = ("#", "Status", "Start (JST)", "End (JST)", "Duration", "Ticks")
    widths = [36, 90, 90, 90, 90, 60]

    style = ttk.Style()
    style.theme_use("default")
    style.configure("T.Treeview",
                    background=BG2, foreground=TEXT,
                    fieldbackground=BG2, rowheight=28,
                    font=("Segoe UI", 9), borderwidth=0)
    style.configure("T.Treeview.Heading",
                    background=BORDER, foreground=MUTED,
                    font=("Segoe UI", 9, "bold"), relief="flat")
    style.map("T.Treeview", background=[("selected", BLUE)])
    style.configure("T.Vertical.TScrollbar",
                    background=BG2, troughcolor=BG, borderwidth=0)

    tree = ttk.Treeview(tbl_frame, columns=cols, show="headings",
                        height=12, style="T.Treeview")
    for col, cw in zip(cols, widths):
        anchor = "center" if col in ("#", "Ticks") else "w"
        tree.heading(col, text=col)
        tree.column(col, width=cw, minwidth=cw, anchor=anchor)

    vsb = ttk.Scrollbar(tbl_frame, orient="vertical",
                        command=tree.yview, style="T.Vertical.TScrollbar")
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    # ── Bottom bar ────────────────────────────────────────
    bot = tk.Frame(root, bg=BG, pady=8)
    bot.pack(fill="x")
    _nav_btn(bot, "⟳  Refresh", lambda: _load())

    # ── Helpers ───────────────────────────────────────────
    def _fmt_time(iso: str) -> str:
        try:
            return (datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    .astimezone(JST).strftime("%H:%M"))
        except Exception:
            return "—"

    def _fmt_dur(seconds: float) -> str:
        t = max(0, int(seconds // 60))
        h, m = divmod(t, 60)
        if h and m: return f"{h}h {m}m"
        return f"{h}h" if h else (f"{m}m" if m else "0m")

    def _draw(segs):
        cvs.delete("all")
        W = cvs.winfo_width() or 720
        H_BAR = 44

        cvs.create_rectangle(0, 0, W, H_BAR, fill=BG2, outline=BORDER, width=1)

        for seg in segs:
            try:
                s_dt  = datetime.fromisoformat(seg["start"].replace("Z", "+00:00")).astimezone(JST)
                e_iso = seg.get("end")
                if e_iso:
                    e_dt = datetime.fromisoformat(e_iso.replace("Z", "+00:00")).astimezone(JST)
                else:
                    e_dt = datetime.now(JST)
                s_min = s_dt.hour * 60 + s_dt.minute + s_dt.second / 60
                e_min = e_dt.hour * 60 + e_dt.minute + e_dt.second / 60
                color = BLUE if seg["status"] == "working" else RED
                cvs.create_rectangle(
                    max(1, s_min / 1440 * W), 2,
                    min(W - 1, max(s_min / 1440 * W + 2, e_min / 1440 * W)), H_BAR - 2,
                    fill=color, outline="", width=0)
            except Exception:
                pass

        now_jst = datetime.now(JST)
        for h in range(0, 25, 2):
            x     = h / 24 * W
            major = h % 4 == 0
            cvs.create_line(x, H_BAR - (8 if major else 4), x, H_BAR,
                            fill=MUTED if major else BORDER, width=1)
            if major:
                cvs.create_text(x, H_BAR + 10, text=f"{h:02d}",
                                font=("Segoe UI", 8), fill=MUTED, anchor="n")

        if state["date"] == datetime.now(JST).date():
            now_min = now_jst.hour * 60 + now_jst.minute + now_jst.second / 60
            nx = now_min / 1440 * W
            cvs.create_line(nx, 0, nx, H_BAR, fill="#f97316", width=2)

    def _load():
        d  = state["date"]
        ds = d.strftime("%Y-%m-%d")
        today_jst = datetime.now(JST).date()
        today_btn.config(state="disabled" if d == today_jst else "normal")
        date_var.set(f"{ds} (JST)")
        status_var.set("Loading…")
        work_chip.pack_forget()
        idle_chip.pack_forget()
        for row in tree.get_children():
            tree.delete(row)
        cvs.delete("all")

        def _fetch():
            try:
                ticks = _fetch_my_ticks(cfg, ds)
                segs  = _group_ticks(ticks, ds)
                root.after(0, lambda: _render(ticks, segs))
            except Exception as exc:
                root.after(0, lambda: status_var.set(f"Error: {exc}"))

        threading.Thread(target=_fetch, daemon=True).start()

    def _render(ticks, segs):
        work_s = idle_s = 0
        for i, seg in enumerate(segs):
            try:
                s = datetime.fromisoformat(seg["start"].replace("Z", "+00:00"))
                e_iso = seg.get("end")
                if e_iso:
                    e = datetime.fromisoformat(e_iso.replace("Z", "+00:00"))
                    dur_s = max(0, (e - s).total_seconds())
                else:
                    dur_s = max(0, (datetime.now(timezone.utc) - s).total_seconds())
                if seg["status"] == "working":
                    work_s += dur_s
                else:
                    idle_s += dur_s
                end_str = _fmt_time(e_iso) if e_iso else "now ●"
                tag = "w" if seg["status"] == "working" else "i"
                tree.insert("", "end",
                            values=(i + 1,
                                    "Working" if seg["status"] == "working" else "Idle",
                                    _fmt_time(seg["start"]), end_str,
                                    _fmt_dur(dur_s), seg["count"]),
                            tags=(tag,))
            except Exception:
                pass

        tree.tag_configure("w", foreground="#93c5fd")
        tree.tag_configure("i", foreground="#fca5a5")

        status_var.set(
            f"{len(ticks)} ticks  •  {len(segs)} segment{'s' if len(segs) != 1 else ''}")
        if work_s > 0:
            work_chip.config(text=f"  Working: {_fmt_dur(work_s)}  ")
            work_chip.pack(side="left", padx=(0, 8))
        if idle_s > 0:
            idle_chip.config(text=f"  Idle: {_fmt_dur(idle_s)}  ")
            idle_chip.pack(side="left")
        if not ticks:
            status_var.set("No data for this day.")

        root.after(50, lambda: _draw(segs))

    def _change_date(delta: int):
        state["date"] = state["date"] + timedelta(days=delta)
        _load()

    def _go_today():
        state["date"] = datetime.now(JST).date()
        _load()

    today_btn.config(command=_go_today)
    root.after(120, _load)
    root.mainloop()


# ── System-tray helpers ──────────────────────────────────────────────
def _make_icon(status: str) -> "Image.Image":
    size  = 64
    img   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    # working = blue (#2563eb), idle = red (#ef4444)
    color = (37, 99, 235) if status == "working" else (239, 68, 68)
    draw.ellipse([6, 6, size - 6, size - 6], fill=color)
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
        unregister_startup()
        icon.stop()
        os._exit(0)

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


# ── Windows auto-startup ─────────────────────────────────────────────
_RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "BlueOrbitTracker"


def register_startup():
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    import winreg
    exe_path = sys.executable
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_READ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, _APP_NAME)
                if current == exe_path:
                    return
            except FileNotFoundError:
                pass
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, exe_path)
        _log("Registered for Windows startup.")
    except Exception as e:
        _log(f"Could not register for startup: {e}")


def unregister_startup():
    if sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, _APP_NAME)
            except FileNotFoundError:
                pass
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
