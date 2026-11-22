"""SQLite 存储层。

表结构：
- sample_groups       样组（登记后固定）：读数总和、独立测量方差之和
- group_blank_usages  样组对各空白样的使用次数（每次测量只用一份空白样，
                      因此一个样组的使用次数之和即其测量总数）
- blanks              空白样：扣减值与方差，可另行登记、更正
- summaries           汇总结果快照（生成时的计算结果）
- summary_groups      汇总实际收录的样组（已去重）
- summary_blanks      汇总生成时采用的空白样数据快照（查旧汇总时原样返回）
"""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_groups (
    group_id     TEXT PRIMARY KEY,
    analyte      TEXT NOT NULL,
    unit         TEXT NOT NULL,
    reading_sum  REAL NOT NULL,
    variance_sum REAL NOT NULL CHECK (variance_sum >= 0),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_blank_usages (
    group_id    TEXT NOT NULL REFERENCES sample_groups (group_id),
    blank_id    TEXT NOT NULL,
    usage_count INTEGER NOT NULL CHECK (usage_count > 0),
    PRIMARY KEY (group_id, blank_id)
);

CREATE TABLE IF NOT EXISTS blanks (
    blank_id   TEXT PRIMARY KEY,
    deduction  REAL NOT NULL,
    variance   REAL NOT NULL CHECK (variance >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analyte               TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    total_measurements    INTEGER NOT NULL,
    reading_sum           REAL NOT NULL,
    variance_sum          REAL NOT NULL,
    blank_deduction_total REAL NOT NULL,
    corrected_mean        REAL NOT NULL,
    standard_error        REAL NOT NULL,
    source_group_ids      TEXT NOT NULL,
    source_summary_ids    TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_groups (
    summary_id INTEGER NOT NULL REFERENCES summaries (summary_id),
    group_id   TEXT NOT NULL,
    PRIMARY KEY (summary_id, group_id)
);

CREATE TABLE IF NOT EXISTS summary_blanks (
    summary_id  INTEGER NOT NULL REFERENCES summaries (summary_id),
    blank_id    TEXT NOT NULL,
    usage_count INTEGER NOT NULL,
    deduction   REAL NOT NULL,
    variance    REAL NOT NULL,
    PRIMARY KEY (summary_id, blank_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
