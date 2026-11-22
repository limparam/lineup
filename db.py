"""Хранилище LineUP: SQLite, схема и подключение."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "lineup.db"
BLACKLIST_NAME = "Чёрный список"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_super      INTEGER NOT NULL DEFAULT 0,
    is_promoter   INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at    TEXT,
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

-- Гостевые списки мероприятия. promoter_id — устаревшая привязка,
-- доступ промоутеров хранится в guest_list_promoters (многие ко многим).
CREATE TABLE IF NOT EXISTS guest_lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    promoter_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    is_blacklist INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guest_list_promoters (
    list_id    INTEGER NOT NULL REFERENCES guest_lists(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (list_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_guest_list_promoters_user
    ON guest_list_promoters(user_id, list_id);

-- Публичные ссылки на саморегистрацию гостей.
CREATE TABLE IF NOT EXISTS guest_registration_links (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    list_id           INTEGER NOT NULL REFERENCES guest_lists(id) ON DELETE CASCADE,
    token             TEXT NOT NULL UNIQUE,
    comment           TEXT NOT NULL DEFAULT '',
    max_registrations INTEGER,
    expires_at        TEXT,
    expiry_mode       TEXT NOT NULL DEFAULT 'custom',
    expiry_offset_minutes INTEGER,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registration_links_event
    ON guest_registration_links(event_id, id);
CREATE INDEX IF NOT EXISTS idx_registration_links_token
    ON guest_registration_links(token);

CREATE TABLE IF NOT EXISTS guests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id        INTEGER NOT NULL REFERENCES guest_lists(id) ON DELETE CASCADE,
    event_id       INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL DEFAULT '',
    table_name     TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'invited',
    share_token    TEXT NOT NULL UNIQUE,
    created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    registration_link_id INTEGER REFERENCES guest_registration_links(id)
                         ON DELETE SET NULL,
    checked_in_at  TEXT,
    checked_in_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS door_sync_operations (
    operation_id TEXT PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    guest_id     INTEGER REFERENCES guests(id) ON DELETE SET NULL,
    action       TEXT NOT NULL,
    client_at    TEXT,
    applied_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_door_sync_event
    ON door_sync_operations(event_id, applied_at);

CREATE TABLE IF NOT EXISTS event_door_links (
    event_id   INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    token      TEXT NOT NULL UNIQUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_guest_lists_event ON guest_lists(event_id, position, id);
CREATE INDEX IF NOT EXISTS idx_guest_lists_promoter ON guest_lists(promoter_id);
CREATE INDEX IF NOT EXISTS idx_guests_list ON guests(list_id, status, id);
CREATE INDEX IF NOT EXISTS idx_guests_event ON guests(event_id);
CREATE INDEX IF NOT EXISTS idx_guests_token ON guests(share_token);
CREATE INDEX IF NOT EXISTS idx_door_token ON event_door_links(token);
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
    conn.create_function(
        "casefold", 1, lambda value: (value or "").casefold(),
        deterministic=True,
    )
    conn.create_function(
        "phone_digits", 1,
        lambda value: re.sub(r"\D", "", value or ""),
        deterministic=True,
    )
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
        purge_expired_promoters(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def purge_expired_promoters(conn: sqlite3.Connection, today: str | None = None,
                            keep_username: str | None = None) -> list[str]:
    """Удаляет промоутеров, у которых истёк срок действия учётной записи."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    sql = (
        "SELECT id, username FROM users WHERE is_promoter = 1 "
        "AND expires_at IS NOT NULL AND expires_at < ?"
    )
    args = [today]
    if keep_username:
        sql += " AND username <> ? COLLATE NOCASE"
        args.append(keep_username)
    rows = conn.execute(sql, args).fetchall()
    for row in rows:
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    return [row["username"] for row in rows]


def ensure_blacklist(conn: sqlite3.Connection, event_id: int,
                     created_by: int | None = None) -> int:
    """Гарантирует наличие чёрного списка у мероприятия."""
    rows = conn.execute(
        "SELECT id, name, is_blacklist FROM guest_lists WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    names = {BLACKLIST_NAME.casefold(), "черный список"}
    for row in rows:
        if int(row["is_blacklist"]) or row["name"].strip().casefold() in names:
            if not int(row["is_blacklist"]):
                conn.execute(
                    "UPDATE guest_lists SET is_blacklist = 1 WHERE id = ?",
                    (row["id"],),
                )
            return int(row["id"])
    cur = conn.execute(
        "INSERT INTO guest_lists (event_id, name, created_by, created_at, "
        "position, is_blacklist) "
        "VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 0, 1)",
        (event_id, BLACKLIST_NAME, created_by),
    )
    return int(cur.lastrowid)


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
    if "is_promoter" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN is_promoter INTEGER NOT NULL DEFAULT 0"
        )
    if "created_by" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN created_by INTEGER")
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN expires_at TEXT")
    # Страховка: если главного нет, но пользователи есть — назначаем старейшего.
    no_super = conn.execute(
        "SELECT COUNT(*) FROM users WHERE is_super = 1"
    ).fetchone()[0] == 0
    if no_super:
        conn.execute(
            "UPDATE users SET is_super = 1 "
            "WHERE id = (SELECT MIN(id) FROM users)"
        )

    # Гостевые списки: чёрный список и несколько промоутеров на список.
    list_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(guest_lists)")
    }
    if "is_blacklist" not in list_columns:
        conn.execute(
            "ALTER TABLE guest_lists ADD COLUMN "
            "is_blacklist INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "INSERT OR IGNORE INTO guest_list_promoters (list_id, user_id, created_at) "
        "SELECT id, promoter_id, created_at FROM guest_lists "
        "WHERE promoter_id IS NOT NULL"
    )
    guest_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(guests)")
    }
    if "registration_link_id" not in guest_columns:
        conn.execute(
            "ALTER TABLE guests ADD COLUMN registration_link_id INTEGER"
        )

    # Срок ссылки саморегистрации: относительный или абсолютный.
    link_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(guest_registration_links)")
    }
    if "expiry_mode" not in link_columns:
        conn.execute(
            "ALTER TABLE guest_registration_links ADD COLUMN "
            "expiry_mode TEXT NOT NULL DEFAULT 'custom'"
        )
    if "expiry_offset_minutes" not in link_columns:
        conn.execute(
            "ALTER TABLE guest_registration_links ADD COLUMN "
            "expiry_offset_minutes INTEGER"
        )
    try:
        links = conn.execute(
            "SELECT rl.id, rl.expires_at, e.starts_at "
            "FROM guest_registration_links rl "
            "JOIN events e ON e.id = rl.event_id"
        ).fetchall()
        for link in links:
            if not link["expires_at"]:
                conn.execute(
                    "UPDATE guest_registration_links SET expiry_mode = 'never', "
                    "expiry_offset_minutes = NULL WHERE id = ?",
                    (link["id"],),
                )
                continue
            try:
                expires = datetime.strptime(
                    link["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
                starts = datetime.strptime(
                    link["starts_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            delta = expires - starts
            minutes = int(delta.total_seconds() // 60)
            if (delta == timedelta(minutes=minutes)
                    and minutes in (0, 60, 120, 180)):
                conn.execute(
                    "UPDATE guest_registration_links SET expiry_mode = 'start', "
                    "expiry_offset_minutes = ? WHERE id = ?",
                    (minutes, link["id"]),
                )
    except sqlite3.OperationalError:
        pass

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

    # Чёрный список создаём после появления owner_id у старых мероприятий.
    for event in conn.execute("SELECT id, owner_id FROM events").fetchall():
        ensure_blacklist(conn, event["id"], event["owner_id"])
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_guest_lists_blacklist "
        "ON guest_lists(event_id) WHERE is_blacklist = 1"
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
