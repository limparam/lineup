"""Управление LineUP: инициализация базы, админ, демо-данные, dev-сервер.

Примеры:
    python manage.py initdb
    python manage.py createadmin
    python manage.py passwd --username admin
    python manage.py seed-demo
    python manage.py serve --port 8080
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import db
import security
import timeutil
from timeutil import to_iso

log = logging.getLogger("lineup")


# --------------------------------------------------------------------------
# Пользователи
# --------------------------------------------------------------------------

def _ask_password(prompt: str = "Пароль: ") -> str:
    from_env = os.environ.get("LINEUP_ADMIN_PASSWORD")
    if from_env:
        return from_env
    while True:
        first = getpass.getpass(prompt)
        problem = security.validate_password(first)
        if problem:
            print(f"  ✗ {problem}", file=sys.stderr)
            continue
        second = getpass.getpass("Повторите пароль: ")
        if first != second:
            print("  ✗ Пароли не совпадают.", file=sys.stderr)
            continue
        return first


def _ask_username(default: str = "admin") -> str:
    username = os.environ.get("LINEUP_ADMIN_USER") or default
    if os.environ.get("LINEUP_ADMIN_USER"):
        return username.strip()
    value = input(f"Логин [{default}]: ").strip()
    return value or default


def cmd_createadmin(args) -> int:
    db.init()
    username = (args.username or "").strip() or _ask_username()
    password = args.password or _ask_password()
    problem = security.validate_password(password)
    if problem:
        print(f"✗ {problem}", file=sys.stderr)
        return 1
    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if exists:
            print(f"✗ Пользователь «{username}» уже существует. "
                  f"Смените пароль: python manage.py passwd --username {username}",
                  file=sys.stderr)
            return 1
        # Первый администратор автоматически становится главным.
        first = db.user_count(conn) == 0
        is_super = 1 if (first or args.super) else 0
        if args.super and not first:
            conn.execute("UPDATE users SET is_super = 0")
        conn.execute(
            "INSERT INTO users (username, password_hash, is_super, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, security.hash_password(password), is_super,
             timeutil.iso_now()),
        )
        conn.commit()
    finally:
        conn.close()
    if is_super:
        print(f"✓ Главный администратор «{username}» создан.")
    else:
        print(f"✓ Администратор «{username}» создан.")
    return 0


def cmd_passwd(args) -> int:
    db.init()
    username = (args.username or "").strip() or _ask_username()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if not row:
            print(f"✗ Пользователь «{username}» не найден.", file=sys.stderr)
            return 1
        password = args.password or _ask_password("Новый пароль: ")
        problem = security.validate_password(password)
        if problem:
            print(f"✗ {problem}", file=sys.stderr)
            return 1
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (security.hash_password(password), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    print(f"✓ Пароль «{username}» обновлён. Все сессии завершены.")
    return 0


# --------------------------------------------------------------------------
# Демо-данные
# --------------------------------------------------------------------------

def _add_hall(conn, event_id: int, name: str, position: int) -> int:
    cur = conn.execute(
        "INSERT INTO halls (event_id, name, position) VALUES (?, ?, ?)",
        (event_id, name, position),
    )
    return int(cur.lastrowid)


def _add_perf(conn, hall_id: int, performer: str, start: datetime, minutes: int) -> None:
    conn.execute(
        "INSERT INTO performances (hall_id, performer, starts_at, ends_at) "
        "VALUES (?, ?, ?, ?)",
        (hall_id, performer, to_iso(start), to_iso(start + timedelta(minutes=minutes))),
    )


def cmd_seed_demo(args) -> int:
    db.init()
    conn = db.connect()
    created = 0
    try:
        owner = conn.execute(
            "SELECT id FROM users WHERE is_super = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if not owner:
            owner = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        owner_id = owner["id"] if owner else None

        # 1. Мероприятие «прямо сейчас», чтобы сразу увидеть живое выделение.
        if not conn.execute("SELECT 1 FROM events WHERE slug = 'dnb-night'").fetchone():
            now = datetime.now(timezone.utc)
            start = now.replace(second=0, microsecond=0) - timedelta(minutes=90)
            end = start + timedelta(hours=9)
            cur = conn.execute(
                "INSERT INTO events (slug, title, city, venue, address, description, "
                "timezone, starts_at, ends_at, published, owner_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    "dnb-night", "DNB NIGHT", "Москва", "бар Звезда",
                    "ул. Тверская, 12",
                    "Drum & bass до утра: два зала, резидентские сеты "
                    "и специальные гости.",
                    "Europe/Moscow", to_iso(start), to_iso(end),
                    owner_id, timeutil.iso_now(),
                ),
            )
            event_id = int(cur.lastrowid)
            hall1 = _add_hall(conn, event_id, "Зал 1", 1)
            hall2 = _add_hall(conn, event_id, "Зал 2", 2)
            _add_perf(conn, hall1, "DJ Vik", start, 60)
            _add_perf(conn, hall1, "DJ Smash", start + timedelta(minutes=60), 60)
            _add_perf(conn, hall1, "DJ Valera", start + timedelta(minutes=120), 90)
            _add_perf(conn, hall1, "MC Lira", start + timedelta(minutes=210), 60)
            _add_perf(conn, hall2, "DJ Rob", start, 90)
            _add_perf(conn, hall2, "Misterio", start + timedelta(minutes=90), 60)
            _add_perf(conn, hall2, "Nebula", start + timedelta(minutes=150), 120)
            created += 1

        # 2. Мероприятие с датами из примера — проверка «будущего» состояния.
        if not conn.execute("SELECT 1 FROM events WHERE slug = 'techno-room'").fetchone():
            tz = "Europe/Moscow"
            start = timeutil.parse_local("2027-11-04T20:00", tz)
            end = timeutil.parse_local("2027-11-05T06:00", tz)
            cur = conn.execute(
                "INSERT INTO events (slug, title, city, venue, address, description, "
                "timezone, starts_at, ends_at, published, owner_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    "techno-room", "TECHNO ROOM", "Санкт-Петербург",
                    "клуб Космонавт", "наб. Обводного канала, 24",
                    "Ночь техно на двух танцполах.", tz,
                    to_iso(start), to_iso(end), owner_id, timeutil.iso_now(),
                ),
            )
            event_id = int(cur.lastrowid)
            hall1 = _add_hall(conn, event_id, "Зал 1", 1)
            hall2 = _add_hall(conn, event_id, "Зал 2", 2)
            base = timeutil.parse_local("2027-11-04T20:00", tz)
            _add_perf(conn, hall1, "DJ Proton", base, 120)
            _add_perf(conn, hall1, "Mira", base + timedelta(hours=2), 120)
            _add_perf(conn, hall1, "KODE", base + timedelta(hours=4), 180)
            _add_perf(conn, hall2, "RIFT", base, 90)
            _add_perf(conn, hall2, "Zvuk", base + timedelta(minutes=90), 150)
            _add_perf(conn, hall2, "NOVA", base + timedelta(hours=4), 240)
            created += 1
        conn.commit()
    finally:
        conn.close()
    if created:
        print(f"✓ Демо-данные добавлены ({created} мероприятия).")
    else:
        print("• Демо-мероприятия уже существуют, ничего не добавлено.")
    return 0


# --------------------------------------------------------------------------
# Прочее
# --------------------------------------------------------------------------

def cmd_initdb(args) -> int:
    db.init()
    print(f"✓ База готова: {db.db_path()}")
    return 0


def cmd_list(args) -> int:
    db.init()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY starts_at").fetchall()
        if not rows:
            print("Мероприятий нет.")
            return 0
        for row in rows:
            mark = " " if row["published"] else "×"
            print(f"{mark} #{row['id']:<3} {row['title']} — "
                  f"{timeutil.human_range(row['starts_at'], row['ends_at'], row['timezone'])}"
                  f" ({row['city']}, {row['venue']}) /e/{row['slug']}")
    finally:
        conn.close()
    return 0


def cmd_serve(args) -> int:
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
    from socketserver import ThreadingMixIn

    import app as application

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, fmt, *a):  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % a)

    db.init()
    httpd = make_server(args.host, args.port, application.app,
                        server_class=ThreadingWSGIServer,
                        handler_class=QuietHandler)
    print(f"LineUP запущен: http://{args.host}:{args.port}  (админка: /admin)")
    print("Остановка — Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Управление сайтом LineUP")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("initdb", help="создать таблицы")
    p.set_defaults(func=cmd_initdb)

    p = sub.add_parser("createadmin", help="создать администратора")
    p.add_argument("--username")
    p.add_argument("--password", help="необязательно; иначе запросит интерактивно")
    p.add_argument("--super", action="store_true",
                   help="сделать главным администратором")
    p.set_defaults(func=cmd_createadmin)

    p = sub.add_parser("passwd", help="сменить пароль администратора")
    p.add_argument("--username")
    p.add_argument("--password")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("seed-demo", help="добавить демо-мероприятия")
    p.set_defaults(func=cmd_seed_demo)

    p = sub.add_parser("list", help="список мероприятий")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("serve", help="dev-сервер")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
