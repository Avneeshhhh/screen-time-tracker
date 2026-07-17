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

app.mainloop()

print(get_active_app())