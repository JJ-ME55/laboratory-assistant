"""
db.py — Persistent storage for settings, admins, images and stats
"""

import sqlite3
import json
import os
from config import DEFAULT_SETTINGS, SUPER_ADMIN_ID, ACTIVE_CHAT_ID

DB_PATH = os.path.join(os.path.dirname(__file__), "lab_assistant.db")

def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS images (
            event_type TEXT PRIMARY KEY,
            file_id TEXT,
            file_type TEXT DEFAULT 'photo',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            total_sol REAL DEFAULT 0,
            PRIMARY KEY (date, event_type)
        )
    """)

    for key, value in DEFAULT_SETTINGS.items():
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )

    # Always sync chat_id from config
    c.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("chat_id", json.dumps(ACTIVE_CHAT_ID))
    )

    c.execute(
        "INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
        (SUPER_ADMIN_ID, "super_admin", SUPER_ADMIN_ID)
    )

    conn.commit()
    conn.close()

# ============================================================
# SETTINGS
# ============================================================
def get_setting(key):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return DEFAULT_SETTINGS.get(key)

def set_setting(key, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()
    conn.close()

def get_all_settings():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    rows = c.fetchall()
    conn.close()
    return {k: json.loads(v) for k, v in rows}

# ============================================================
# ADMINS
# ============================================================
def is_admin(user_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    result = c.fetchone() is not None
    conn.close()
    return result

def add_admin(user_id: int, username: str, added_by: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
        (user_id, username or "unknown", added_by)
    )
    conn.commit()
    conn.close()

def remove_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return False
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_all_admins():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at")
    rows = c.fetchall()
    conn.close()
    return rows

# ============================================================
# IMAGES
# ============================================================
def get_image(event_type: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT file_id, file_type FROM images WHERE event_type = ?", (event_type,))
    row = c.fetchone()
    conn.close()
    return row

def set_image(event_type: str, file_id: str, file_type: str = "photo"):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO images (event_type, file_id, file_type, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    """, (event_type, file_id, file_type))
    conn.commit()
    conn.close()

def get_all_images():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT event_type, file_type, updated_at FROM images")
    rows = c.fetchall()
    conn.close()
    return rows

# ============================================================
# STATS
# ============================================================
def increment_stat(event_type: str, sol_amount: float = 0):
    from datetime import date
    today = date.today().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO stats (date, event_type, count, total_sol)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(date, event_type) DO UPDATE SET
            count = count + 1,
            total_sol = total_sol + excluded.total_sol
    """, (today, event_type, sol_amount))
    conn.commit()
    conn.close()

def get_stats_today():
    from datetime import date
    today = date.today().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT event_type, count, total_sol FROM stats WHERE date = ?", (today,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_stats_all_time():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT event_type, SUM(count) as total_count, SUM(total_sol) as total_sol
        FROM stats GROUP BY event_type
    """)
    rows = c.fetchall()
    conn.close()
    return rows
