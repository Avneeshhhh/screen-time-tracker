# -*- coding: utf-8 -*-
"""
Screen Time Tracker — Improved
================================
Fixes applied vs original:
  1. Thread-safe usage_data via threading.Lock (race condition fix)
  2. DB upsert with ON CONFLICT — no duplicate rows on multiple sessions
  3. UNIQUE(app_name, date) constraint on DB schema
  4. session_seconds reset on each new start
  5. stop_tracking() guarded against double-stop
  6. threading.Event for clean background-thread shutdown
  7. plt.show() moved to daemon thread — UI no longer freezes
  8. Robust get_active_app() handling bad/system PIDs
  9. load_today_data() restores today's DB rows on startup
  10. Friendly app name mapping (chrome.exe → Google Chrome, etc.)
  11. export_csv() warns user when there is no data; saves to script dir
  12. Timer uses independent after() chain — no drift from tracking thread

UI improvements:
  - Deep navy dark theme (#0B1120 base)
  - Header bar with live clock
  - Three animated stat cards (Session Time, Total Today, Apps Tracked)
  - Scrollable app list with rank badge, mini progress bar, colour coding
  - Start / Stop toggle with proper enabled/disabled styling
  - Chart, Export CSV, Clear Session buttons
  - Dashboard + History tab view
  - 7-day history bar chart embedded via matplotlib FigureCanvasTkAgg
  - Footer bar

Requirements:
    pip install customtkinter psutil pywin32 matplotlib pandas
"""

import os
import threading
import time
from datetime import datetime, timedelta

import customtkinter as ctk
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import pandas as pd
import psutil
import sqlite3
import win32gui
import win32process
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import messagebox

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

DB_NAME       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.db")
POLL_INTERVAL = 1        # seconds between active-app polls
TOP_N_APPS    = 15       # how many apps to show in the list

# Map raw process names → human-readable labels
FRIENDLY_NAMES: dict[str, str] = {
    "chrome.exe":          "Google Chrome",
    "firefox.exe":         "Firefox",
    "msedge.exe":          "Microsoft Edge",
    "Code.exe":            "VS Code",
    "devenv.exe":          "Visual Studio",
    "explorer.exe":        "File Explorer",
    "Spotify.exe":         "Spotify",
    "Discord.exe":         "Discord",
    "slack.exe":           "Slack",
    "Teams.exe":           "Microsoft Teams",
    "WINWORD.EXE":         "Microsoft Word",
    "EXCEL.EXE":           "Microsoft Excel",
    "POWERPNT.EXE":        "PowerPoint",
    "ONENOTE.EXE":         "OneNote",
    "notepad.exe":         "Notepad",
    "notepad++.exe":       "Notepad++",
    "WindowsTerminal.exe": "Windows Terminal",
    "powershell.exe":      "PowerShell",
    "cmd.exe":             "Command Prompt",
    "vlc.exe":             "VLC Media Player",
    "Taskmgr.exe":         "Task Manager",
    "python.exe":          "Python",
    "pythonw.exe":         "Python (GUI)",
    "OUTLOOK.EXE":         "Outlook",
    "steamwebhelper.exe":  "Steam",
    "idea64.exe":          "IntelliJ IDEA",
    "pycharm64.exe":       "PyCharm",
    "zoom.exe":            "Zoom",
    "brave.exe":           "Brave Browser",
    "opera.exe":           "Opera",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Shared state  (always access usage_data under data_lock)
# ─────────────────────────────────────────────────────────────────────────────

usage_data:     dict[str, int] = {}
data_lock:      threading.Lock = threading.Lock()
tracking:       bool           = False
stop_event:     threading.Event = threading.Event()
session_seconds: int           = 0


# ─────────────────────────────────────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def init_database() -> None:
    """Create the usage table with a UNIQUE constraint (app_name, date)."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name      TEXT    NOT NULL,
                usage_seconds INTEGER NOT NULL DEFAULT 0,
                date          TEXT    NOT NULL,
                UNIQUE(app_name, date)
            )
        """)
        conn.commit()


def load_today_data() -> dict[str, int]:
    """
    Return today's rows from the DB so that a new session
    accumulates on top of earlier sessions from the same day.
    """
    today  = datetime.now().strftime("%Y-%m-%d")
    result: dict[str, int] = {}
    try:
        with sqlite3.connect(DB_NAME) as conn:
            rows = conn.execute(
                "SELECT app_name, usage_seconds FROM usage WHERE date = ?",
                (today,)
            ).fetchall()
        for app_name, seconds in rows:
            result[app_name] = int(seconds)
    except Exception as exc:
        print(f"[DB] load_today_data error: {exc}")
    return result


def save_to_database() -> None:
    """
    Upsert today's usage.
    Uses ON CONFLICT … DO UPDATE so multiple stop/start cycles never
    create duplicate rows — they simply update the existing row.
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Take a snapshot outside the long DB operation
    with data_lock:
        snapshot = dict(usage_data)

    try:
        with sqlite3.connect(DB_NAME) as conn:
            for app_name, seconds in snapshot.items():
                conn.execute("""
                    INSERT INTO usage (app_name, usage_seconds, date)
                    VALUES (?, ?, ?)
                    ON CONFLICT(app_name, date)
                    DO UPDATE SET usage_seconds = excluded.usage_seconds
                """, (app_name, seconds, today))
            conn.commit()
    except Exception as exc:
        print(f"[DB] save_to_database error: {exc}")


def get_history(days: int = 7) -> pd.DataFrame:
    """Return a DataFrame with one row per day: (date, total_seconds)."""
    start = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_NAME) as conn:
            df = pd.read_sql_query(
                """
                SELECT date, SUM(usage_seconds) AS total_seconds
                FROM usage
                WHERE date >= ?
                GROUP BY date
                ORDER BY date
                """,
                conn, params=(start,)
            )
        return df
    except Exception as exc:
        print(f"[DB] get_history error: {exc}")
        return pd.DataFrame(columns=["date", "total_seconds"])


# ─────────────────────────────────────────────────────────────────────────────
#  Active-app detection
# ─────────────────────────────────────────────────────────────────────────────

def get_active_app() -> str:
    """
    Return a friendly display name for the currently focused application.
    Handles system windows, locked screens, and AccessDenied gracefully.
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "Desktop"

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid <= 0:
            return "System"

        try:
            proc     = psutil.Process(pid)
            raw_name = proc.name()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return "System"
        except psutil.AccessDenied:
            # Still give a name from the window title if possible
            title = win32gui.GetWindowText(hwnd)
            return title[:40] if title else "System (Protected)"

        return FRIENDLY_NAMES.get(raw_name, raw_name.removesuffix(".exe").removesuffix(".EXE"))

    except Exception:
        return "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  Background tracking thread
# ─────────────────────────────────────────────────────────────────────────────

def tracking_loop() -> None:
    """
    Poll the active app every POLL_INTERVAL seconds.
    Uses stop_event.wait() instead of time.sleep() so the thread
    exits promptly when stop_event is set.
    """
    while not stop_event.is_set():
        app_name = get_active_app()

        with data_lock:
            usage_data[app_name] = usage_data.get(app_name, 0) + 1

        # Schedule the UI refresh on the main thread — never touch widgets here
        root.after(0, update_dashboard)

        stop_event.wait(POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_sorted_apps() -> list[tuple[str, int]]:
    """Return a thread-safe sorted snapshot of usage_data."""
    with data_lock:
        return sorted(usage_data.items(), key=lambda x: x[1], reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard UI refresh  (always called from main thread via root.after)
# ─────────────────────────────────────────────────────────────────────────────

def update_dashboard() -> None:
    """Refresh stat cards and the scrollable app list."""
    with data_lock:
        total     = sum(usage_data.values())
        app_count = len(usage_data)

    total_time_var.set(format_time(total))
    apps_count_var.set(str(app_count))

    sorted_apps = get_sorted_apps()
    top_apps    = sorted_apps[:TOP_N_APPS]
    max_time    = top_apps[0][1] if top_apps else 1

    _rebuild_app_list(top_apps, max_time)


def _rebuild_app_list(top_apps: list[tuple[str, int]], max_time: int) -> None:
    """Destroy all rows and repopulate the scrollable frame."""
    for widget in scroll_frame.winfo_children():
        widget.destroy()

    if not top_apps:
        ctk.CTkLabel(
            scroll_frame,
            text="No data yet — press ▶ Start to begin tracking",
            font=("Segoe UI", 14),
            text_color="#4B5563"
        ).pack(pady=60)
        return

    for idx, (name, secs) in enumerate(top_apps):
        ratio = secs / max_time if max_time > 0 else 0
        _create_app_row(idx, name, secs, ratio)


def _create_app_row(idx: int, name: str, secs: int, ratio: float) -> None:
    """Build one app-row card inside the scrollable frame."""
    row_bg = "#1A2234" if idx % 2 == 0 else "#1E293B"

    row = ctk.CTkFrame(scroll_frame, fg_color=row_bg, corner_radius=8, height=58)
    row.pack(fill="x", padx=8, pady=2)
    row.pack_propagate(False)

    # ── Rank badge
    ctk.CTkLabel(
        row,
        text=f"#{idx + 1}",
        font=("Segoe UI", 11, "bold"),
        text_color="#4B5563",
        width=30,
        anchor="center"
    ).place(x=8, y=20)

    # ── App name
    ctk.CTkLabel(
        row,
        text=name,
        font=("Segoe UI", 13, "bold"),
        text_color="#E2E8F0",
        anchor="w",
    ).place(x=46, y=8)

    # ── Progress bar background
    BAR_W = 220
    bar_bg = ctk.CTkFrame(row, fg_color="#0F172A", corner_radius=4, height=6, width=BAR_W)
    bar_bg.place(x=46, y=38)

    # ── Progress bar fill  (colour-coded by usage share)
    fill_w = max(int(ratio * BAR_W), 4)
    if ratio > 0.60:
        bar_color = "#EF4444"    # red   — dominant app
    elif ratio > 0.30:
        bar_color = "#F59E0B"    # amber — moderate
    else:
        bar_color = "#3B82F6"    # blue  — low

    ctk.CTkFrame(row, fg_color=bar_color, corner_radius=4, height=6, width=fill_w).place(x=46, y=38)

    # ── Time on the right
    ctk.CTkLabel(
        row,
        text=format_time(secs),
        font=("Consolas", 13, "bold"),
        text_color="#94A3B8"
    ).place(relx=1.0, x=-12, y=20, anchor="e")


# ─────────────────────────────────────────────────────────────────────────────
#  Live timers  (main thread, app.after chains)
# ─────────────────────────────────────────────────────────────────────────────

def tick_session_timer() -> None:
    """Increment and display the session timer every second."""
    global session_seconds
    if tracking:
        session_seconds += 1
        session_time_var.set(format_time(session_seconds))
        root.after(1000, tick_session_timer)


def tick_clock() -> None:
    """Update the header clock every second."""
    clock_var.set(datetime.now().strftime("%I:%M:%S %p"))
    root.after(1000, tick_clock)


# ─────────────────────────────────────────────────────────────────────────────
#  Button actions
# ─────────────────────────────────────────────────────────────────────────────

def start_tracking() -> None:
    global tracking, session_seconds

    if tracking:
        return  # guard: already running

    tracking        = True
    session_seconds = 0        # always reset for a fresh session display
    stop_event.clear()

    start_btn.configure(state="disabled", fg_color="#1E3A5F", text_color="#6B7280")
    stop_btn.configure(state="normal",   fg_color="#EF4444", hover_color="#DC2626", text_color="white")
    status_var.set("● TRACKING")
    status_label.configure(text_color="#22C55E")

    tick_session_timer()
    threading.Thread(target=tracking_loop, daemon=True).start()


def stop_tracking() -> None:
    global tracking

    if not tracking:
        return  # guard: already stopped

    tracking = False
    stop_event.set()

    save_to_database()

    start_btn.configure(state="normal",   fg_color="#3B82F6", hover_color="#2563EB", text_color="white")
    stop_btn.configure(state="disabled", fg_color="#374151", text_color="#6B7280")
    status_var.set("◼ STOPPED")
    status_label.configure(text_color="#EF4444")

    messagebox.showinfo("Saved", "Session data saved to database.")


def clear_session() -> None:
    """Wipe the current in-memory session (does not touch DB)."""
    global session_seconds

    if tracking:
        messagebox.showwarning("Active Session", "Stop tracking before clearing session data.")
        return

    with data_lock:
        if not usage_data:
            return

    if not messagebox.askyesno("Clear Session", "Clear all in-memory session data?\n(Database records are kept.)"):
        return

    with data_lock:
        usage_data.clear()

    session_seconds = 0
    session_time_var.set("00:00:00")
    total_time_var.set("00:00:00")
    apps_count_var.set("0")
    update_dashboard()


def show_chart() -> None:
    """Render a dark-themed pie chart in a non-blocking daemon thread."""
    with data_lock:
        snapshot = dict(usage_data)

    if not snapshot:
        messagebox.showwarning("No Data", "Start tracking first.")
        return

    def _draw() -> None:
        apps   = list(snapshot.keys())
        values = list(snapshot.values())

        COLORS = [
            "#3B82F6", "#8B5CF6", "#EC4899", "#F59E0B", "#10B981",
            "#EF4444", "#06B6D4", "#84CC16", "#F97316", "#6366F1",
            "#14B8A6", "#F43F5E", "#A855F7", "#FBBF24", "#34D399",
        ]

        fig, ax = plt.subplots(figsize=(9, 7))
        fig.patch.set_facecolor("#0F172A")
        ax.set_facecolor("#0F172A")

        wedges, texts, autotexts = ax.pie(
            values,
            labels=apps,
            autopct="%1.1f%%",
            colors=COLORS[:len(apps)],
            pctdistance=0.80,
            startangle=140,
            wedgeprops={"linewidth": 1.5, "edgecolor": "#0F172A"},
        )
        for text in texts:
            text.set_color("#CBD5E1")
            text.set_fontsize(9)
        for at in autotexts:
            at.set_color("white")
            at.set_fontsize(8)

        ax.set_title(
            "Application Usage — Current Session",
            color="#F1F5F9", fontsize=14, pad=20
        )
        fig.tight_layout()
        plt.show()

    threading.Thread(target=_draw, daemon=True).start()


def show_history_chart() -> None:
    """Embed a 7-day bar chart inside the History tab frame."""
    # Remove any previous chart widgets
    for widget in history_chart_frame.winfo_children():
        widget.destroy()

    df = get_history(7)

    if df.empty:
        ctk.CTkLabel(
            history_chart_frame,
            text="No history yet.\nStart tracking to build your history.",
            font=("Segoe UI", 15),
            text_color="#4B5563"
        ).pack(expand=True)
        return

    df["minutes"] = df["total_seconds"] / 60
    df["label"]   = pd.to_datetime(df["date"]).dt.strftime("%a\n%m/%d")
    today_label   = datetime.now().strftime("%a\n%m/%d")

    fig = Figure(figsize=(8, 4), dpi=100)
    fig.patch.set_facecolor("#0F172A")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#0F172A")

    bar_colors = [
        "#8B5CF6" if lbl == today_label else "#3B82F6"
        for lbl in df["label"]
    ]
    bars = ax.bar(df["label"], df["minutes"], color=bar_colors, width=0.55, zorder=3)

    # Value labels on top of each bar
    for bar in bars:
        h = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            h + 0.5,
            f"{h:.0f}m",
            ha="center", va="bottom",
            color="#94A3B8", fontsize=8
        )

    ax.set_ylabel("Minutes", color="#94A3B8", fontsize=10)
    ax.tick_params(colors="#94A3B8", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#1E293B")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.yaxis.grid(True, color="#1E293B", linestyle="--", zorder=0)
    ax.set_title("Last 7 Days — Daily Screen Time", color="#F1F5F9", fontsize=12, pad=12)
    fig.tight_layout()

    canvas = FigureCanvasTkAgg(fig, master=history_chart_frame)
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)


def export_csv() -> None:
    """Export current session data to a timestamped CSV in the project folder."""
    with data_lock:
        snapshot = dict(usage_data)

    if not snapshot:
        messagebox.showwarning("No Data", "Nothing to export.\nStart tracking first.")
        return

    rows = [
        {"Application": name, "Seconds": secs, "Time (HH:MM:SS)": format_time(secs)}
        for name, secs in sorted(snapshot.items(), key=lambda x: x[1], reverse=True)
    ]
    df = pd.DataFrame(rows)

    filename = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"screen_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    try:
        df.to_csv(filename, index=False)
        messagebox.showinfo("Export Complete", f"Report saved to:\n{filename}")
    except Exception as exc:
        messagebox.showerror("Export Failed", str(exc))


def on_close() -> None:
    """Save in-progress data then destroy the window."""
    global tracking
    if tracking:
        tracking = False
        stop_event.set()
        save_to_database()
    root.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

init_database()
usage_data = load_today_data()   # restore today's DB rows into memory

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

root = ctk.CTk()
root.title("Screen Time Tracker")
root.geometry("1100x760")
root.minsize(900, 640)
root.configure(fg_color="#0B1120")

# ─────────────────────────────────────────────────────────────────────────────
#  StringVars
# ─────────────────────────────────────────────────────────────────────────────

session_time_var = ctk.StringVar(value="00:00:00")
total_time_var   = ctk.StringVar(value=format_time(sum(usage_data.values())))
apps_count_var   = ctk.StringVar(value=str(len(usage_data)))
status_var       = ctk.StringVar(value="◼ IDLE")
clock_var        = ctk.StringVar(value="")

# ─────────────────────────────────────────────────────────────────────────────
#  Header
# ─────────────────────────────────────────────────────────────────────────────

header = ctk.CTkFrame(root, fg_color="#0F172A", corner_radius=0, height=68)
header.pack(fill="x")
header.pack_propagate(False)

ctk.CTkLabel(
    header,
    text="⏱  Screen Time Tracker",
    font=("Segoe UI", 22, "bold"),
    text_color="#F1F5F9"
).place(x=24, rely=0.5, anchor="w")

ctk.CTkLabel(
    header,
    textvariable=clock_var,
    font=("Consolas", 15),
    text_color="#94A3B8"
).place(relx=1.0, x=-24, rely=0.5, anchor="e")

# ─────────────────────────────────────────────────────────────────────────────
#  Tab view
# ─────────────────────────────────────────────────────────────────────────────

tabs = ctk.CTkTabview(
    root,
    fg_color="#0B1120",
    segmented_button_fg_color="#0F172A",
    segmented_button_selected_color="#3B82F6",
    segmented_button_selected_hover_color="#2563EB",
    segmented_button_unselected_color="#0F172A",
    segmented_button_unselected_hover_color="#1E293B",
    text_color="#CBD5E1",
    text_color_disabled="#4B5563",
)
tabs.pack(fill="both", expand=True, padx=16, pady=(8, 0))

tab_dashboard = tabs.add("  Dashboard  ")
tab_history   = tabs.add("  History  ")

# ═════════════════════════════════════════════════════════════════════════════
#  DASHBOARD TAB
# ═════════════════════════════════════════════════════════════════════════════

dash = ctk.CTkFrame(tab_dashboard, fg_color="transparent")
dash.pack(fill="both", expand=True)

# ── Status row ───────────────────────────────────────────────────────────────
status_row = ctk.CTkFrame(dash, fg_color="transparent")
status_row.pack(fill="x", padx=4, pady=(4, 6))

status_label = ctk.CTkLabel(
    status_row,
    textvariable=status_var,
    font=("Segoe UI", 12, "bold"),
    text_color="#6B7280"
)
status_label.pack(side="left")

# ── Stat cards ───────────────────────────────────────────────────────────────
cards_row = ctk.CTkFrame(dash, fg_color="transparent")
cards_row.pack(fill="x", padx=4, pady=(0, 10))
cards_row.grid_columnconfigure((0, 1, 2), weight=1)


def _stat_card(parent: ctk.CTkFrame, col: int, icon: str,
               label: str, var: ctk.StringVar, accent: str) -> None:
    card = ctk.CTkFrame(
        parent,
        fg_color="#1E293B",
        corner_radius=14,
        border_width=1,
        border_color="#2D3D55"
    )
    card.grid(row=0, column=col, padx=6, sticky="ew")

    ctk.CTkLabel(card, text=icon, font=("Segoe UI Emoji", 28)).pack(pady=(14, 2))
    ctk.CTkLabel(card, text=label, font=("Segoe UI", 11), text_color="#6B7280").pack()
    ctk.CTkLabel(
        card,
        textvariable=var,
        font=("Consolas", 26, "bold"),
        text_color=accent
    ).pack(pady=(2, 14))


_stat_card(cards_row, 0, "⏱", "Session Time", session_time_var, "#3B82F6")
_stat_card(cards_row, 1, "📅", "Total Today",  total_time_var,  "#8B5CF6")
_stat_card(cards_row, 2, "🖥", "Apps Tracked", apps_count_var,  "#10B981")

# ── Control buttons ──────────────────────────────────────────────────────────
ctrl = ctk.CTkFrame(
    dash,
    fg_color="#0F172A",
    corner_radius=12,
    border_width=1,
    border_color="#1E293B"
)
ctrl.pack(fill="x", padx=4, pady=(0, 10))

BTN_H = 40

start_btn = ctk.CTkButton(
    ctrl, text="▶  Start", width=130, height=BTN_H,
    font=("Segoe UI", 13, "bold"),
    fg_color="#3B82F6", hover_color="#2563EB",
    corner_radius=8, command=start_tracking
)
start_btn.grid(row=0, column=0, padx=10, pady=12)

stop_btn = ctk.CTkButton(
    ctrl, text="■  Stop", width=130, height=BTN_H,
    font=("Segoe UI", 13, "bold"),
    fg_color="#374151", hover_color="#374151",
    text_color="#6B7280",
    corner_radius=8, command=stop_tracking,
    state="disabled"
)
stop_btn.grid(row=0, column=1, padx=10, pady=12)

ctk.CTkButton(
    ctrl, text="📊  Chart", width=130, height=BTN_H,
    font=("Segoe UI", 13, "bold"),
    fg_color="#1E293B", hover_color="#334155",
    corner_radius=8, command=show_chart
).grid(row=0, column=2, padx=10, pady=12)

ctk.CTkButton(
    ctrl, text="💾  Export CSV", width=140, height=BTN_H,
    font=("Segoe UI", 13, "bold"),
    fg_color="#1E293B", hover_color="#334155",
    corner_radius=8, command=export_csv
).grid(row=0, column=3, padx=10, pady=12)

ctk.CTkButton(
    ctrl, text="🗑  Clear", width=110, height=BTN_H,
    font=("Segoe UI", 13, "bold"),
    fg_color="#1E293B", hover_color="#450A0A",
    text_color="#EF4444",
    corner_radius=8, command=clear_session
).grid(row=0, column=4, padx=10, pady=12)

# ── App list header ──────────────────────────────────────────────────────────
list_hdr = ctk.CTkFrame(dash, fg_color="transparent")
list_hdr.pack(fill="x", padx=12, pady=(0, 4))

ctk.CTkLabel(
    list_hdr,
    text=f"Application Usage  —  Top {TOP_N_APPS}",
    font=("Segoe UI", 13, "bold"),
    text_color="#64748B"
).pack(side="left")

# ── Scrollable app list ──────────────────────────────────────────────────────
scroll_frame = ctk.CTkScrollableFrame(
    dash,
    fg_color="#111827",
    corner_radius=12,
    border_width=1,
    border_color="#1E293B",
    scrollbar_button_color="#1E293B",
    scrollbar_button_hover_color="#334155",
)
scroll_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

# ═════════════════════════════════════════════════════════════════════════════
#  HISTORY TAB
# ═════════════════════════════════════════════════════════════════════════════

hist = ctk.CTkFrame(tab_history, fg_color="transparent")
hist.pack(fill="both", expand=True)

hist_ctrl = ctk.CTkFrame(hist, fg_color="transparent")
hist_ctrl.pack(fill="x", padx=4, pady=(8, 4))

ctk.CTkLabel(
    hist_ctrl,
    text="Last 7 Days",
    font=("Segoe UI", 16, "bold"),
    text_color="#94A3B8"
).pack(side="left", padx=8)

ctk.CTkButton(
    hist_ctrl,
    text="🔄  Refresh",
    font=("Segoe UI", 12, "bold"),
    fg_color="#1E293B", hover_color="#334155",
    corner_radius=8, height=34, width=110,
    command=show_history_chart
).pack(side="right", padx=8)

history_chart_frame = ctk.CTkFrame(
    hist,
    fg_color="#0F172A",
    corner_radius=12,
    border_width=1,
    border_color="#1E293B"
)
history_chart_frame.pack(fill="both", expand=True, padx=4, pady=(0, 8))

# ─────────────────────────────────────────────────────────────────────────────
#  Footer
# ─────────────────────────────────────────────────────────────────────────────

footer = ctk.CTkFrame(root, fg_color="#0F172A", corner_radius=0, height=26)
footer.pack(fill="x", side="bottom")
footer.pack_propagate(False)

ctk.CTkLabel(
    footer,
    text=f"Screen Time Tracker  •  Data stored in  {DB_NAME}",
    font=("Segoe UI", 10),
    text_color="#374151"
).pack(side="left", padx=16, pady=5)

# ─────────────────────────────────────────────────────────────────────────────
#  Kick off live updates
# ─────────────────────────────────────────────────────────────────────────────

update_dashboard()          # populate list from loaded DB data
show_history_chart()        # render initial 7-day chart
tick_clock()                # start header clock

root.protocol("WM_DELETE_WINDOW", on_close)
root.mainloop()
