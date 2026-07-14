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
    for app, seconds in usage_data.items()
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

print(get_active_app())