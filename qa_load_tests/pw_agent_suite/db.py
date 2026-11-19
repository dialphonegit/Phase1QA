import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("/tmp/pw_agent_suite.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    extension  TEXT PRIMARY KEY,
    email      TEXT,
    name       TEXT,
    status     TEXT DEFAULT 'offline',
    answered   INTEGER DEFAULT 0,
    missed     INTEGER DEFAULT 0,
    total_dur  INTEGER DEFAULT 0,
    last_seen  REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_ext   TEXT,
    caller_id   TEXT DEFAULT '',
    queue_ext   TEXT DEFAULT '',
    ring_at     REAL,
    answer_at   REAL,
    end_at      REAL,
    duration    INTEGER DEFAULT 0,
    outcome     TEXT   -- answered | missed | error
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL DEFAULT (strftime('%s','now')),
    level     TEXT DEFAULT 'info',
    agent_ext TEXT,
    msg       TEXT
);
"""

@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init():
    with _conn() as c:
        c.executescript(_SCHEMA)

def upsert_agent(ext, email, name):
    with _conn() as c:
        c.execute("""INSERT INTO agents(extension,email,name) VALUES(?,?,?)
                     ON CONFLICT(extension) DO UPDATE SET email=excluded.email, name=excluded.name""",
                  (ext, email, name))

def set_status(ext, status):
    with _conn() as c:
        c.execute("UPDATE agents SET status=?,last_seen=? WHERE extension=?",
                  (status, time.time(), ext))

def record_call(agent_ext, ring_at, answer_at, end_at, outcome, caller_id="", queue_ext=""):
    dur = int(end_at - answer_at) if answer_at and end_at else 0
    with _conn() as c:
        c.execute("""INSERT INTO calls(agent_ext,caller_id,queue_ext,ring_at,answer_at,end_at,duration,outcome)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (agent_ext, caller_id, queue_ext, ring_at, answer_at, end_at, dur, outcome))
        if outcome == "answered":
            c.execute("UPDATE agents SET answered=answered+1,total_dur=total_dur+? WHERE extension=?",
                      (dur, agent_ext))
        elif outcome == "missed":
            c.execute("UPDATE agents SET missed=missed+1 WHERE extension=?", (agent_ext,))

def log(level, agent_ext, msg):
    with _conn() as c:
        c.execute("INSERT INTO events(level,agent_ext,msg) VALUES(?,?,?)", (level, agent_ext, msg))

def stats():
    with _conn() as c:
        agents   = [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY answered DESC").fetchall()]
        summary  = dict(c.execute("""
            SELECT COUNT(*) total, SUM(CASE WHEN outcome='answered' THEN 1 ELSE 0 END) answered,
                   SUM(CASE WHEN outcome='missed'   THEN 1 ELSE 0 END) missed,
                   CAST(AVG(CASE WHEN outcome='answered' THEN duration END) AS INT) avg_dur,
                   SUM(duration) total_dur
            FROM calls""").fetchone())
        recent   = [dict(r) for r in c.execute("""
            SELECT c.*,a.name agent_name FROM calls c LEFT JOIN agents a ON c.agent_ext=a.extension
            ORDER BY c.ring_at DESC LIMIT 100""").fetchall()]
        timeline = [dict(r) for r in c.execute("""
            SELECT strftime('%H:%M',ring_at,'unixepoch') bucket,
                   COUNT(*) total,
                   SUM(CASE WHEN outcome='answered' THEN 1 ELSE 0 END) answered
            FROM calls GROUP BY bucket ORDER BY bucket""").fetchall()]
        return dict(agents=agents, summary=summary, recent=recent, timeline=timeline)
