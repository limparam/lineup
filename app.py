"""LineUP — WSGI-приложение: публичный сайт, API и защищённая админка.

Запуск в разработке:  python manage.py serve
В продакшене:         gunicorn -w 3 -b 127.0.0.1:8000 app:app  (за nginx)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import time
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote

import db
import security
import timeutil
from timeutil import TimeError

log = logging.getLogger("lineup")

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = Path(os.environ.get("LINEUP_PUBLIC_DIR") or (BASE_DIR / "public")).resolve()


def _auto_init() -> None:
    """Создаёт схему при старте, чтобы приложение пережило первый запуск."""
    if os.environ.get("LINEUP_AUTO_INIT", "1").strip().lower() in ("0", "false", "no"):
        return
    try:
        db.init()
    except Exception:  # noqa: BLE001 — не роняем воркер, ошибка будет видна в логах
        log.exception("Не удалось инициализировать базу данных")


_auto_init()

COOKIE_NAME = "lineup_session"
SESSION_HOURS = int(os.environ.get("LINEUP_SESSION_HOURS", "12") or 12)
LOGIN_MAX_FAILURES = int(os.environ.get("LINEUP_LOGIN_MAX_FAILURES", "10") or 10)
LOGIN_MAX_USER_FAILURES = int(os.environ.get("LINEUP_LOGIN_MAX_USER_FAILURES", "20") or 20)
LOGIN_WINDOW_SECONDS = int(os.environ.get("LINEUP_LOGIN_WINDOW", "900") or 900)
MAX_BODY_BYTES = 256 * 1024

# Через сколько дней после окончания мероприятие перестаёт быть публичным.
PUBLIC_RETENTION_DAYS = int(
    os.environ.get("LINEUP_PUBLIC_RETENTION_DAYS", "7") or 7)

ASSET_CACHE = os.environ.get("LINEUP_ASSET_CACHE", "public, max-age=86400")

TRUST_PROXY = os.environ.get("LINEUP_TRUST_PROXY", "1") not in ("0", "false", "no")
COOKIE_SECURE_SETTING = os.environ.get("LINEUP_COOKIE_SECURE", "auto").strip().lower()

TIMEZONES = [
    "Europe/Kaliningrad", "Europe/Moscow", "Europe/Minsk", "Europe/Kiev",
    "Europe/Chisinau", "Europe/Riga", "Europe/Vilnius", "Europe/Tallinn",
    "Europe/Warsaw", "Europe/Berlin", "Europe/Paris", "Europe/London",
    "Asia/Yerevan", "Asia/Tbilisi", "Asia/Baku", "Asia/Almaty",
    "Asia/Tashkent", "Asia/Bishkek", "Asia/Yekaterinburg", "Asia/Omsk",
    "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
    "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka", "UTC",
]

CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'self'"
)

_IP_RE = re.compile(r"[0-9a-fA-F:.]{2,45}")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


# --------------------------------------------------------------------------
# Ошибки и HTTP-примитивы
# --------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class Response:
    def __init__(self, body: bytes = b"", status: int = 200,
                 content_type: str = "text/plain; charset=utf-8",
                 headers: dict | None = None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.headers.setdefault("Content-Type", content_type)

    def emit(self, environ, start_response):
        body = b"" if environ.get("REQUEST_METHOD") == "HEAD" else self.body
        headers = list(self.headers.items())
        if "Content-Length" not in self.headers:
            headers.append(("Content-Length", str(len(self.body))))
        start_response(status_line(self.status), headers)
        return [body]


REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    429: "Too Many Requests", 500: "Internal Server Error",
}


def status_line(code: int, custom: str | None = None) -> str:
    return f"{code} {custom or REASONS.get(code, 'Unknown')}"


class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        self.path = path
        self.query = environ.get("QUERY_STRING", "")
        self.headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                name = key[len("HTTP_"):].replace("_", "-").lower()
            elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                name = key.lower().replace("_", "-")
            else:
                continue
            self.headers[name] = value
        self.cookies = {}
        raw_cookie = environ.get("HTTP_COOKIE")
        if raw_cookie:
            jar = SimpleCookie()
            try:
                jar.load(raw_cookie)
                self.cookies = {k: morsel.value for k, morsel in jar.items()}
            except Exception:
                self.cookies = {}
        self._body: bytes | None = None
        self.remote_addr = self._client_ip()

    def _client_ip(self) -> str:
        if TRUST_PROXY:
            forwarded = self.headers.get("x-forwarded-for", "")
            if forwarded:
                # Берём последний адрес: его добавляет наш nginx, всё левее
                # мог прислать клиент.
                candidate = forwarded.split(",")[-1].strip()
                if _IP_RE.fullmatch(candidate):
                    return candidate
        addr = self.environ.get("REMOTE_ADDR") or "0.0.0.0"
        return addr if _IP_RE.fullmatch(addr) else "0.0.0.0"

    @property
    def scheme(self) -> str:
        if TRUST_PROXY:
            proto = self.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            if proto in ("http", "https"):
                return proto
        return self.environ.get("wsgi.url_scheme", "http")

    @property
    def host(self) -> str:
        if TRUST_PROXY:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host")
        else:
            host = self.headers.get("host")
        return (host or "localhost").split(",")[0].strip()

    def body(self) -> bytes:
        if self._body is not None:
            return self._body
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            raise APIError(413, "too_large", "Слишком большой запрос.")
        stream = self.environ.get("wsgi.input")
        self._body = stream.read(length) if length > 0 and stream else b""
        return self._body

    def json(self) -> dict:
        raw = self.body()
        if not raw:
            raise APIError(400, "validation", "Пустой запрос.")
        ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise APIError(415, "unsupported_media_type", "Ожидается application/json.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise APIError(400, "validation", "Некорректный JSON.") from None
        if not isinstance(data, dict):
            raise APIError(400, "validation", "Ожидается JSON-объект.")
        return data


def json_response(data, status: int = 200, headers: dict | None = None) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store"}
    hdrs.update(headers or {})
    return Response(body, status, headers=hdrs)


def error_response(err: APIError) -> Response:
    return json_response(
        {"error": {"code": err.code, "message": err.message}},
        err.status, err.headers,
    )


# --------------------------------------------------------------------------
# Маршрутизация
# --------------------------------------------------------------------------

ROUTES = []


def route(pattern: str, methods=("GET",)):
    compiled = re.compile("^" + pattern + "$")

    def decorator(fn):
        ROUTES.append((compiled, {m.upper() for m in methods}, fn))
        return fn
    return decorator


def dispatch(req: Request) -> Response:
    allowed = set()
    for pattern, methods, handler in ROUTES:
        match = pattern.match(req.path)
        if not match:
            continue
        allowed |= methods
        if req.method in methods or (req.method == "HEAD" and "GET" in methods):
            return handler(req, *match.groups())
    if allowed:
        raise APIError(405, "method_not_allowed", "Метод не поддерживается.",
                       {"Allow": ", ".join(sorted(allowed))})
    raise APIError(404, "not_found", "Страница не найдена.")


# --------------------------------------------------------------------------
# Утилиты
# --------------------------------------------------------------------------

def slugify(title: str) -> str:
    text = title.strip().lower()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = _SLUG_STRIP.sub("-", text).strip("-")
    return text[:60] or "event"


def unique_slug(conn: sqlite3.Connection, title: str) -> str:
    base = slugify(title)
    candidate = base
    n = 2
    while conn.execute("SELECT 1 FROM events WHERE slug = ?", (candidate,)).fetchone():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _str_field(data: dict, key: str, label: str, *, maxlen: int,
               required: bool = True, current: str = "") -> str:
    if key not in data:
        value = current
    else:
        value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise APIError(400, "validation", f"Поле «{label}» должно быть строкой.")
    value = value.strip()
    if required and not value:
        raise APIError(400, "validation", f"Укажите {label.lower()}.")
    if len(value) > maxlen:
        raise APIError(400, "validation", f"Поле «{label}» длиннее {maxlen} символов.")
    return value


def _bool_field(data: dict, key: str, default: bool) -> bool:
    if key not in data:
        return default
    value = data.get(key)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise APIError(400, "validation", "Некорректное значение флага публикации.")


def _event_row(conn, event_id: int):
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        raise APIError(404, "not_found", "Мероприятие не найдено.")
    return row


def _hall_row(conn, hall_id: int):
    row = conn.execute(
        "SELECT h.*, e.starts_at AS event_starts, e.ends_at AS event_ends, "
        "e.timezone AS event_timezone "
        "FROM halls h JOIN events e ON e.id = h.event_id WHERE h.id = ?",
        (hall_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Зал не найден.")
    return row


def _perf_row(conn, perf_id: int):
    row = conn.execute(
        "SELECT p.*, h.event_id AS event_id, e.timezone AS event_timezone "
        "FROM performances p JOIN halls h ON h.id = p.hall_id "
        "JOIN events e ON e.id = h.event_id WHERE p.id = ?",
        (perf_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Выступление не найдено.")
    return row


def _perf_json(row) -> dict:
    return {
        "id": row["id"],
        "performer": row["performer"],
        "startsAt": row["starts_at"],
        "endsAt": row["ends_at"],
    }


def _perf_admin_json(row, tz: str) -> dict:
    data = _perf_json(row)
    data["startsAtLocal"] = timeutil.to_local_input(timeutil.parse_iso(row["starts_at"]), tz)
    data["endsAtLocal"] = timeutil.to_local_input(timeutil.parse_iso(row["ends_at"]), tz)
    return data


MAX_EVENT_LINKS = 12

MAX_SITE_LINKS = 8
SITE_LOGO_KEY = "logo_png"
# 180 КБ в base64 занимают меньше 256 КБ, лимита тела запроса.
MAX_LOGO_BYTES = 180 * 1024
MAX_LOGO_SIDE = 5000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

LINEUP_FILTERS = ("all", "live", "played")


def _lineup_filter(row) -> str:
    """Жёстко выбранный фильтр расписания; неизвестное значение — «все»."""
    value = row["lineup_filter"] if "lineup_filter" in row.keys() else "all"
    return value if value in LINEUP_FILTERS else "all"


def _event_json(conn, row, *, admin: bool = False) -> dict:
    lineup_visible = _lineup_is_visible(row)
    show_lineup = admin or lineup_visible

    hall_items = []
    total = 0
    if show_lineup:
        halls = conn.execute(
            "SELECT * FROM halls WHERE event_id = ? ORDER BY position, id", (row["id"],)
        ).fetchall()
        for hall in halls:
            perfs = conn.execute(
                "SELECT * FROM performances WHERE hall_id = ? ORDER BY starts_at, id",
                (hall["id"],),
            ).fetchall()
            total += len(perfs)
            if admin:
                perf_items = [_perf_admin_json(p, row["timezone"]) for p in perfs]
            else:
                perf_items = [_perf_json(p) for p in perfs]
            hall_items.append({
                "id": hall["id"],
                "name": hall["name"],
                "position": hall["position"],
                "performances": perf_items,
            })

    link_rows = conn.execute(
        "SELECT id, title, url FROM event_links WHERE event_id = ? ORDER BY position, id",
        (row["id"],),
    ).fetchall()

    data = {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "city": row["city"],
        "venue": row["venue"],
        "address": row["address"],
        "description": row["description"],
        "timezone": row["timezone"],
        "startsAt": row["starts_at"],
        "endsAt": row["ends_at"],
        "links": [{"id": link["id"], "title": link["title"], "url": link["url"]}
                  for link in link_rows],
        "halls": hall_items,
        "lineupVisible": lineup_visible,
        "lineupFilter": _lineup_filter(row),
    }
    if admin:
        data.update({
            "published": bool(row["published"]),
            "createdAt": row["created_at"],
            "startsAtLocal": timeutil.to_local_input(
                timeutil.parse_iso(row["starts_at"]), row["timezone"]),
            "endsAtLocal": timeutil.to_local_input(
                timeutil.parse_iso(row["ends_at"]), row["timezone"]),
            "lineupVisible": bool(row["lineup_visible"]),
            "lineupPublishAt": row["lineup_publish_at"],
            "lineupPublishAtLocal": (
                timeutil.to_local_input(
                    timeutil.parse_iso(row["lineup_publish_at"]), row["timezone"])
                if row["lineup_publish_at"] else None),
            "ownerId": row["owner_id"],
            "ownerUsername": _event_owner_name(conn, row),
            "publiclyVisible": _is_publicly_visible(row),
            "stats": {"halls": len(hall_items), "performances": total},
        })
    elif not lineup_visible and row["lineup_publish_at"]:
        # Публично не раскрываем залы и артистов, но можем показать, когда
        # расписание откроется автоматически.
        data["lineupPublishAt"] = row["lineup_publish_at"]
    return data


def _event_summary(conn, row, *, admin: bool = False) -> dict:
    lineup_visible = _lineup_is_visible(row)
    show_lineup = admin or lineup_visible
    if show_lineup:
        hall_count = conn.execute(
            "SELECT COUNT(*) FROM halls WHERE event_id = ?", (row["id"],)
        ).fetchone()[0]
        perf_count = conn.execute(
            "SELECT COUNT(*) FROM performances p JOIN halls h ON h.id = p.hall_id "
            "WHERE h.event_id = ?", (row["id"],)
        ).fetchone()[0]
    else:
        hall_count = 0
        perf_count = 0
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "city": row["city"],
        "venue": row["venue"],
        "timezone": row["timezone"],
        "startsAt": row["starts_at"],
        "endsAt": row["ends_at"],
        "hallCount": hall_count,
        "performanceCount": perf_count,
        "lineupVisible": lineup_visible,
        "lineupFilter": _lineup_filter(row),
    }


def _site_logo_row(conn):
    return conn.execute(
        "SELECT value, updated_at FROM site_settings WHERE key = ?",
        (SITE_LOGO_KEY,),
    ).fetchone()


def _site_logo_info(conn) -> dict:
    row = _site_logo_row(conn)
    if not row or not row["value"]:
        return {
            "logoUrl": None,
            "logoVersion": None,
            "logoUpdatedAt": None,
            "logoBytes": 0,
        }
    data = row["value"]
    if not isinstance(data, (bytes, bytearray)):
        data = str(data).encode("utf-8")
    data = bytes(data)
    version = hashlib.sha256(data).hexdigest()[:16]
    return {
        "logoUrl": f"/logo?v={version}",
        "logoVersion": version,
        "logoUpdatedAt": row["updated_at"] or None,
        "logoBytes": len(data),
    }


def _site_links(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, description, url FROM site_links "
        "ORDER BY position, id"
    ).fetchall()
    return [{
        "id": row["id"],
        "label": row["label"],
        "description": row["description"],
        "url": row["url"],
    } for row in rows]


def _normalize_site_links(data: dict) -> list[dict]:
    raw = data.get("links")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise APIError(400, "validation", "Кнопки должны быть списком.")
    if len(raw) > MAX_SITE_LINKS:
        raise APIError(400, "validation",
                       f"Можно добавить не больше {MAX_SITE_LINKS} кнопок.")
    links = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise APIError(400, "validation", f"Кнопка №{index} заполнена неверно.")
        label = item.get("label") or ""
        description = item.get("description") or ""
        url = item.get("url") or ""
        if not all(isinstance(value, str)
                   for value in (label, description, url)):
            raise APIError(400, "validation",
                           f"Кнопка №{index} заполнена неверно.")
        label = label.strip()
        description = description.strip()
        url = url.strip()
        if not label:
            raise APIError(400, "validation", f"Укажите название кнопки №{index}.")
        if len(label) > 60:
            raise APIError(400, "validation",
                           f"Название кнопки №{index} длиннее 60 символов.")
        if len(description) > 1200:
            raise APIError(400, "validation",
                           f"Описание кнопки №{index} длиннее 1200 символов.")
        if not description and not url:
            raise APIError(400, "validation",
                           f"Заполните описание или ссылку кнопки №{index}.")
        if len(url) > 500:
            raise APIError(400, "validation",
                           f"Ссылка кнопки №{index} длиннее 500 символов.")
        if url:
            lowered = url.lower()
            if not (lowered.startswith("http://")
                    or lowered.startswith("https://")):
                raise APIError(400, "validation",
                               f"Ссылка кнопки №{index} должна начинаться "
                               f"с http:// или https://.")
            if any(ch.isspace() or ord(ch) < 32 for ch in url):
                raise APIError(400, "validation",
                               f"Ссылка кнопки №{index} содержит "
                               f"недопустимые символы.")
        links.append({
            "label": label,
            "description": description,
            "url": url,
        })
    return links


def _replace_site_links(conn, links: list[dict]) -> None:
    conn.execute("DELETE FROM site_links")
    now = timeutil.iso_now()
    for position, link in enumerate(links, start=1):
        conn.execute(
            "INSERT INTO site_links (label, description, url, position, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (link["label"], link["description"], link["url"], position, now),
        )


def _decode_logo_payload(data: dict) -> bytes:
    raw = data.get("data")
    if not isinstance(raw, str) or not raw.strip():
        raise APIError(400, "validation", "Передайте PNG-файл.")
    value = raw.strip()
    if value.lower().startswith("data:"):
        header, separator, payload = value.partition(",")
        if not separator or not header.lower().startswith("data:image/png;base64"):
            raise APIError(400, "validation", "Ожидается PNG-файл.")
        value = payload
    value = "".join(value.split())
    try:
        blob = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise APIError(400, "validation", "Не удалось прочитать PNG-файл.") from None
    if len(blob) > MAX_LOGO_BYTES:
        raise APIError(
            413, "too_large",
            f"Логотип больше {MAX_LOGO_BYTES // 1024} КБ.",
        )
    if not blob.startswith(PNG_SIGNATURE):
        raise APIError(400, "validation", "Логотип должен быть PNG-файлом.")
    if len(blob) < 33 or blob[12:16] != b"IHDR":
        raise APIError(400, "validation", "PNG-файл повреждён.")
    width = int.from_bytes(blob[16:20], "big")
    height = int.from_bytes(blob[20:24], "big")
    if not width or not height:
        raise APIError(400, "validation", "PNG-файл повреждён.")
    if width > MAX_LOGO_SIDE or height > MAX_LOGO_SIDE:
        raise APIError(
            400, "validation",
            f"Размер логотипа не должен превышать {MAX_LOGO_SIDE}×{MAX_LOGO_SIDE} px.",
        )
    return blob


def _cookie_suffix(req: Request) -> str:
    secure = COOKIE_SECURE_SETTING in ("1", "true", "yes", "on") or (
        COOKIE_SECURE_SETTING == "auto" and req.scheme == "https"
    )
    return "; Secure" if secure else ""


def _session_of(req: Request):
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT s.*, u.username, u.is_super FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (security.token_hash(token),)
        ).fetchone()
        if not row:
            return None
        if int(row["expires_at"]) <= int(time.time()):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            conn.commit()
            return None
        return row
    finally:
        conn.close()


def _require_admin(req: Request, *, csrf: bool = True):
    session = _session_of(req)
    if not session:
        raise APIError(401, "unauthorized", "Требуется вход в админку.")
    if csrf:
        token = req.headers.get("x-csrf-token", "")
        if not token or not security.constant_time_equals(token, session["csrf"]):
            raise APIError(403, "csrf", "Недействительный CSRF-токен. Обновите страницу.")
        origin = req.headers.get("origin")
        if origin:
            try:
                host = origin.split("://", 1)[1].split("/", 1)[0]
            except IndexError:
                raise APIError(403, "origin", "Некорректный Origin.") from None
            if host.lower() != req.host.lower():
                raise APIError(403, "origin", "Запрос с чужого домена запрещён.")
    return session


def _require_super(req: Request, *, csrf: bool = True):
    session = _require_admin(req, csrf=csrf)
    if not session["is_super"]:
        raise APIError(
            403, "forbidden",
            "Действие доступно только главному администратору.",
        )
    return session


def _admin_event_row(conn: sqlite3.Connection, session, event_id: int):
    """Мероприятие, доступное текущему администратору.

    Обычный администратор управляет только своими мероприятиями. Для чужих
    отвечаем 404, чтобы не раскрывать само их существование.
    """
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row or (not session["is_super"] and row["owner_id"] != session["user_id"]):
        raise APIError(404, "not_found", "Мероприятие не найдено.")
    return row


def _audit(conn: sqlite3.Connection, session, action: str, *,
           entity: str = "", entity_id=None, event_id=None,
           detail: str = "") -> None:
    """Пишет действие администратора в журнал (внутри уже открытой транзакции)."""
    conn.execute(
        "INSERT INTO audit_log (actor_id, actor_name, action, entity, entity_id, "
        "event_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session["user_id"], session["username"], action, entity, entity_id,
         event_id, detail, timeutil.iso_now()),
    )


def _event_owner_name(conn: sqlite3.Connection, row) -> str:
    if not row["owner_id"]:
        return ""
    found = conn.execute(
        "SELECT username FROM users WHERE id = ?", (row["owner_id"],)
    ).fetchone()
    return found["username"] if found else ""


def _public_cutoff(now_iso: str | None = None) -> str:
    """Мероприятия, завершившиеся раньше этого момента, скрыты с сайта."""
    moment = timeutil.parse_iso(now_iso) if now_iso else timeutil.now_utc()
    return timeutil.to_iso(moment - timedelta(days=PUBLIC_RETENTION_DAYS))


def _is_publicly_visible(row, now_iso: str | None = None) -> bool:
    return bool(row["published"]) and row["ends_at"] >= _public_cutoff(now_iso)


def _lineup_is_visible(row, now_iso: str | None = None) -> bool:
    """Лайнап виден, если он не скрыт и время автопубликации уже наступило."""
    if not int(row["lineup_visible"] or 0):
        return False
    publish_at = row["lineup_publish_at"]
    if publish_at:
        return (now_iso or timeutil.iso_now()) >= publish_at
    return True


def _set_is_visible(row, now_iso: str, lineup_filter: str) -> bool:
    """Сет виден публично с учётом жёсткого фильтра расписания."""
    if lineup_filter == "live":
        return row["starts_at"] <= now_iso < row["ends_at"]
    if lineup_filter == "played":
        return row["starts_at"] <= now_iso
    return True


def _rate_limited(conn, ip: str, username: str) -> bool:
    since = int(time.time()) - LOGIN_WINDOW_SECONDS
    by_ip = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ts > ? AND ok = 0",
        (ip, since),
    ).fetchone()[0]
    if by_ip >= LOGIN_MAX_FAILURES:
        return True
    if username:
        by_user = conn.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE username = ? COLLATE NOCASE AND ts > ? AND ok = 0",
            (username, since),
        ).fetchone()[0]
        if by_user >= LOGIN_MAX_USER_FAILURES:
            return True
    return False


# --------------------------------------------------------------------------
# Статика и страницы
# --------------------------------------------------------------------------

def _serve_static(req: Request, rel_path: str, *, cache: str) -> Response:
    try:
        path = (PUBLIC_DIR / rel_path).resolve()
        path.relative_to(PUBLIC_DIR)
    except (ValueError, OSError):
        raise APIError(404, "not_found", "Файл не найден.") from None
    if not path.is_file():
        raise APIError(404, "not_found", "Файл не найден.")
    stat = path.stat()
    etag = f'W/"{stat.st_size:x}-{int(stat.st_mtime):x}"'
    if req.headers.get("if-none-match") == etag:
        return Response(b"", 304, headers={"ETag": etag, "Cache-Control": cache})
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
        ctype += "; charset=utf-8"
    return Response(
        path.read_bytes(), 200,
        content_type=ctype,
        headers={"ETag": etag, "Cache-Control": cache,
                 "X-Content-Type-Options": "nosniff"},
    )


@route(r"/")
def page_home(req):
    return _serve_static(req, "index.html", cache="no-cache")


@route(r"/e/([a-z0-9-]{1,80})")
def page_event(req, slug):
    return _serve_static(req, "event.html", cache="no-cache")


@route(r"/admin")
def page_admin(req):
    return _serve_static(req, "admin.html", cache="no-cache")


@route(r"/artists")
def page_artists(req):
    return _serve_static(req, "artists.html", cache="no-cache")


@route(r"/assets/([A-Za-z0-9_.-]{1,80})")
def asset(req, name):
    return _serve_static(req, f"assets/{name}", cache=ASSET_CACHE)


@route(r"/favicon\.svg")
def favicon(req):
    return _serve_static(req, "favicon.svg", cache="public, max-age=604800")


@route(r"/logo")
def site_logo(req):
    conn = db.connect()
    try:
        row = _site_logo_row(conn)
        if not row or not row["value"]:
            raise APIError(404, "not_found", "Логотип не загружен.")
        data = row["value"]
        if not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8")
        data = bytes(data)
        etag = f'W/"{hashlib.sha256(data).hexdigest()[:16]}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        }
        if req.headers.get("if-none-match") == etag:
            return Response(b"", 304, headers=headers)
        return Response(data, 200, content_type="image/png", headers=headers)
    finally:
        conn.close()


@route(r"/healthz")
def health(req):
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return json_response({"status": "ok"})


# --------------------------------------------------------------------------
# Публичный API
# --------------------------------------------------------------------------

def _published_events(conn, now_iso: str | None = None):
    cutoff = _public_cutoff(now_iso)
    return conn.execute(
        "SELECT * FROM events WHERE published = 1 AND ends_at >= ? "
        "ORDER BY starts_at ASC",
        (cutoff,),
    ).fetchall()


def _pick_featured(rows, now_iso: str):
    if not rows:
        return None
    live = [r for r in rows if r["starts_at"] <= now_iso <= r["ends_at"]]
    if live:
        return max(live, key=lambda r: r["starts_at"])
    upcoming = [r for r in rows if r["starts_at"] > now_iso]
    if upcoming:
        return min(upcoming, key=lambda r: r["starts_at"])
    return max(rows, key=lambda r: r["ends_at"])


@route(r"/api/public/featured")
def api_featured(req):
    conn = db.connect()
    try:
        now_iso = timeutil.iso_now()
        rows = _published_events(conn, now_iso)
        featured = _pick_featured(rows, now_iso)
        others = [r for r in rows if featured is None or r["id"] != featured["id"]]
        return json_response({
            "event": _event_json(conn, featured) if featured else None,
            "others": [_event_summary(conn, r) for r in others],
            "serverNow": now_iso,
        })
    finally:
        conn.close()


@route(r"/api/public/events")
def api_public_events(req):
    conn = db.connect()
    try:
        now_iso = timeutil.iso_now()
        rows = _published_events(conn, now_iso)
        return json_response({
            "events": [_event_summary(conn, r) for r in rows],
            "serverNow": now_iso,
        })
    finally:
        conn.close()


@route(r"/api/public/site")
def api_public_site(req):
    conn = db.connect()
    try:
        data = {"links": _site_links(conn), "serverNow": timeutil.iso_now()}
        data.update(_site_logo_info(conn))
        return json_response(data)
    finally:
        conn.close()


@route(r"/api/public/events/([a-z0-9-]{1,80})")
def api_public_event(req, slug):
    conn = db.connect()
    try:
        now_iso = timeutil.iso_now()
        row = conn.execute(
            "SELECT * FROM events WHERE slug = ? AND published = 1 AND ends_at >= ?",
            (slug, _public_cutoff(now_iso)),
        ).fetchone()
        if not row:
            raise APIError(404, "not_found", "Мероприятие не найдено.")
        return json_response({
            "event": _event_json(conn, row),
            "serverNow": now_iso,
        })
    finally:
        conn.close()


def _artist_key(name: str) -> str:
    return (name or "").strip().casefold()


def _public_artists(conn, now_iso: str, query: str = "") -> list[dict]:
    """Артисты с текущими публичными мероприятиями и поиск по всей базе."""
    cutoff = _public_cutoff(now_iso)
    artists: dict[str, dict] = {}

    def entry_for(name: str) -> dict:
        key = _artist_key(name)
        item = artists.get(key)
        if item is None:
            item = {
                "name": name.strip(),
                "events": {},
                "hasPublicEvent": False,
                "hasHiddenLineup": False,
            }
            artists[key] = item
        return item

    # Публичный список и поиск строятся только по базе исполнителей.
    # Если главный администратор удалил имя из неё, артист перестаёт
    # находиться через поиск, даже если его сет остался в мероприятии.
    for row in conn.execute("SELECT name FROM performers"):
        name = (row["name"] or "").strip()
        if name:
            entry_for(name)

    rows = conn.execute(
        "SELECT p.performer, p.starts_at, p.ends_at, h.name AS hall_name, "
        "e.id AS event_id, e.slug, e.title, e.city, e.venue, e.timezone, "
        "e.starts_at AS event_starts, e.ends_at AS event_ends, "
        "e.published, e.lineup_visible, e.lineup_publish_at, e.lineup_filter "
        "FROM performances p "
        "JOIN halls h ON h.id = p.hall_id "
        "JOIN events e ON e.id = h.event_id "
        "WHERE e.published = 1"
    ).fetchall()
    for row in rows:
        name = (row["performer"] or "").strip()
        if not name:
            continue
        item = artists.get(_artist_key(name))
        if item is None:
            # Имя удалено из базы автозаполнения — не показываем артиста.
            continue
        if row["event_ends"] < cutoff:
            # Прошедшее мероприятие уже не публично: имя остаётся в поиске,
            # но событие не показываем.
            continue
        if not _lineup_is_visible(row, now_iso):
            # Не раскрываем артистов из скрытого лайнапа.
            item["hasHiddenLineup"] = True
            continue

        # Мероприятие с опубликованным лайнапом показываем всегда, даже если
        # время конкретного сета скрыто жёстким фильтром расписания.
        item["hasPublicEvent"] = True
        event = item["events"].get(row["event_id"])
        if event is None:
            event = {
                "id": row["event_id"],
                "slug": row["slug"],
                "title": row["title"],
                "city": row["city"],
                "venue": row["venue"],
                "timezone": row["timezone"],
                "startsAt": row["event_starts"],
                "endsAt": row["event_ends"],
                "sets": [],
            }
            item["events"][row["event_id"]] = event

        lineup_filter = (
            row["lineup_filter"] if row["lineup_filter"] in LINEUP_FILTERS
            else "all"
        )
        if not _set_is_visible(row, now_iso, lineup_filter):
            # Время сета пока не публично: артист и мероприятие остаются
            # видимыми, но время выступления не отдаём.
            continue

        event["sets"].append({
            "hall": row["hall_name"],
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
        })

    needle = _artist_key(query)
    result = []
    for item in artists.values():
        if needle and needle not in _artist_key(item["name"]):
            continue
        if not needle and not item["hasPublicEvent"]:
            continue
        # Если артист есть только в скрытых лайнапах текущих мероприятий,
        # не показываем его даже в поиске.
        if not item["hasPublicEvent"] and item["hasHiddenLineup"]:
            continue
        events = sorted(item["events"].values(), key=lambda ev: ev["startsAt"])
        for event in events:
            event["sets"].sort(key=lambda perf: perf["startsAt"])
        result.append({
            "name": item["name"],
            "eventCount": len(events),
            "events": events,
        })

    result.sort(key=lambda item: _artist_key(item["name"]))
    return result[:200]


@route(r"/api/public/artists")
def api_public_artists(req):
    params = parse_qs(req.query)
    query = (params.get("q") or [""])[0].strip()[:80]
    conn = db.connect()
    try:
        now_iso = timeutil.iso_now()
        return json_response({
            "artists": _public_artists(conn, now_iso, query),
            "query": query,
            "serverNow": now_iso,
        })
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Авторизация
# --------------------------------------------------------------------------

@route(r"/api/admin/session")
def api_admin_session(req):
    session = _session_of(req)
    if not session:
        conn = db.connect()
        try:
            setup = db.user_count(conn) == 0
        finally:
            conn.close()
        return json_response(
            {"authenticated": False, "setupRequired": setup}, 401)
    return json_response({
        "authenticated": True,
        "user": {"username": session["username"],
                 "isSuper": bool(session["is_super"])},
        "csrfToken": session["csrf"],
        "expiresAt": int(session["expires_at"]),
    })


@route(r"/api/admin/login", methods=("POST",))
def api_admin_login(req):
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise APIError(400, "validation", "Введите логин и пароль.")
    if len(username) > 80 or len(password) > security.MAX_PASSWORD_LENGTH:
        raise APIError(400, "validation", "Слишком длинные логин или пароль.")

    conn = db.connect()
    try:
        if _rate_limited(conn, req.remote_addr, username):
            raise APIError(
                429, "too_many_attempts",
                "Слишком много неудачных попыток. Попробуйте позже.",
                {"Retry-After": str(LOGIN_WINDOW_SECONDS)},
            )

        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        stored = row["password_hash"] if row else security.DUMMY_HASH
        valid = security.verify_password(password, stored)
        if not valid or not row:
            # Небольшая задержка против перебора.
            time.sleep(0.15)
            conn.execute(
                "INSERT INTO login_attempts (ip, username, ts, ok) VALUES (?, ?, ?, 0)",
                (req.remote_addr, username[:80], int(time.time())),
            )
            conn.execute("DELETE FROM login_attempts WHERE ts < ?",
                         (int(time.time()) - 86400,))
            conn.commit()
            raise APIError(401, "invalid_credentials", "Неверный логин или пароль.")

        token = security.new_token()
        csrf = security.new_token()
        now = int(time.time())
        expires = now + SESSION_HOURS * 3600
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (security.token_hash(token), row["id"], csrf, now, expires),
        )
        conn.execute(
            "INSERT INTO login_attempts (ip, username, ts, ok) VALUES (?, ?, ?, 1)",
            (req.remote_addr, username[:80], now),
        )
        conn.execute("DELETE FROM login_attempts WHERE ts < ?", (now - 86400,))
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.commit()
        log.info("Вход в админку: %s (%s)", row["username"], req.remote_addr)
        resp = json_response({
            "authenticated": True,
            "user": {"username": row["username"],
                     "isSuper": bool(row["is_super"])},
            "csrfToken": csrf,
            "expiresAt": expires,
        })
        resp.headers["Set-Cookie"] = (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_HOURS * 3600}{_cookie_suffix(req)}"
        )
        return resp
    finally:
        conn.close()


@route(r"/api/admin/logout", methods=("POST",))
def api_admin_logout(req):
    session = _session_of(req)
    if session:
        _require_admin(req)
        conn = db.connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                         (session["token_hash"],))
            conn.commit()
        finally:
            conn.close()
        log.info("Выход из админки: %s", session["username"])
    resp = json_response({"ok": True})
    resp.headers["Set-Cookie"] = (
        f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        f"{_cookie_suffix(req)}"
    )
    return resp


@route(r"/api/admin/password", methods=("POST",))
def api_admin_password(req):
    session = _require_admin(req)
    data = req.json()
    current = str(data.get("currentPassword") or "")
    new = str(data.get("newPassword") or "")
    if not security.verify_password(current, _password_hash(session["user_id"])):
        raise APIError(403, "invalid_password", "Текущий пароль указан неверно.")
    problem = security.validate_password(new)
    if problem:
        raise APIError(400, "validation", problem)
    if new == current:
        raise APIError(400, "validation", "Новый пароль совпадает с текущим.")
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (security.hash_password(new), session["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                     (session["user_id"], session["token_hash"]))
        conn.commit()
    finally:
        conn.close()
    log.info("Пароль изменён: %s", session["username"])
    return json_response({"ok": True})


def _password_hash(user_id: int) -> str:
    conn = db.connect()
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        return row["password_hash"] if row else security.DUMMY_HASH
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Админка: администраторы (только главный)
# --------------------------------------------------------------------------

def _user_json(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "isSuper": bool(row["is_super"]),
        "createdAt": row["created_at"],
    }


def _user_row(conn, user_id: int):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise APIError(404, "not_found", "Администратор не найден.")
    return row


@route(r"/api/admin/users")
def api_admin_users(req):
    _require_super(req, csrf=False)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY id"
        ).fetchall()
        return json_response({"users": [_user_json(row) for row in rows]})
    finally:
        conn.close()


@route(r"/api/admin/users", methods=("POST",))
def api_admin_user_create(req):
    session = _require_super(req)
    data = req.json()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or len(username) > 80:
        raise APIError(400, "validation", "Укажите логин (до 80 символов).")
    if any(ch.isspace() for ch in username):
        raise APIError(400, "validation", "Логин не должен содержать пробелов.")
    problem = security.validate_password(password)
    if problem:
        raise APIError(400, "validation", problem)

    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if exists:
            raise APIError(409, "conflict",
                           f"Администратор «{username}» уже существует.")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_super, created_at) "
            "VALUES (?, ?, 0, ?)",
            (username, security.hash_password(password), timeutil.iso_now()),
        )
        _audit(conn, session, "user.create", entity="user",
               entity_id=int(cur.lastrowid), detail=username)
        conn.commit()
        row = _user_row(conn, int(cur.lastrowid))
    finally:
        conn.close()
    log.info("Добавлен администратор «%s»", username)
    return json_response({"user": _user_json(row)}, 201)


@route(r"/api/admin/users/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_user_update(req, user_id):
    session = _require_super(req)
    data = req.json()
    conn = db.connect()
    try:
        row = _user_row(conn, int(user_id))
        if row["is_super"]:
            raise APIError(403, "forbidden",
                           "Пароль главного администратора меняется в разделе «Пароль».")
        password = str(data.get("password") or "")
        problem = security.validate_password(password)
        if problem:
            raise APIError(400, "validation", problem)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (security.hash_password(password), row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        _audit(conn, session, "user.update", entity="user",
               entity_id=row["id"], detail=f"{row['username']}: пароль изменён")
        conn.commit()
    finally:
        conn.close()
    log.info("Пароль администратора «%s» изменён (кем: %s)",
             row["username"], session["username"])
    return json_response({"ok": True, "user": _user_json(row)})


@route(r"/api/admin/users/(\d{1,12})", methods=("DELETE",))
def api_admin_user_delete(req, user_id):
    session = _require_super(req)
    conn = db.connect()
    try:
        row = _user_row(conn, int(user_id))
        if row["is_super"]:
            raise APIError(403, "forbidden",
                           "Главного администратора удалить нельзя.")
        if row["id"] == session["user_id"]:
            raise APIError(403, "forbidden", "Нельзя удалить самого себя.")
        moved = conn.execute(
            "SELECT COUNT(*) FROM events WHERE owner_id = ?", (row["id"],)
        ).fetchone()[0]
        # Мероприятия удалённого админа переходят действующему главному.
        conn.execute(
            "UPDATE events SET owner_id = ? WHERE owner_id = ?",
            (session["user_id"], row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        _audit(conn, session, "user.delete", entity="user", entity_id=row["id"],
               detail=f"{row['username']} · мероприятий передано: {moved}")
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    log.info("Удалён администратор «%s»", row["username"])
    return json_response({"ok": True, "deletedId": row["id"]})


# --------------------------------------------------------------------------
# Админка: оформление сайта (только главный)
# --------------------------------------------------------------------------

@route(r"/api/admin/site")
def api_admin_site(req):
    _require_super(req, csrf=False)
    conn = db.connect()
    try:
        data = {"links": _site_links(conn)}
        data.update(_site_logo_info(conn))
        data.update({
            "maxLinks": MAX_SITE_LINKS,
            "maxLogoBytes": MAX_LOGO_BYTES,
            "maxLogoSide": MAX_LOGO_SIDE,
        })
        return json_response(data)
    finally:
        conn.close()


@route(r"/api/admin/site/links", methods=("PUT", "PATCH"))
def api_admin_site_links(req):
    session = _require_super(req)
    data = req.json()
    links = _normalize_site_links(data)
    conn = db.connect()
    try:
        _replace_site_links(conn, links)
        _audit(conn, session, "site.links", entity="site", detail=(
            f"кнопок: {len(links)}"
            + (f" · {', '.join(item['label'] for item in links)}" if links else "")
        ))
        conn.commit()
        return json_response({"links": _site_links(conn)})
    finally:
        conn.close()


@route(r"/api/admin/site/logo", methods=("PUT", "POST"))
def api_admin_site_logo_upload(req):
    session = _require_super(req)
    blob = _decode_logo_payload(req.json())
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO site_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (SITE_LOGO_KEY, sqlite3.Binary(blob), timeutil.iso_now()),
        )
        _audit(conn, session, "site.logo", entity="site",
               detail=f"PNG-логотип, {len(blob) // 1024} КБ")
        conn.commit()
        data = _site_logo_info(conn)
        return json_response({"ok": True, **data})
    finally:
        conn.close()


@route(r"/api/admin/site/logo", methods=("DELETE",))
def api_admin_site_logo_delete(req):
    session = _require_super(req)
    conn = db.connect()
    try:
        deleted = conn.execute(
            "DELETE FROM site_settings WHERE key = ?", (SITE_LOGO_KEY,)
        ).rowcount
        if deleted:
            _audit(conn, session, "site.logo", entity="site",
                   detail="логотип удалён")
        conn.commit()
        return json_response({"ok": True, "deleted": bool(deleted),
                              **_site_logo_info(conn)})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Админка: мероприятия
# --------------------------------------------------------------------------

def _normalize_links(data: dict) -> list[dict] | None:
    """Проверяет список ссылок. None — поле не передано, менять не нужно."""
    if "links" not in data:
        return None
    raw = data.get("links")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise APIError(400, "validation", "Ссылки должны быть списком.")
    if len(raw) > MAX_EVENT_LINKS:
        raise APIError(400, "validation",
                       f"Можно добавить не больше {MAX_EVENT_LINKS} ссылок.")
    links = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise APIError(400, "validation", f"Ссылка №{index} заполнена неверно.")
        title = item.get("title") or ""
        url = item.get("url") or ""
        if not isinstance(title, str) or not isinstance(url, str):
            raise APIError(400, "validation", f"Ссылка №{index} заполнена неверно.")
        title = title.strip()
        url = url.strip()
        if not url:
            raise APIError(400, "validation", f"Укажите адрес ссылки №{index}.")
        if len(title) > 80:
            raise APIError(400, "validation",
                           f"Название ссылки №{index} длиннее 80 символов.")
        if len(url) > 500:
            raise APIError(400, "validation", f"Ссылка №{index} длиннее 500 символов.")
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise APIError(400, "validation",
                           f"Ссылка №{index} должна начинаться с http:// или https://.")
        if any(ch.isspace() or ord(ch) < 32 for ch in url):
            raise APIError(400, "validation",
                           f"Ссылка №{index} содержит недопустимые символы.")
        links.append({"title": title, "url": url})
    return links


def _replace_links(conn, event_id: int, links: list[dict]) -> None:
    conn.execute("DELETE FROM event_links WHERE event_id = ?", (event_id,))
    for position, link in enumerate(links, start=1):
        conn.execute(
            "INSERT INTO event_links (event_id, title, url, position) "
            "VALUES (?, ?, ?, ?)",
            (event_id, link["title"], link["url"], position),
        )


def _normalize_event(data: dict, current=None) -> dict:
    current = dict(current) if current else {}
    title = _str_field(data, "title", "название", maxlen=120,
                       current=current.get("title", ""))
    city = _str_field(data, "city", "город", maxlen=80, required=False,
                      current=current.get("city", ""))
    venue = _str_field(data, "venue", "место проведения", maxlen=120,
                       current=current.get("venue", ""))
    address = _str_field(data, "address", "адрес", maxlen=200, required=False,
                         current=current.get("address", ""))
    description = _str_field(data, "description", "описание", maxlen=2000,
                             required=False, current=current.get("description", ""))

    tz_input = data.get("timezone", current.get("timezone", "Europe/Moscow"))
    try:
        tz = timeutil.validate_timezone(str(tz_input))
    except TimeError as exc:
        raise APIError(400, "validation", str(exc)) from None

    def resolve_moment(key: str, current_key: str):
        value = data.get(key)
        if value not in (None, ""):
            if not isinstance(value, str):
                raise APIError(400, "validation", "Некорректная дата и время.")
            try:
                return timeutil.parse_local(value, tz)
            except TimeError as exc:
                raise APIError(400, "validation", str(exc)) from None
        if current.get(current_key):
            return timeutil.parse_iso(current[current_key])
        raise APIError(400, "validation", "Укажите дату и время начала и конца.")

    starts = resolve_moment("startsAt", "starts_at")
    ends = resolve_moment("endsAt", "ends_at")
    if ends <= starts:
        raise APIError(400, "validation", "Конец мероприятия должен быть позже начала.")
    if ends - starts > timeutil.MAX_DURATION:
        raise APIError(400, "validation", "Мероприятие не может длиться дольше 14 дней.")
    if starts.year < 2000 or ends.year > 2100:
        raise APIError(400, "validation", "Проверьте год начала и конца.")

    published = _bool_field(data, "published", bool(current.get("published", 1)))
    lineup_visible = _bool_field(
        data, "lineupVisible", bool(current.get("lineup_visible", 1)))
    raw_filter = data.get("lineupFilter")
    if raw_filter in (None, ""):
        lineup_filter = current.get("lineup_filter", "all") or "all"
    elif not isinstance(raw_filter, str):
        raise APIError(400, "validation",
                       "Некорректный фильтр расписания.")
    else:
        lineup_filter = raw_filter.strip().lower()
    if lineup_filter not in LINEUP_FILTERS:
        raise APIError(400, "validation",
                       "Фильтр расписания: допустимо all, live или played.")
    if "lineupPublishAt" in data:
        raw_publish = data.get("lineupPublishAt")
        if raw_publish in (None, ""):
            lineup_publish_at = None
        else:
            if not isinstance(raw_publish, str):
                raise APIError(400, "validation",
                               "Некорректная дата открытия лайнапа.")
            try:
                lineup_publish_at = timeutil.to_iso(
                    timeutil.parse_local(raw_publish, tz))
            except TimeError as exc:
                raise APIError(400, "validation", str(exc)) from None
    else:
        lineup_publish_at = current.get("lineup_publish_at")
    return {
        "title": title, "city": city, "venue": venue, "address": address,
        "description": description, "timezone": tz,
        "starts_at": timeutil.to_iso(starts), "ends_at": timeutil.to_iso(ends),
        "published": 1 if published else 0,
        "lineup_visible": 1 if lineup_visible else 0,
        "lineup_publish_at": lineup_publish_at,
        "lineup_filter": lineup_filter,
    }


@route(r"/api/admin/events")
def api_admin_events(req):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        if session["is_super"]:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY starts_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE owner_id = ? ORDER BY starts_at DESC",
                (session["user_id"],),
            ).fetchall()
        events = []
        for row in rows:
            item = _event_summary(conn, row, admin=True)
            item.update({
                "published": bool(row["published"]),
                "startsAtLocal": timeutil.to_local_input(
                    timeutil.parse_iso(row["starts_at"]), row["timezone"]),
                "endsAtLocal": timeutil.to_local_input(
                    timeutil.parse_iso(row["ends_at"]), row["timezone"]),
                "lineupVisible": bool(row["lineup_visible"]),
                "lineupPublishAt": row["lineup_publish_at"],
                "lineupPublishAtLocal": (
                    timeutil.to_local_input(
                        timeutil.parse_iso(row["lineup_publish_at"]), row["timezone"])
                    if row["lineup_publish_at"] else None),
                "ownerId": row["owner_id"],
                "ownerUsername": _event_owner_name(conn, row),
                "publiclyVisible": _is_publicly_visible(row),
            })
            events.append(item)
        return json_response({"events": events, "timezones": TIMEZONES,
                              "serverNow": timeutil.iso_now()})
    finally:
        conn.close()


@route(r"/api/admin/events", methods=("POST",))
def api_admin_event_create(req):
    session = _require_admin(req)
    data = req.json()
    fields = _normalize_event(data)
    conn = db.connect()
    try:
        slug = unique_slug(conn, fields["title"])
        cur = conn.execute(
            "INSERT INTO events (slug, title, city, venue, address, description, "
            "timezone, starts_at, ends_at, published, owner_id, lineup_visible, "
            "lineup_publish_at, lineup_filter, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, fields["title"], fields["city"], fields["venue"],
             fields["address"], fields["description"], fields["timezone"],
             fields["starts_at"], fields["ends_at"], fields["published"],
             session["user_id"], fields["lineup_visible"],
             fields["lineup_publish_at"], fields["lineup_filter"],
             timeutil.iso_now()),
        )
        event_id = int(cur.lastrowid)
        links = _normalize_links(data)
        if links:
            _replace_links(conn, event_id, links)
        _audit(conn, session, "event.create", entity="event",
               entity_id=event_id, event_id=event_id, detail=fields["title"])
        conn.commit()
        row = _event_row(conn, event_id)
        log.info("Создано мероприятие #%s «%s» (владелец: %s)",
                 row["id"], row["title"], session["username"])
        return json_response({"event": _event_json(conn, row, admin=True)}, 201)
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})")
def api_admin_event_get(req, event_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        row = _admin_event_row(conn, session, int(event_id))
        return json_response({"event": _event_json(conn, row, admin=True),
                              "timezones": TIMEZONES})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_event_update(req, event_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        row = _admin_event_row(conn, session, int(event_id))
        fields = _normalize_event(data, current=row)
        conn.execute(
            "UPDATE events SET title=?, city=?, venue=?, address=?, description=?, "
            "timezone=?, starts_at=?, ends_at=?, published=?, lineup_visible=?, "
            "lineup_publish_at=?, lineup_filter=? WHERE id=?",
            (fields["title"], fields["city"], fields["venue"], fields["address"],
             fields["description"], fields["timezone"], fields["starts_at"],
             fields["ends_at"], fields["published"], fields["lineup_visible"],
             fields["lineup_publish_at"], fields["lineup_filter"], row["id"]),
        )
        links = _normalize_links(data)
        if links is not None:
            _replace_links(conn, row["id"], links)
        _audit(conn, session, "event.update", entity="event",
               entity_id=row["id"], event_id=row["id"], detail=fields["title"])
        conn.commit()
        row = _event_row(conn, row["id"])
        log.info("Изменено мероприятие #%s «%s»", row["id"], row["title"])
        return json_response({"event": _event_json(conn, row, admin=True)})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})", methods=("DELETE",))
def api_admin_event_delete(req, event_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        row = _admin_event_row(conn, session, int(event_id))
        hall_rows = conn.execute(
            "SELECT id, name FROM halls WHERE event_id = ? ORDER BY position, id",
            (row["id"],),
        ).fetchall()
        parts = []
        for hall in hall_rows:
            perfs = conn.execute(
                "SELECT performer FROM performances WHERE hall_id = ? "
                "ORDER BY starts_at, id", (hall["id"],),
            ).fetchall()
            names = ", ".join(p["performer"] for p in perfs)
            parts.append(f"{hall['name']}: {names}" if names else hall["name"])
        detail = f"{row['title']} ({row['slug']})"
        if parts:
            detail += " · " + "; ".join(parts)
        _audit(conn, session, "event.delete", entity="event",
               entity_id=row["id"], event_id=row["id"], detail=detail)
        conn.execute("DELETE FROM event_links WHERE event_id = ?", (row["id"],))
        conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
        conn.commit()
        log.info("Удалено мероприятие #%s «%s»", row["id"], row["title"])
        return json_response({"ok": True, "deletedId": row["id"]})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Админка: залы
# --------------------------------------------------------------------------

@route(r"/api/admin/events/(\d{1,12})/halls", methods=("POST",))
def api_admin_hall_create(req, event_id):
    session = _require_admin(req)
    data = req.json()
    name = _str_field(data, "name", "название зала", maxlen=60)
    conn = db.connect()
    try:
        event = _admin_event_row(conn, session, int(event_id))
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM halls WHERE event_id = ?",
            (event["id"],),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO halls (event_id, name, position) VALUES (?, ?, ?)",
            (event["id"], name, position),
        )
        hall_id = int(cur.lastrowid)
        _audit(conn, session, "hall.create", entity="hall", entity_id=hall_id,
               event_id=event["id"], detail=f"{event['title']} · {name}")
        conn.commit()
        log.info("Создан зал #%s «%s» (мероприятие #%s)",
                 hall_id, name, event["id"])
        return json_response({
            "hall": {"id": hall_id, "name": name,
                     "position": position, "performances": []}
        }, 201)
    finally:
        conn.close()


@route(r"/api/admin/halls/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_hall_update(req, hall_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        hall = _hall_row(conn, int(hall_id))
        event = _admin_event_row(conn, session, hall["event_id"])
        old_name = hall["name"]
        name = _str_field(data, "name", "название зала", maxlen=60,
                          current=old_name)
        conn.execute("UPDATE halls SET name = ? WHERE id = ?", (name, hall["id"]))
        _audit(conn, session, "hall.update", entity="hall", entity_id=hall["id"],
               event_id=event["id"], detail=f"{event['title']} · {old_name} → {name}")
        conn.commit()
        return json_response({"hall": {"id": hall["id"], "name": name}})
    finally:
        conn.close()


@route(r"/api/admin/halls/(\d{1,12})", methods=("DELETE",))
def api_admin_hall_delete(req, hall_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        hall = _hall_row(conn, int(hall_id))
        event = _admin_event_row(conn, session, hall["event_id"])
        count = conn.execute(
            "SELECT COUNT(*) FROM performances WHERE hall_id = ?", (hall["id"],)
        ).fetchone()[0]
        _audit(conn, session, "hall.delete", entity="hall", entity_id=hall["id"],
               event_id=event["id"],
               detail=f"{event['title']} · {hall['name']} · сетов: {count}")
        conn.execute("DELETE FROM halls WHERE id = ?", (hall["id"],))
        conn.commit()
        log.info("Удалён зал #%s «%s»", hall["id"], hall["name"])
        return json_response({"ok": True, "deletedId": hall["id"]})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Админка: выступления
# --------------------------------------------------------------------------

def _remember_performer(conn: sqlite3.Connection, owner_id, name: str) -> None:
    """Пополняет базу исполнителей для автозаполнения."""
    name = (name or "").strip()
    if not owner_id or not name:
        return
    conn.execute(
        "INSERT OR IGNORE INTO performers (owner_id, name, created_at) "
        "VALUES (?, ?, ?)",
        (owner_id, name, timeutil.iso_now()),
    )


def _performance_times(data: dict, tz: str, event_start: str, event_end: str,
                       *, current=None):
    def moment(key, column, label):
        value = data.get(key)
        if value in (None, ""):
            if current:
                return timeutil.parse_iso(current[column])
            raise APIError(400, "validation", f"Укажите {label} выступления.")
        if not isinstance(value, str):
            raise APIError(400, "validation", f"Некорректное {label}.")
        try:
            return timeutil.parse_local(value, tz)
        except TimeError as exc:
            raise APIError(400, "validation", str(exc)) from None

    starts = moment("startsAt", "starts_at", "время начала")
    ends = moment("endsAt", "ends_at", "время окончания")
    ev_start = timeutil.parse_iso(event_start)
    ev_end = timeutil.parse_iso(event_end)
    problem = timeutil.overlap_warning(ev_start, ev_end, starts, ends)
    if problem:
        raise APIError(400, "validation", problem)
    return starts, ends


def _check_hall_overlap(conn, hall_id: int, starts, ends, tz: str,
                        exclude_id: int | None = None) -> None:
    query = (
        "SELECT * FROM performances WHERE hall_id = ? AND starts_at < ? AND ends_at > ?"
    )
    params = [hall_id, timeutil.to_iso(ends), timeutil.to_iso(starts)]
    if exclude_id is not None:
        query += " AND id <> ?"
        params.append(exclude_id)
    conflict = conn.execute(query + " ORDER BY starts_at LIMIT 1", params).fetchone()
    if conflict:
        label = timeutil.human_range(
            conflict["starts_at"], conflict["ends_at"], tz)
        raise APIError(
            409, "overlap",
            f"Время пересекается с «{conflict['performer']}» ({label}).",
        )


@route(r"/api/admin/halls/(\d{1,12})/performances", methods=("POST",))
def api_admin_perf_create(req, hall_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        hall = _hall_row(conn, int(hall_id))
        event = _admin_event_row(conn, session, hall["event_id"])
        performer = _str_field(data, "performer", "имя исполнителя", maxlen=120)
        tz = hall["event_timezone"]
        starts, ends = _performance_times(
            data, tz, hall["event_starts"], hall["event_ends"])
        _check_hall_overlap(conn, hall["id"], starts, ends, tz)
        cur = conn.execute(
            "INSERT INTO performances (hall_id, performer, starts_at, ends_at) "
            "VALUES (?, ?, ?, ?)",
            (hall["id"], performer, timeutil.to_iso(starts), timeutil.to_iso(ends)),
        )
        perf_id = int(cur.lastrowid)
        _remember_performer(conn, event["owner_id"], performer)
        _audit(conn, session, "performance.create", entity="performance",
               entity_id=perf_id, event_id=event["id"],
               detail=f"{event['title']} · {hall['name']} · {performer}")
        conn.commit()
        row = conn.execute("SELECT * FROM performances WHERE id = ?",
                           (perf_id,)).fetchone()
        log.info("Добавлено выступление #%s «%s» (зал #%s)",
                 row["id"], performer, hall["id"])
        return json_response({"performance": _perf_admin_json(row, tz)}, 201)
    finally:
        conn.close()


@route(r"/api/admin/performances/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_perf_update(req, perf_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        perf = _perf_row(conn, int(perf_id))
        event = _admin_event_row(conn, session, perf["event_id"])
        hall = _hall_row(conn, perf["hall_id"])
        tz = hall["event_timezone"]
        performer = _str_field(data, "performer", "имя исполнителя", maxlen=120,
                               current=perf["performer"])
        starts, ends = _performance_times(
            data, tz, hall["event_starts"], hall["event_ends"], current=perf)
        _check_hall_overlap(conn, hall["id"], starts, ends, tz,
                            exclude_id=perf["id"])
        conn.execute(
            "UPDATE performances SET performer = ?, starts_at = ?, ends_at = ? "
            "WHERE id = ?",
            (performer, timeutil.to_iso(starts), timeutil.to_iso(ends), perf["id"]),
        )
        _remember_performer(conn, event["owner_id"], performer)
        _audit(conn, session, "performance.update", entity="performance",
               entity_id=perf["id"], event_id=event["id"],
               detail=(f"{event['title']} · {hall['name']} · "
                       + (performer if perf["performer"] == performer
                          else f"{perf['performer']} → {performer}")))
        conn.commit()
        row = conn.execute("SELECT * FROM performances WHERE id = ?",
                           (perf["id"],)).fetchone()
        return json_response({"performance": _perf_admin_json(row, tz)})
    finally:
        conn.close()


@route(r"/api/admin/performances/(\d{1,12})", methods=("DELETE",))
def api_admin_perf_delete(req, perf_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        perf = _perf_row(conn, int(perf_id))
        event = _admin_event_row(conn, session, perf["event_id"])
        hall = _hall_row(conn, perf["hall_id"])
        _audit(conn, session, "performance.delete", entity="performance",
               entity_id=perf["id"], event_id=event["id"],
               detail=f"{event['title']} · {hall['name']} · {perf['performer']}")
        conn.execute("DELETE FROM performances WHERE id = ?", (perf["id"],))
        conn.commit()
        log.info("Удалено выступление #%s «%s»", perf["id"], perf["performer"])
        return json_response({"ok": True, "deletedId": perf["id"]})
    finally:
        conn.close()


@route(r"/api/admin/performers")
def api_admin_performers(req):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        if session["is_super"]:
            rows = conn.execute(
                "SELECT name FROM performers ORDER BY name COLLATE NOCASE"
            ).fetchall()
        else:
            # Подсказки только по своим артистам, чтобы не раскрывать базу
            # исполнителей других промо-групп.
            rows = conn.execute(
                "SELECT name FROM performers WHERE owner_id = ? "
                "ORDER BY name COLLATE NOCASE",
                (session["user_id"],),
            ).fetchall()
        seen = set()
        names = []
        for row in rows:
            key = row["name"].casefold()
            if key not in seen:
                seen.add(key)
                names.append(row["name"])
        return json_response({"performers": names})
    finally:
        conn.close()


@route(r"/api/admin/performers", methods=("DELETE",))
def api_admin_performer_delete(req):
    """Убирает имя из базы автозаполнения (только главный администратор)."""
    session = _require_super(req)
    name = (parse_qs(req.query).get("name") or [""])[0].strip()
    if not name:
        raise APIError(400, "validation", "Укажите имя артиста.")
    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM performers WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        if not exists:
            raise APIError(404, "not_found", "Артист не найден в списке.")
        # Имя может повторяться у разных владельцев — убираем все такие записи.
        deleted = conn.execute(
            "DELETE FROM performers WHERE name = ?", (name,)
        ).rowcount
        _audit(conn, session, "performer.delete", entity="performer",
               detail=name)
        conn.commit()
        log.info("Артист «%s» удалён из подсказок: %s", name, deleted)
        return json_response({"ok": True, "name": name, "deleted": deleted})
    finally:
        conn.close()


@route(r"/api/admin/audit")
def api_admin_audit(req):
    _require_super(req, csrf=False)
    params = parse_qs(req.query)
    try:
        limit = int((params.get("limit") or ["200"])[0])
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 500))
    before = (params.get("before") or [""])[0]
    conn = db.connect()
    try:
        query = "SELECT * FROM audit_log"
        args: list = []
        if before.isdigit():
            query += " WHERE id < ?"
            args.append(int(before))
        query += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(query, args).fetchall()
        entries = [{
            "id": row["id"],
            "actor": row["actor_name"],
            "action": row["action"],
            "entity": row["entity"],
            "entityId": row["entity_id"],
            "eventId": row["event_id"],
            "detail": row["detail"],
            "createdAt": row["created_at"],
        } for row in rows]
        return json_response({
            "entries": entries,
            "serverNow": timeutil.iso_now(),
        })
    finally:
        conn.close()


@route(r"/api/admin/audit", methods=("DELETE",))
def api_admin_audit_clear(req):
    _require_super(req)
    conn = db.connect()
    try:
        deleted = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        log.info("Журнал действий очищен: %s записей", deleted)
        return json_response({"ok": True, "deleted": deleted})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Точка входа WSGI
# --------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def app(environ, start_response):
    started = time.time()
    req = Request(environ)
    try:
        resp = dispatch(req)
    except APIError as exc:
        resp = error_response(exc)
    except sqlite3.Error:
        log.exception("Ошибка базы данных: %s %s", req.method, req.path)
        resp = error_response(APIError(500, "server_error",
                                       "Внутренняя ошибка сервера."))
    except Exception:
        log.exception("Необработанная ошибка: %s %s", req.method, req.path)
        resp = error_response(APIError(500, "server_error",
                                       "Внутренняя ошибка сервера."))

    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    for key, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    if req.scheme == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if req.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")

    logger = logging.getLogger("lineup.access")
    if logger.isEnabledFor(logging.INFO):
        logger.info("%s %s %s %d %.0fms", req.remote_addr, req.method,
                    req.path, resp.status, (time.time() - started) * 1000)
    return resp.emit(environ, start_response)
