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