"""The SQLite index: derived from the transcripts (and, from M2, the vault), so it
can be deleted and rebuilt at any time (brain reindex)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from brain.config import private_dir

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- One row per transcripts/<day>.jsonl read: what was there the last time.
CREATE TABLE IF NOT EXISTS day_files(
  day TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, synced_at TEXT NOT NULL);

-- The paragraphs of the JSONL, one per line. pk is an explicit INTEGER PRIMARY
-- KEY so that the FTS rowids survive VACUUM.
CREATE TABLE IF NOT EXISTS paragraphs(
  pk INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  day TEXT NOT NULL, session TEXT NOT NULL, ms INTEGER NOT NULL,
  start TEXT NOT NULL, end TEXT NOT NULL, start_ts INTEGER NOT NULL,
  speaker TEXT, person TEXT, via_kind TEXT, via_app TEXT,
  place_name TEXT, lat REAL, lon REAL, language TEXT,
  text TEXT NOT NULL,
  -- me: said by the user; said: by others (in the room, in a call); heard: media.
  channel TEXT NOT NULL CHECK(channel IN ('me', 'said', 'heard')),
  weight REAL NOT NULL,
  row_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS paragraphs_day ON paragraphs(day);
CREATE INDEX IF NOT EXISTS paragraphs_session ON paragraphs(session, ms);
CREATE INDEX IF NOT EXISTS paragraphs_person ON paragraphs(person, start_ts);
CREATE INDEX IF NOT EXISTS paragraphs_ts ON paragraphs(start_ts);

-- Words (accents ignored) and, for "did I say this?", substrings.
CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_fts USING fts5(
  text, content='paragraphs', content_rowid='pk', tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_tri USING fts5(
  text, content='paragraphs', content_rowid='pk', tokenize='trigram remove_diacritics 1');
CREATE TRIGGER IF NOT EXISTS paragraphs_ai AFTER INSERT ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(rowid, text) VALUES (new.pk, new.text);
  INSERT INTO paragraphs_tri(rowid, text) VALUES (new.pk, new.text);
END;
CREATE TRIGGER IF NOT EXISTS paragraphs_ad AFTER DELETE ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(paragraphs_fts, rowid, text) VALUES ('delete', old.pk, old.text);
  INSERT INTO paragraphs_tri(paragraphs_tri, rowid, text) VALUES ('delete', old.pk, old.text);
END;
CREATE TRIGGER IF NOT EXISTS paragraphs_au AFTER UPDATE OF text ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(paragraphs_fts, rowid, text) VALUES ('delete', old.pk, old.text);
  INSERT INTO paragraphs_tri(paragraphs_tri, rowid, text) VALUES ('delete', old.pk, old.text);
  INSERT INTO paragraphs_fts(rowid, text) VALUES (new.pk, new.text);
  INSERT INTO paragraphs_tri(rowid, text) VALUES (new.pk, new.text);
END;

-- Sessions, recomputed from their paragraphs whenever one of them changes.
-- summary_status: stale until summarized (M2) for the current content_hash.
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, day TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  start_ts INTEGER NOT NULL, place TEXT, kinds TEXT NOT NULL, speakers TEXT NOT NULL,
  persons TEXT NOT NULL, n_paragraphs INTEGER NOT NULL, content_hash TEXT NOT NULL,
  summary_status TEXT NOT NULL DEFAULT 'stale'
    CHECK(summary_status IN ('stale', 'queued', 'done', 'failed')),
  summarized_hash TEXT);
CREATE INDEX IF NOT EXISTS sessions_day ON sessions(day);

-- What the model extracted from a session (M2). Also written to the vault note,
-- which is the source of truth: brain reindex reads it back from there.
CREATE TABLE IF NOT EXISTS summaries(
  session TEXT PRIMARY KEY, title TEXT NOT NULL, quality TEXT NOT NULL, data TEXT NOT NULL,
  model TEXT, prompt_v INTEGER, content_hash TEXT, created TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
  session UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
-- id: stable across re-summarizing (session + the paragraph where it was said),
-- so the status you tick in commitments.md stays with it.
CREATE TABLE IF NOT EXISTS commitments(
  id TEXT PRIMARY KEY, session TEXT NOT NULL, day TEXT NOT NULL,
  owner TEXT, owner_label TEXT, counterpart TEXT, counterpart_label TEXT,
  what TEXT NOT NULL, due TEXT, due_text TEXT, refs TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS commitments_session ON commitments(session);
CREATE TABLE IF NOT EXISTS commitment_status(
  id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('open', 'done', 'dropped')), updated TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY, session TEXT NOT NULL, day TEXT NOT NULL, person TEXT, about_label TEXT,
  fact TEXT NOT NULL, refs TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS facts_person ON facts(person);
CREATE TABLE IF NOT EXISTS topics(
  session TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL, refs TEXT NOT NULL,
  PRIMARY KEY(session, slug));
CREATE INDEX IF NOT EXISTS topics_slug ON topics(slug);

-- "Chi è": a few sentences per person written by the model from their facts.
CREATE TABLE IF NOT EXISTS profiles(
  person TEXT PRIMARY KEY, text TEXT NOT NULL, facts INTEGER NOT NULL, created TEXT NOT NULL);

-- Conversations with brain, from the iPhone, the web page or the terminal (M6).
-- Also written to the vault (chats/), the source of truth: reindex reads them back.
CREATE TABLE IF NOT EXISTS chats(
  id TEXT PRIMARY KEY, origin TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY, chat TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
  content TEXT NOT NULL, citations TEXT, state TEXT NOT NULL DEFAULT 'done', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_messages_chat ON chat_messages(chat, id);

-- You and the named people, from `omy people list`.
CREATE TABLE IF NOT EXISTS people(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, minutes REAL, sessions INTEGER,
  last_seen TEXT, synced_at TEXT NOT NULL);
"""


def connect(path: Path, threads: bool = False) -> sqlite3.Connection:
    """threads: the connection is created here and used by one other thread (the worker)."""
    private_dir(path.parent)
    if not path.exists():
        # Created 600 before SQLite opens it (the umask may be wider when imported).
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=30, check_same_thread=not threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
