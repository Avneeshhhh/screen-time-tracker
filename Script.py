import customtkinter as ctk
import sqlite3
import threading
import time
import psutil
import win32gui
import win32process
from datetime import datetime
import matplotlib.pyplot as plt
import pandas as pd
from tkinter import messagebox

#Database 

conn = sqlite3.connect("tracker.db")

cursor = conn.cursor()

cursor.execute("""
               CREATE TABLE IF NOT EXISTS usage(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               app_name TEXT,
               usage_second INTEGER,
               date TEXT)
               """)

conn.commit()

tracking = False

usage_data = {}
session_seconds = 0

def get_active_app():
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        return process.name()
    except:
        return "Unknown"

def tracking_loop():
    global tracking 

    while tracking:
        app = get_active_app()
        usage_data[app] = usage_data.get(app, 0) + 1
        update_dashboard()
        time.sleep(1)


def save_to_database():
    today = datetime.now().strftime("%Y-%m-%d")
    for app, seconds in usage_data.items():
        cursor.execute("""
INSERT INTO usage(
                   app_name,
                   usage_seconds,
                   date)
                   Values(?,?,?)
""", (app, seconds, today))
    conn.commit()


def update_dashboard():
    pass

def update_live_timer():
    pass

def start_tracking():
    global tracking
    global session_seconds

    if tracking:
        return 
    session_seconds = 0

    tracking = True

    status_label.configure(
        text = "Tracking Running"
    )

    update_live_timer()

    thread = threading.Thread(
        target = tracking_loop,
        daemon = True
    )
    thread.start()
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

app = ctk.CTk()
app.geometry("1100x750")
app.title("Screen Time Tracker")
app.configure(fg_color = "#f3f4f6")

#Title

title = ctk.CTkLabel(
    app,
    text = "Screen Time Dashboard",
    font = ("Arial", 36, "bold"),
    text_color = "#1F2937" 
)

title.pack(pady=25)

# Status

status_label = ctk.CTkLabel(
    app,
    text = "Ready",
    font = ("Arial", 20 , "bold"),
    text_color = "#374151"
)

status_label.pack()

timer_label = ctk.CTkLabel(
    app,
    text = "00:00:00",
    font = ("Consolas", 42, "bold"),
    text_color = "#F97316"
)

timer_label.pack(pady=10)

#Button Frame

button_frame = ctk.CTkFrame(
    app,
    fg_color = "#FFFFFF",
    corner_radius = 15
)

button_frame.pack(pady=25)

#button 

start_btn = ctk.CTkButton(
    button_frame,
    text = "Start Tracking",
    width = 180,
    height = 45,
    font = ("Arial", 16, "bold"),
    command = start_tracking
)

start_btn.grid(
    row = 0, 
    column = 3,
    padx = 12,
    pady = 15
)

stop_btn = ctk.CTkButton(
    button_frame,
    text = "Stop Tracking",
    width = 180,
    height = 45,
    font = ("Arial", 16, "bold"),
    command = stop_tracking
)

stop_btn.grid(
    row = 0, 
    column = 1,
    padx = 12,
    pady = 15
)

chart_btn = ctk.CTkButton(
    button_frame,
    text = "View Chart",
    width = 180,
    height = 45,
    font = ("Arial", 16, "bold"),
    command = show_chart
)

chart_btn.grid(
    row = 0, 
    column = 2,
    padx = 12,
    pady = 15
)

export_btn = ctk.CTkButton(
    button_frame,
    text = "Export CSV",
    width = 180,
    height = 45,
    font = ("Arial", 16, "bold"),
    command = export_report
)

export_btn.grid(
    row = 0, 
    column = 3,
    padx = 12,
    pady = 15
)

app.mainloop()

print(get_active_app())