"""Хранилище LineUP: SQLite, схема и подключение."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "lineup.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_super      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ip       TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    city              TEXT NOT NULL DEFAULT '',
    venue             TEXT NOT NULL,
    address           TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    timezone          TEXT NOT NULL,
    starts_at         TEXT NOT NULL,
    ends_at           TEXT NOT NULL,
    published         INTEGER NOT NULL DEFAULT 1,
    owner_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    lineup_visible    INTEGER NOT NULL DEFAULT 1,
    lineup_publish_at TEXT,
    lineup_filter     TEXT NOT NULL DEFAULT 'all',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_links (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    title    TEXT NOT NULL DEFAULT '',
    url      TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS halls (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS performances (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    hall_id        INTEGER NOT NULL REFERENCES halls(id) ON DELETE CASCADE,
    performer      TEXT NOT NULL,
    starts_at      TEXT NOT NULL,
    ends_at        TEXT NOT NULL,
    -- 1 = слот отдан замене: исходное имя не публикуется, вместо него
    -- показывается replacement_performer (если замена уже найдена).
    is_replacement INTEGER NOT NULL DEFAULT 0,
    replacement_performer TEXT
);

CREATE TABLE IF NOT EXISTS performers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name       TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL,
    UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER,
    actor_name TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL DEFAULT '',
    entity_id  INTEGER,
    event_id   INTEGER,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Кнопки в подвале публичных страниц (управляет главный администратор).
CREATE TABLE IF NOT EXISTS site_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Настройки сайта: PNG-логотип и другие данные в формате ключ-значение.
CREATE TABLE IF NOT EXISTS site_settings (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_starts   ON events(starts_at);
CREATE INDEX IF NOT EXISTS idx_links_event     ON event_links(event_id, position);
CREATE INDEX IF NOT EXISTS idx_halls_event     ON halls(event_id, position);
CREATE INDEX IF NOT EXISTS idx_perf_hall_start ON performances(hall_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_perf_performer  ON performances(performer COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_performers_name ON performers(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_performers_owner ON performers(owner_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_sessions_exp    ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_attempts_ts     ON login_attempts(ts);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_event     ON audit_log(event_id);
CREATE INDEX IF NOT EXISTS idx_site_links_pos  ON site_links(position, id);
"""


def db_path() -> Path:
    raw = os.environ.get("LINEUP_DB", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init(conn: sqlite3.Connection | None = None) -> None:
    """Создаёт таблицы. Идемпотентно."""
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняет схему старых баз до актуальной."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "is_super" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0"
        )
        # Главным становится самый первый администратор.
        conn.execute(
            "UPDATE users SET is_super = 1 "
            "WHERE id = (SELECT MIN(id) FROM users)"
        )
    # Страховка: если главного нет, но пользователи есть — назначаем старейшего.
    no_super = conn.execute(
        "SELECT COUNT(*) FROM users WHERE is_super = 1"
    ).fetchone()[0] == 0
    if no_super:
        conn.execute(
            "UPDATE users SET is_super = 1 "
            "WHERE id = (SELECT MIN(id) FROM users)"
        )

    # Владелец мероприятия и видимость лайнапа.
    event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "owner_id" not in event_columns:
        conn.execute("ALTER TABLE events ADD COLUMN owner_id INTEGER")
    if "lineup_visible" not in event_columns:
        conn.execute(
            "ALTER TABLE events ADD COLUMN lineup_visible INTEGER NOT NULL DEFAULT 1"
        )
    if "lineup_publish_at" not in event_columns:
        conn.execute("ALTER TABLE events ADD COLUMN lineup_publish_at TEXT")
    if "lineup_filter" not in event_columns:
        conn.execute(
            "ALTER TABLE events ADD COLUMN lineup_filter TEXT NOT NULL DEFAULT 'all'"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_owner ON events(owner_id)")

    # Старые мероприятия без владельца передаём старейшему главному администратору.
    owner = conn.execute(
        "SELECT id FROM users WHERE is_super = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if owner:
        conn.execute(
            "UPDATE events SET owner_id = ? WHERE owner_id IS NULL", (owner["id"],)
        )

    # Слоты с заменой: имя артиста не публикуется и не попадает в подсказки.
    perf_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(performances)")}
    if "is_replacement" not in perf_columns:
        conn.execute(
            "ALTER TABLE performances ADD COLUMN "
            "is_replacement INTEGER NOT NULL DEFAULT 0"
        )
    if "replacement_performer" not in perf_columns:
        conn.execute(
            "ALTER TABLE performances ADD COLUMN replacement_performer TEXT"
        )

    # База исполнителей: сохраняем имена из уже существующих выступлений,
    # чтобы автозаполнение работало и после удаления старых сетов.
    conn.execute(
        "INSERT OR IGNORE INTO performers (owner_id, name, created_at) "
        "SELECT e.owner_id, TRIM(p.performer), MIN(e.created_at) "
        "FROM performances p "
        "JOIN halls h ON h.id = p.hall_id "
        "JOIN events e ON e.id = h.event_id "
        "WHERE TRIM(p.performer) <> '' AND e.owner_id IS NOT NULL "
        "AND p.is_replacement = 0 "
        "GROUP BY e.owner_id, TRIM(p.performer) COLLATE NOCASE"
    )


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
