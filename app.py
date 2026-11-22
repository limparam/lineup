"""LineUP — WSGI-приложение: публичный сайт, API и защищённая админка.

Запуск в разработке:  python manage.py serve
В продакшене:         gunicorn -w 3 -b 127.0.0.1:8000 app:app  (за nginx)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote

import segno

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
    410: "Gone",
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


def _replacement_name(row) -> str:
    if "replacement_performer" not in row.keys():
        return ""
    return (row["replacement_performer"] or "").strip()


def _perf_display_name(row) -> str:
    """Имя для интерфейса: у замены — диджей, который её отыграет."""
    replacement = bool(row["is_replacement"]) if "is_replacement" in row.keys() else False
    if replacement:
        return _display_artist_name(_replacement_name(row)) or "Замена"
    return _display_artist_name(row["performer"])


def _perf_json(row) -> dict:
    replacement = bool(row["is_replacement"]) if "is_replacement" in row.keys() else False
    return {
        "id": row["id"],
        "performer": row["performer"],
        "replacementPerformer": _replacement_name(row),
        "displayPerformer": _perf_display_name(row),
        "startsAt": row["starts_at"],
        "endsAt": row["ends_at"],
        "isReplacement": replacement,
    }


def _perf_admin_json(row, tz: str) -> dict:
    data = _perf_json(row)
    data["startsAtLocal"] = timeutil.to_local_input(timeutil.parse_iso(row["starts_at"]), tz)
    data["endsAtLocal"] = timeutil.to_local_input(timeutil.parse_iso(row["ends_at"]), tz)
    return data


MAX_EVENT_LINKS = 12

MAX_SITE_LINKS = 8
SITE_LOGO_KEY = "logo_png"
SITE_COPYRIGHT_KEY = "copyright_text"
SITE_THEME_KEY = "theme"
MAX_GUEST_LISTS = 20
MAX_GUESTS_PER_LIST = 5000
GUEST_STATUSES = ("invited", "checked_in")
# 180 КБ в base64 занимают меньше 256 КБ, лимита тела запроса.
MAX_LOGO_BYTES = 180 * 1024
MAX_LOGO_SIDE = 5000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CUSTOM_THEME_KEYS = ("background", "panel", "text", "muted", "accent", "timing")


def _site_theme(theme_id: str, name: str, description: str, *,
                background: str, panel: str, text: str, muted: str,
                accent: str, secondary: str, timing: str,
                timing_deep: str) -> dict:
    return {
        "id": theme_id,
        "name": name,
        "description": description,
        "colors": {
            "background": background,
            "panel": panel,
            "text": text,
            "muted": muted,
            "accent": accent,
            "secondary": secondary,
            "timing": timing,
            "timingDeep": timing_deep,
        },
    }


SITE_THEMES = [
    _site_theme("default", "Изумруд", "Текущий стиль LineUP",
                background="#06070a", panel="#0e1117", text="#f2f5f7",
                muted="#98a2b3", accent="#2bff88", secondary="#606cff",
                timing="#2bff88", timing_deep="#17d96f"),
    _site_theme("neon", "Неон", "Фиолетовый неон и розовые акценты",
                background="#08060f", panel="#12101d", text="#f4f0ff",
                muted="#a79ac9", accent="#b66cff", secondary="#ff5ed2",
                timing="#d08cff", timing_deep="#9b4dff"),
    _site_theme("cyber", "Кибер", "Холодный синий и бирюза",
                background="#050a12", panel="#0c1522", text="#eaf6ff",
                muted="#86a3bf", accent="#39c6ff", secondary="#7c5cff",
                timing="#6fe0ff", timing_deep="#1aa3e8"),
    _site_theme("sunset", "Закат", "Тёплый оранжевый и красный",
                background="#0f0806", panel="#1c110c", text="#fff1e8",
                muted="#c49a86", accent="#ff8a3d", secondary="#ff4d6d",
                timing="#ffb15c", timing_deep="#ff6a00"),
    _site_theme("crimson", "Красный бархат", "Бордовый клубный стиль",
                background="#100608", panel="#1d0c11", text="#ffeef1",
                muted="#c18d99", accent="#ff4d6d", secondary="#ff9f43",
                timing="#ff7a90", timing_deep="#d61f45"),
    _site_theme("ice", "Лёд", "Ледяной голубой свет",
                background="#050b10", panel="#0b1620", text="#eafcff",
                muted="#8fb3c2", accent="#7fe9ff", secondary="#a7c7ff",
                timing="#b4f4ff", timing_deep="#38c8e6"),
    _site_theme("gold", "Золото", "Лаунж с золотыми акцентами",
                background="#0d0a05", panel="#1a150a", text="#fff6df",
                muted="#c9b184", accent="#f5c15c", secondary="#ff8a3d",
                timing="#ffd98a", timing_deep="#e0a72e"),
    _site_theme("magenta", "Магента", "Яркий розовый неон",
                background="#100611", panel="#1c0b1d", text="#ffeaf9",
                muted="#c48bb2", accent="#ff5ed2", secondary="#b66cff",
                timing="#ff8ee0", timing_deep="#e026b4"),
    _site_theme("acid", "Кислота", "Кислотный лайм и зелёный",
                background="#080b05", panel="#131a0c", text="#f4ffe3",
                muted="#a7ba86", accent="#c6ff3d", secondary="#39ff88",
                timing="#dbff77", timing_deep="#a8e600"),
    _site_theme("mono", "Монохром", "Строгий чёрно-белый стиль",
                background="#08090b", panel="#13151a", text="#f5f7fa",
                muted="#9aa3b2", accent="#d7dde7", secondary="#8b93a1",
                timing="#ffffff", timing_deep="#b7c0cf"),
    _site_theme("purple", "Глубокий фиолет", "Тёмный фиолетовый и лаванда",
                background="#080713", panel="#121128", text="#efedff",
                muted="#9c96c9", accent="#8a7dff", secondary="#ff5ed2",
                timing="#a99fff", timing_deep="#6b5cff"),
]

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


def _site_setting_row(conn, key: str):
    return conn.execute(
        "SELECT value, updated_at FROM site_settings WHERE key = ?", (key,)
    ).fetchone()


def _default_copyright() -> str:
    return f"© {timeutil.now_utc().year} LineUP — расписание мероприятий"


def _site_copyright(conn) -> str:
    row = _site_setting_row(conn, SITE_COPYRIGHT_KEY)
    if row and isinstance(row["value"], str) and row["value"].strip():
        return row["value"].strip()
    return _default_copyright()


def _theme_by_id(theme_id: str) -> dict | None:
    return next((item for item in SITE_THEMES if item["id"] == theme_id), None)


def _custom_theme_colors(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    colors = {}
    for key in CUSTOM_THEME_KEYS:
        item = value.get(key)
        if not isinstance(item, str) or not HEX_COLOR_RE.fullmatch(item):
            return None
        colors[key] = item.lower()
    return colors


def _site_theme_info(conn) -> dict:
    row = _site_setting_row(conn, SITE_THEME_KEY)
    if row and isinstance(row["value"], str) and row["value"].strip():
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            value = {}
        if isinstance(value, dict):
            if value.get("mode") == "preset":
                theme = _theme_by_id(str(value.get("id") or ""))
                if theme:
                    return {**theme, "mode": "preset"}
            if value.get("mode") == "custom":
                colors = _custom_theme_colors(value.get("colors"))
                if colors:
                    return {
                        "mode": "custom",
                        "id": "custom",
                        "name": "Своя тема",
                        "description": "Собственные цвета",
                        "colors": colors,
                    }
    return {**SITE_THEMES[0], "mode": "preset"}


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
            "SELECT s.*, u.username, u.is_super, u.is_promoter, "
            "u.expires_at AS account_expires_at "
            "FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (security.token_hash(token),)
        ).fetchone()
        if not row:
            return None
        if int(row["expires_at"]) <= int(time.time()):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
            conn.commit()
            return None
        if row["account_expires_at"] and row["account_expires_at"] < timeutil.iso_now()[:10]:
            conn.execute("DELETE FROM users WHERE id = ?", (row["user_id"],))
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
        links = _site_links(conn)
        copyright = _site_copyright(conn)
        theme = _site_theme_info(conn)
        logo = _site_logo_info(conn)
        fingerprint = json.dumps({
            "links": links,
            "copyright": copyright,
            "theme": theme,
            "logoVersion": logo.get("logoVersion"),
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data = {
            "links": links,
            "copyright": copyright,
            "theme": theme,
            "siteVersion": hashlib.sha256(fingerprint).hexdigest()[:16],
            "serverNow": timeutil.iso_now(),
        }
        data.update(logo)
        return json_response(data)
    finally:
        conn.close()


@route(r"/r/([A-Za-z0-9_-]{16,80})")
def page_registration(req, token):
    return _serve_static(req, "registration.html", cache="no-cache")


@route(r"/api/public/registration/([A-Za-z0-9_-]{16,80})")
def api_public_registration_info(req, token):
    conn = db.connect()
    try:
        row = _registration_link_by_token(conn, token)
        state = _registration_link_state(conn, row)
        return json_response({
            "event": {
                "title": row["event_title"],
                "startsAt": row["event_starts"],
                "endsAt": row["event_ends"],
                "timezone": row["event_timezone"],
            },
            "canRegister": state["canRegister"],
            "reason": state["reason"],
            "message": (_registration_closed_message(state["reason"])
                        if state["reason"] else ""),
            "registrationCount": state["registrationCount"],
            "maxRegistrations": state["maxRegistrations"],
            "remaining": state["remaining"],
            "expiresAt": row["expires_at"],
            "serverNow": timeutil.iso_now(),
        })
    finally:
        conn.close()


@route(r"/api/public/registration/([A-Za-z0-9_-]{16,80})",
       methods=("POST",))
def api_public_registration(req, token):
    data = req.json()
    raw_name = data.get("name")
    raw_phone = data.get("phone")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise APIError(400, "validation", "Укажите имя.")
    name = raw_name.strip()
    if len(name) > 120:
        raise APIError(400, "validation", "Слишком длинное имя.")
    if raw_phone is None:
        raw_phone = ""
    if not isinstance(raw_phone, str):
        raise APIError(400, "validation", "Телефон должен быть строкой.")
    phone = raw_phone.strip()
    if len(phone) > 40:
        raise APIError(400, "validation", "Слишком длинный телефон.")

    conn = db.connect()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _registration_link_by_token(conn, token)
            state = _registration_link_state(conn, row)
            if not state["canRegister"]:
                raise APIError(
                    410, "registration_closed",
                    _registration_closed_message(state["reason"]),
                )
            guest_token = _new_guest_token(conn)
            now = timeutil.iso_now()
            cur = conn.execute(
                "INSERT INTO guests (list_id, event_id, name, phone, table_name, "
                "notes, status, share_token, created_by, registration_link_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '', 'Саморегистрация', 'invited', ?, NULL, ?, ?, ?)",
                (row["list_id"], row["event_id"], name, phone, guest_token,
                 row["id"], now, now),
            )
            limit = state["maxRegistrations"]
            if limit is not None and state["registrationCount"] + 1 >= limit:
                conn.execute(
                    "UPDATE guest_registration_links SET is_active = 0 WHERE id = ?",
                    (row["id"],),
                )
            _audit(conn, {"user_id": None, "username": "registration"},
                   "guest.registration", entity="guest",
                   entity_id=int(cur.lastrowid), event_id=row["event_id"],
                   detail=f"{row['event_title']} · {row['list_name']} · {name}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return json_response({
            "guest": {"name": name, "shareToken": guest_token},
            "event": {"title": row["event_title"]},
        }, 201)
    finally:
        conn.close()


@route(r"/g/([A-Za-z0-9_-]{16,80})")
def page_guest(req, token):
    return _serve_static(req, "guest.html", cache="no-cache")


@route(r"/api/public/guests/([A-Za-z0-9_-]{16,80})")
def api_public_guest(req, token):
    conn = db.connect()
    try:
        row = _public_guest_row(conn, token)
        return json_response(_guest_public_json(row))
    finally:
        conn.close()


@route(r"/g/([A-Za-z0-9_-]{16,80})/qr\.png")
def guest_qr(req, token):
    conn = db.connect()
    try:
        _public_guest_row(conn, token)
        url = f"{req.scheme}://{req.host}/g/{token}"
        qr = segno.make(url, error="m")
        buffer = io.BytesIO()
        qr.save(buffer, kind="png", scale=24, border=4)
        return Response(
            buffer.getvalue(), 200, content_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    finally:
        conn.close()


@route(r"/door/([A-Za-z0-9_-]{16,80})")
def page_door(req, token):
    resp = _serve_static(req, "door.html", cache="no-cache")
    resp.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(self), payment=()")
    return resp


@route(r"/api/door/([A-Za-z0-9_-]{16,80})")
def api_door_event(req, token):
    conn = db.connect()
    try:
        row = _door_link_row(conn, token)
        return json_response(_door_event_json(conn, row))
    finally:
        conn.close()


@route(r"/api/door/([A-Za-z0-9_-]{16,80})/search")
def api_door_search(req, token):
    conn = db.connect()
    try:
        row = _door_link_row(conn, token)
        query = (parse_qs(req.query).get("q") or [""])[0].strip()[:80]
        if not query:
            return json_response({"guests": [], "stats": _door_event_stats(
                conn, row["link_event_id"])})
        clause, args = _guest_search_clause(query, "g.")
        guests = conn.execute(
            "SELECT g.*, gl.name AS list_name, gl.is_blacklist FROM guests g "
            "JOIN guest_lists gl ON gl.id = g.list_id "
            "WHERE g.event_id = ?" + clause +
            " ORDER BY CASE g.status WHEN 'checked_in' THEN 1 ELSE 0 END, "
            "g.name COLLATE NOCASE LIMIT 30",
            [row["link_event_id"], *args],
        ).fetchall()
        payload = []
        for guest in guests:
            item = _door_guest_json(guest)
            if not item["blacklisted"]:
                match = _blacklist_match(conn, row["link_event_id"], guest)
                if match:
                    item["blacklistMatch"] = _door_guest_json(match)
            payload.append(item)
        return json_response({
            "guests": payload,
            "stats": _door_event_stats(conn, row["link_event_id"]),
        })
    finally:
        conn.close()


@route(r"/api/door/([A-Za-z0-9_-]{16,80})/scan", methods=("POST",))
def api_door_scan(req, token):
    data = req.json()
    raw = data.get("code")
    if not isinstance(raw, str) or not raw.strip():
        raise APIError(400, "validation", "Передайте QR-код гостя.")
    raw = raw.strip()
    if len(raw) > 500:
        raise APIError(400, "validation", "Слишком длинный QR-код.")
    match = re.search(r"/g/([A-Za-z0-9_-]{16,80})", raw)
    guest_token = match.group(1) if match else raw
    conn = db.connect()
    try:
        door = _door_link_row(conn, token)
        guest = conn.execute(
            "SELECT g.*, gl.name AS list_name, gl.is_blacklist FROM guests g "
            "JOIN guest_lists gl ON gl.id = g.list_id "
            "WHERE g.share_token = ? AND g.event_id = ?",
            (guest_token, door["link_event_id"]),
        ).fetchone()
        if not guest:
            raise APIError(404, "not_found", "Гость не найден.")
        match = _blacklist_match(conn, door["link_event_id"], guest)
        if match:
            return json_response({
                "guest": _door_guest_json(guest),
                "blacklisted": True,
                "blacklistMatch": _door_guest_json(match),
                "already": guest["status"] == "checked_in",
                "stats": _door_event_stats(conn, door["link_event_id"]),
            })
        already = guest["status"] == "checked_in"
        checked = _door_checkin(conn, door, guest)
        updated = _door_guest_row(conn, door["link_event_id"], guest["id"])
        return json_response({
            "guest": _door_guest_json(updated),
            "already": already or not checked,
            "stats": _door_event_stats(conn, door["link_event_id"]),
        })
    finally:
        conn.close()


@route(r"/api/door/([A-Za-z0-9_-]{16,80})/checkin", methods=("POST",))
def api_door_checkin(req, token):
    data = req.json()
    guest_id = data.get("guestId")
    if isinstance(guest_id, bool) or not isinstance(guest_id, int):
        raise APIError(400, "validation", "Укажите гостя.")
    conn = db.connect()
    try:
        door = _door_link_row(conn, token)
        guest = _door_guest_row(conn, door["link_event_id"], guest_id)
        if _blacklist_match(conn, door["link_event_id"], guest):
            raise APIError(403, "blacklisted",
                           "Гость в чёрном списке. Вход запрещён.")
        already = guest["status"] == "checked_in"
        checked = _door_checkin(conn, door, guest)
        updated = _door_guest_row(conn, door["link_event_id"], guest["id"])
        return json_response({
            "guest": _door_guest_json(updated),
            "already": already or not checked,
            "stats": _door_event_stats(conn, door["link_event_id"]),
        })
    finally:
        conn.close()


@route(r"/api/door/([A-Za-z0-9_-]{16,80})/undo", methods=("POST",))
def api_door_undo(req, token):
    data = req.json()
    guest_id = data.get("guestId")
    if isinstance(guest_id, bool) or not isinstance(guest_id, int):
        raise APIError(400, "validation", "Укажите гостя.")
    conn = db.connect()
    try:
        door = _door_link_row(conn, token)
        guest = _door_guest_row(conn, door["link_event_id"], guest_id)
        _door_undo(conn, door, guest)
        updated = _door_guest_row(conn, door["link_event_id"], guest["id"])
        return json_response({
            "guest": _door_guest_json(updated),
            "stats": _door_event_stats(conn, door["link_event_id"]),
        })
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


# Совместные сеты: «ZVUK (B2B) RAZNOS», «LUMEN (VS) MIRA»,
# «KIRA (f2f) NORD», «SOL (&) VERA», «RIFT (AND) NOVA».
# Маркер в скобках считается разделителем имён.
_ARTIST_SEPARATOR_TOKEN = r"(?:v\s*\.?\s*s\.?|b\s*2\s*b|f\s*2\s*f|&|and)"
_ARTIST_SEPARATOR_RE = re.compile(
    rf"\s*\(\s*{_ARTIST_SEPARATOR_TOKEN}\s*\)\s*",
    re.IGNORECASE,
)
_ARTIST_SEPARATOR_DISPLAY_RE = re.compile(
    rf"\s*\(\s*({_ARTIST_SEPARATOR_TOKEN})\s*\)\s*",
    re.IGNORECASE,
)


def _display_artist_name(name: str) -> str:
    """Убирает скобки у B2B/F2F/VS/AND-маркеров в расписании.

    «NOVA (b2b) ZVUK» показываем как «NOVA B2B ZVUK», «SOL (&) VERA» —
    как «SOL & VERA». Незнакомые скобки, например «DJ (Live)», остаются
    частью имени.
    """
    raw = (name or "").strip()
    if not raw:
        return ""

    def normalize_marker(match: re.Match) -> str:
        marker = re.sub(r"[\s.]+", "", match.group(1)).upper()
        return f" {marker} "

    text = _ARTIST_SEPARATOR_DISPLAY_RE.sub(normalize_marker, raw)
    return re.sub(r"\s+", " ", text).strip()


def _split_artist_names(name: str) -> list[str]:
    """Разбивает совместное название на отдельных артистов."""
    raw = (name or "").strip()
    if not raw:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for part in _ARTIST_SEPARATOR_RE.split(raw):
        part = re.sub(r"\s+", " ", part).strip(" \t,;/")
        if not part:
            continue
        key = _artist_key(part)
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return result or [raw]


def _public_artists(conn, now_iso: str, query: str = "") -> list[dict]:
    """Артисты ближайших мероприятий с актуальными сетами.

    Артист попадает в список, только если у него есть сет, который ещё не
    закончился, в опубликованном мероприятии, которое ещё не закончилось.
    Прошедшие мероприятия и отыгравшие сеты в разделе артистов не участвуют.
    """
    artists: dict[str, dict] = {}
    # Имена, которые главный администратор не удалил из базы исполнителей.
    # Удалённое имя не должно находиться в публичном разделе, даже если сет
    # остался в мероприятии.
    known = {
        _artist_key(row["name"])
        for row in conn.execute("SELECT name FROM performers")
        if (row["name"] or "").strip()
    }

    def entry_for(name: str) -> dict:
        key = _artist_key(name)
        item = artists.get(key)
        if item is None:
            item = {
                "name": name.strip(),
                "events": {},
            }
            artists[key] = item
        return item

    rows = conn.execute(
        "SELECT p.performer, p.replacement_performer, p.is_replacement, "
        "p.starts_at, p.ends_at, h.name AS hall_name, "
        "e.id AS event_id, e.slug, e.title, e.city, e.venue, e.timezone, "
        "e.starts_at AS event_starts, e.ends_at AS event_ends, "
        "e.published, e.lineup_visible, e.lineup_publish_at, e.lineup_filter "
        "FROM performances p "
        "JOIN halls h ON h.id = p.hall_id "
        "JOIN events e ON e.id = h.event_id "
        "WHERE e.published = 1 "
        "AND e.ends_at > ? AND p.ends_at > ?",
        (now_iso, now_iso),
    ).fetchall()
    for row in rows:
        if row["is_replacement"]:
            # У замены публикуем только имя того, кто её отыграет. Пока имя
            # не указано, слот не попадает в список артистов.
            name = (row["replacement_performer"] or "").strip()
        else:
            name = (row["performer"] or "").strip()
        if not name:
            continue
        if _artist_key(name) not in known:
            # Имя удалено из базы автозаполнения — не показываем артиста.
            continue
        if not _lineup_is_visible(row, now_iso):
            # Не раскрываем артистов из скрытого лайнапа.
            continue

        lineup_filter = (
            row["lineup_filter"] if row["lineup_filter"] in LINEUP_FILTERS
            else "all"
        )
        set_visible = _set_is_visible(row, now_iso, lineup_filter)

        for member in _split_artist_names(name):
            item = entry_for(member)
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

            if not set_visible:
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
        "user": {
            "id": session["user_id"],
            "username": session["username"],
            "isSuper": bool(session["is_super"]),
            "isPromoter": bool(session["is_promoter"]),
            "expiresAt": session["account_expires_at"],
        },
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
        purged = db.purge_expired_promoters(
            conn, timeutil.iso_now()[:10], keep_username=username)
        if purged:
            conn.commit()
            log.info("Удалены просроченные промоутеры: %s", ", ".join(purged))
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

        if row["expires_at"] and row["expires_at"] < timeutil.iso_now()[:10]:
            conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
            conn.commit()
            log.info("Удалён просроченный промоутер «%s»", row["username"])
            raise APIError(
                403, "expired",
                "Срок действия учётной записи истёк. Учётная запись удалена.",
            )

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
            "user": {
                "id": row["id"],
                "username": row["username"],
                "isSuper": bool(row["is_super"]),
                "isPromoter": bool(row["is_promoter"]),
                "expiresAt": row["expires_at"],
            },
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

def _normalize_expiry(raw):
    if raw is None or raw == "":
        return None
    try:
        parsed = date.fromisoformat(str(raw).strip())
    except ValueError:
        raise APIError(400, "validation", "Некорректная дата окончания.") from None
    if parsed.isoformat() < timeutil.iso_now()[:10]:
        raise APIError(400, "validation", "Дата окончания уже прошла.")
    return parsed.isoformat()


def _user_json(row) -> dict:
    keys = row.keys()
    expires = row["expires_at"] if "expires_at" in keys else None
    creator = row["created_by_username"] if "created_by_username" in keys else ""
    return {
        "id": row["id"],
        "username": row["username"],
        "isSuper": bool(row["is_super"]),
        "isPromoter": bool(row["is_promoter"]),
        "createdBy": row["created_by"] if "created_by" in keys else None,
        "createdByUsername": creator or "",
        "expiresAt": expires,
        "expired": bool(expires and expires < timeutil.iso_now()[:10]),
        "createdAt": row["created_at"],
    }


def _user_row(conn, user_id: int):
    row = conn.execute(
        "SELECT u.*, c.username AS created_by_username FROM users u "
        "LEFT JOIN users c ON c.id = u.created_by WHERE u.id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Пользователь не найден.")
    return row


@route(r"/api/admin/users")
def api_admin_users(req):
    session = _require_admin(req, csrf=False)
    if session["is_promoter"]:
        raise APIError(403, "forbidden",
                       "Промоутеру недоступно управление пользователями.")
    conn = db.connect()
    try:
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        base = (
            "SELECT u.*, c.username AS created_by_username FROM users u "
            "LEFT JOIN users c ON c.id = u.created_by "
        )
        if session["is_super"]:
            rows = conn.execute(
                base + "ORDER BY u.is_super DESC, u.is_promoter DESC, u.id"
            ).fetchall()
        else:
            rows = conn.execute(
                base + "WHERE u.is_promoter = 1 AND u.created_by = ? "
                "ORDER BY u.id",
                (session["user_id"],),
            ).fetchall()
        return json_response({"users": [_user_json(row) for row in rows]})
    finally:
        conn.close()


@route(r"/api/admin/users", methods=("POST",))
def api_admin_user_create(req):
    session = _require_admin(req)
    if session["is_promoter"]:
        raise APIError(403, "forbidden",
                       "Промоутер не может создавать учётные записи.")
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

    is_promoter = bool(data.get("isPromoter"))
    if not session["is_super"]:
        is_promoter = True
    expires_at = _normalize_expiry(data.get("expiresAt")) if is_promoter else None

    conn = db.connect()
    try:
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if exists:
            raise APIError(409, "conflict",
                           f"Пользователь «{username}» уже существует.")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_super, is_promoter, "
            "created_by, expires_at, created_at) "
            "VALUES (?, ?, 0, ?, ?, ?, ?)",
            (username, security.hash_password(password), 1 if is_promoter else 0,
             session["user_id"] if is_promoter else None, expires_at,
             timeutil.iso_now()),
        )
        kind = "промоутер" if is_promoter else "администратор"
        _audit(conn, session, "user.create", entity="user",
               entity_id=int(cur.lastrowid), detail=f"{username} · {kind}")
        conn.commit()
        row = _user_row(conn, int(cur.lastrowid))
    finally:
        conn.close()
    log.info("Добавлен %s «%s» (кем: %s)", kind, username, session["username"])
    return json_response({"user": _user_json(row)}, 201)


@route(r"/api/admin/users/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_user_update(req, user_id):
    session = _require_admin(req)
    if session["is_promoter"]:
        raise APIError(403, "forbidden",
                       "Промоутер не может менять пароли пользователей.")
    data = req.json()
    conn = db.connect()
    try:
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        row = _user_row(conn, int(user_id))
        if row["is_super"]:
            raise APIError(403, "forbidden",
                           "Пароль главного администратора меняется в разделе «Пароль».")
        if not session["is_super"] and (
                not row["is_promoter"]
                or row["created_by"] != session["user_id"]):
            raise APIError(403, "forbidden",
                           "Можно менять пароль только своим промоутерам.")
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
    log.info("Пароль пользователя «%s» изменён (кем: %s)",
             row["username"], session["username"])
    return json_response({"ok": True, "user": _user_json(row)})


@route(r"/api/admin/users/(\d{1,12})", methods=("DELETE",))
def api_admin_user_delete(req, user_id):
    session = _require_admin(req)
    if session["is_promoter"]:
        raise APIError(403, "forbidden",
                       "Промоутер не может удалять учётные записи.")
    conn = db.connect()
    try:
        row = _user_row(conn, int(user_id))
        if row["is_super"]:
            raise APIError(403, "forbidden",
                           "Главного администратора удалить нельзя.")
        if row["id"] == session["user_id"]:
            raise APIError(403, "forbidden", "Нельзя удалить самого себя.")
        if not session["is_super"] and (
                not row["is_promoter"]
                or row["created_by"] != session["user_id"]):
            raise APIError(403, "forbidden",
                           "Можно удалять только промоутеров, которых вы добавили.")
        moved = conn.execute(
            "SELECT COUNT(*) FROM events WHERE owner_id = ?", (row["id"],)
        ).fetchone()[0]
        # Мероприятия удалённого пользователя переходят действующему администратору.
        conn.execute(
            "UPDATE events SET owner_id = ? WHERE owner_id = ?",
            (session["user_id"], row["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        kind = "промоутера" if row["is_promoter"] else "администратора"
        _audit(conn, session, "user.delete", entity="user", entity_id=row["id"],
               detail=f"{row['username']} · {kind} · мероприятий передано: {moved}")
        conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    log.info("Удалён %s «%s»", kind, row["username"])
    return json_response({"ok": True, "deletedId": row["id"]})


# --------------------------------------------------------------------------
# Админка: оформление сайта (только главный)
# --------------------------------------------------------------------------

@route(r"/api/admin/site")
def api_admin_site(req):
    _require_super(req, csrf=False)
    conn = db.connect()
    try:
        data = {
            "links": _site_links(conn),
            "copyright": _site_copyright(conn),
            "copyrightDefault": _default_copyright(),
            "theme": _site_theme_info(conn),
            "themes": SITE_THEMES,
        }
        data.update(_site_logo_info(conn))
        data.update({
            "maxLinks": MAX_SITE_LINKS,
            "maxLogoBytes": MAX_LOGO_BYTES,
            "maxLogoSide": MAX_LOGO_SIDE,
        })
        return json_response(data)
    finally:
        conn.close()


@route(r"/api/admin/site/copyright", methods=("PUT", "PATCH"))
def api_admin_site_copyright(req):
    session = _require_super(req)
    data = req.json()
    text = data.get("text", "")
    if not isinstance(text, str):
        raise APIError(400, "validation", "Копирайт должен быть строкой.")
    text = text.strip()
    if len(text) > 200:
        raise APIError(400, "validation",
                       "Копирайт длиннее 200 символов.")
    if "\n" in text or "\r" in text:
        raise APIError(400, "validation",
                       "Копирайт должен быть в одну строку.")
    conn = db.connect()
    try:
        if text:
            conn.execute(
                "INSERT INTO site_settings (key, value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (SITE_COPYRIGHT_KEY, text, timeutil.iso_now()),
            )
        else:
            conn.execute(
                "DELETE FROM site_settings WHERE key = ?", (SITE_COPYRIGHT_KEY,))
        _audit(conn, session, "site.copyright", entity="site",
               detail=text or "сброшен к стандартному")
        conn.commit()
        return json_response({
            "copyright": _site_copyright(conn),
            "isDefault": not bool(text),
        })
    finally:
        conn.close()


@route(r"/api/admin/site/theme", methods=("PUT", "PATCH"))
def api_admin_site_theme(req):
    session = _require_super(req)
    data = req.json()
    mode = data.get("mode")
    if mode == "preset":
        theme = _theme_by_id(str(data.get("id") or ""))
        if not theme:
            raise APIError(400, "validation", "Неизвестная тема оформления.")
        setting = {"mode": "preset", "id": theme["id"]}
        detail = f"тема «{theme['name']}»"
    elif mode == "custom":
        colors = _custom_theme_colors(data.get("colors"))
        if not colors:
            raise APIError(400, "validation",
                           "Укажите корректные цвета в формате #RRGGBB.")
        setting = {"mode": "custom", "colors": colors}
        detail = "своя тема · " + ", ".join(
            f"{key}: {value}" for key, value in colors.items())
    else:
        raise APIError(400, "validation", "Выберите тему оформления.")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO site_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (SITE_THEME_KEY, json.dumps(setting, ensure_ascii=False,
                                        separators=(",", ":")),
             timeutil.iso_now()),
        )
        _audit(conn, session, "site.theme", entity="site", detail=detail)
        conn.commit()
        return json_response({"theme": _site_theme_info(conn)})
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
# Гостевые списки
# --------------------------------------------------------------------------

def _event_guest_role(conn, session, event_id: int):
    """Мероприятие и роль: owner | promoter. Чужие события — 404."""
    event = _event_row(conn, event_id)
    if session["is_super"] or event["owner_id"] == session["user_id"]:
        return event, "owner"
    assigned = conn.execute(
        "SELECT 1 FROM guest_list_promoters glp "
        "JOIN guest_lists gl ON gl.id = glp.list_id "
        "WHERE gl.event_id = ? AND glp.user_id = ? "
        "AND gl.is_blacklist = 0 LIMIT 1",
        (event["id"], session["user_id"]),
    ).fetchone()
    if assigned:
        return event, "promoter"
    raise APIError(404, "not_found", "Мероприятие не найдено.")


def _guest_list_row(conn, list_id: int):
    row = conn.execute(
        "SELECT gl.*, e.owner_id, e.slug AS event_slug, e.title AS event_title, "
        "e.starts_at AS event_starts, e.ends_at AS event_ends, "
        "e.timezone AS event_timezone, e.published "
        "FROM guest_lists gl "
        "JOIN events e ON e.id = gl.event_id "
        "WHERE gl.id = ?",
        (list_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Гостевой список не найден.")
    return row


def _guest_row(conn, guest_id: int):
    row = conn.execute(
        "SELECT g.*, gl.name AS list_name, gl.is_blacklist, e.owner_id, "
        "e.slug AS event_slug, e.title AS event_title, "
        "e.starts_at AS event_starts, e.ends_at AS event_ends, "
        "e.timezone AS event_timezone, e.published "
        "FROM guests g "
        "JOIN guest_lists gl ON gl.id = g.list_id "
        "JOIN events e ON e.id = g.event_id "
        "WHERE g.id = ?",
        (guest_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Гость не найден.")
    return row


def _guest_list_for_session(conn, session, list_id: int):
    row = _guest_list_row(conn, list_id)
    if session["is_super"] or row["owner_id"] == session["user_id"]:
        return row, "owner"
    if int(row["is_blacklist"]):
        raise APIError(404, "not_found", "Гостевой список не найден.")
    assigned = conn.execute(
        "SELECT 1 FROM guest_list_promoters WHERE list_id = ? AND user_id = ?",
        (row["id"], session["user_id"]),
    ).fetchone()
    if assigned:
        return row, "promoter"
    raise APIError(404, "not_found", "Гостевой список не найден.")


def _guest_for_session(conn, session, guest_id: int):
    row = _guest_row(conn, guest_id)
    list_row, role = _guest_list_for_session(conn, session, row["list_id"])
    return row, list_row, role


def _guest_search_clause(query: str, prefix: str = ""):
    clause = f" AND casefold({prefix}name) LIKE ?"
    args = [f"%{query.casefold()}%"]
    digits = re.sub(r"\D", "", query)
    if digits:
        clause = (f" AND (casefold({prefix}name) LIKE ? "
                  f"OR phone_digits({prefix}phone) LIKE ?)")
        args.append(f"%{digits}%")
    return clause, args


def _normalize_guest(data: dict, current=None) -> dict:
    current = dict(current) if current else {}
    name = _str_field(data, "name", "имя гостя", maxlen=120,
                      current=current.get("name", ""))
    phone = _str_field(data, "phone", "телефон", maxlen=40, required=False,
                       current=current.get("phone", ""))
    table_name = _str_field(data, "tableName", "стол", maxlen=40,
                            required=False,
                            current=current.get("table_name", ""))
    notes = _str_field(data, "notes", "заметку", maxlen=500, required=False,
                       current=current.get("notes", ""))
    status = current.get("status", "invited")
    if "status" in data:
        status = data.get("status")
        if status not in GUEST_STATUSES:
            raise APIError(400, "validation", "Некорректный статус гостя.")
    return {
        "name": name,
        "phone": phone,
        "table_name": table_name,
        "notes": notes,
        "status": status,
    }


def _new_guest_token(conn) -> str:
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM guests WHERE share_token = ?", (token,)
        ).fetchone()
        if not exists:
            return token
    raise APIError(500, "server_error", "Не удалось создать QR-код гостя.")


def _guest_json(row) -> dict:
    return {
        "id": row["id"],
        "listId": row["list_id"],
        "eventId": row["event_id"],
        "listName": row["list_name"],
        "listIsBlacklist": bool(row["is_blacklist"]),
        "name": row["name"],
        "phone": row["phone"],
        "tableName": row["table_name"],
        "notes": row["notes"],
        "status": row["status"],
        "shareToken": row["share_token"],
        "checkedInAt": row["checked_in_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _guest_list_json(conn, row) -> dict:
    stats = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN status = 'checked_in' THEN 1 ELSE 0 END), 0) "
        "AS arrived "
        "FROM guests WHERE list_id = ?",
        (row["id"],),
    ).fetchone()
    promoters = conn.execute(
        "SELECT u.id, u.username FROM guest_list_promoters glp "
        "JOIN users u ON u.id = glp.user_id "
        "WHERE glp.list_id = ? ORDER BY u.username COLLATE NOCASE",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "eventId": row["event_id"],
        "name": row["name"],
        "isBlacklist": bool(row["is_blacklist"]),
        "promoters": [
            {"id": item["id"], "username": item["username"]}
            for item in promoters
        ],
        "guestCount": stats["total"],
        "checkedInCount": stats["arrived"],
    }


def _registration_link_row(conn, link_id: int):
    row = conn.execute(
        "SELECT rl.*, e.title AS event_title, e.starts_at AS event_starts, "
        "e.ends_at AS event_ends, e.timezone AS event_timezone, e.published, "
        "gl.name AS list_name, gl.is_blacklist "
        "FROM guest_registration_links rl "
        "JOIN events e ON e.id = rl.event_id "
        "JOIN guest_lists gl ON gl.id = rl.list_id "
        "WHERE rl.id = ?",
        (link_id,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Ссылка на регистрацию не найдена.")
    return row


def _registration_link_by_token(conn, token: str):
    row = conn.execute(
        "SELECT rl.*, e.title AS event_title, e.starts_at AS event_starts, "
        "e.ends_at AS event_ends, e.timezone AS event_timezone, e.published, "
        "gl.name AS list_name, gl.is_blacklist "
        "FROM guest_registration_links rl "
        "JOIN events e ON e.id = rl.event_id "
        "JOIN guest_lists gl ON gl.id = rl.list_id "
        "WHERE rl.token = ?",
        (token,),
    ).fetchone()
    if not row or int(row["is_blacklist"]) or not int(row["published"]):
        raise APIError(404, "not_found", "Ссылка на регистрацию не найдена.")
    return row


def _registration_link_state(conn, row) -> dict:
    count = conn.execute(
        "SELECT COUNT(*) FROM guests WHERE registration_link_id = ?",
        (row["id"],),
    ).fetchone()[0]
    now = timeutil.iso_now()
    limit = int(row["max_registrations"]) if row["max_registrations"] else None
    limit_reached = limit is not None and count >= limit
    expired = bool(row["expires_at"] and row["expires_at"] < now)
    event_ended = row["event_ends"] < now
    published = bool(row["published"])
    active = bool(row["is_active"])
    reason = ""
    if not published:
        reason = "unpublished"
    elif event_ended:
        reason = "event_ended"
    elif expired:
        reason = "expired"
    elif limit_reached:
        reason = "limit"
    elif not active:
        reason = "closed"
    return {
        "registrationCount": count,
        "maxRegistrations": limit,
        "remaining": None if limit is None else max(0, limit - count),
        "expired": expired,
        "limitReached": limit_reached,
        "eventEnded": event_ended,
        "isActive": active,
        "canRegister": not reason,
        "reason": reason,
    }


def _registration_link_json(conn, row) -> dict:
    state = _registration_link_state(conn, row)
    return {
        "id": row["id"],
        "eventId": row["event_id"],
        "listId": row["list_id"],
        "listName": row["list_name"],
        "token": row["token"],
        "path": f"/r/{row['token']}",
        "comment": row["comment"],
        "expiresAt": row["expires_at"],
        "createdAt": row["created_at"],
        **state,
    }


def _registration_expiry(data: dict, event) -> str | None:
    mode = str(data.get("expiryMode") or "never").strip()
    if mode == "never":
        return None
    starts = timeutil.parse_iso(event["starts_at"])
    if mode == "start":
        expires = starts
    elif mode.startswith("start+"):
        try:
            hours = int(mode.split("+", 1)[1])
        except ValueError:
            raise APIError(400, "validation",
                           "Некорректный срок действия ссылки.") from None
        if hours < 1 or hours > 336:
            raise APIError(400, "validation",
                           "Срок после начала — от 1 до 336 часов.")
        expires = starts + timedelta(hours=hours)
    elif mode == "custom":
        raw = data.get("expiresAtLocal")
        if not isinstance(raw, str) or not raw.strip():
            raise APIError(400, "validation",
                           "Укажите дату и время окончания.")
        try:
            expires = timeutil.parse_local(raw.strip(), event["timezone"])
        except TimeError as exc:
            raise APIError(400, "validation", str(exc)) from None
    else:
        raise APIError(400, "validation",
                       "Некорректный срок действия ссылки.")
    iso = timeutil.to_iso(expires)
    if iso < timeutil.iso_now():
        raise APIError(400, "validation",
                       "Время окончания регистрации уже прошло.")
    return iso


def _registration_limit(raw) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise APIError(400, "validation",
                       "Лимит регистраций должен быть числом.")
    if raw < 1 or raw > 100000:
        raise APIError(400, "validation",
                       "Лимит регистраций — от 1 до 100000.")
    return raw


def _new_registration_token(conn) -> str:
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM guest_registration_links WHERE token = ?", (token,)
        ).fetchone()
        if not exists:
            return token
    raise APIError(500, "server_error",
                   "Не удалось создать ссылку на регистрацию.")


def _registration_closed_message(reason: str) -> str:
    return {
        "unpublished": "Регистрация недоступна.",
        "event_ended": "Мероприятие уже прошло.",
        "expired": "Срок действия ссылки истёк.",
        "limit": "Лимит регистраций по этой ссылке исчерпан.",
        "closed": "Регистрация по этой ссылке закрыта.",
    }.get(reason, "Регистрация закрыта.")


def _guest_public_json(row) -> dict:
    return {
        "guest": {
            "name": row["name"],
            "tableName": row["table_name"],
            "status": row["status"],
        },
        "event": {
            "title": row["event_title"],
            "slug": row["event_slug"],
            "city": row["event_city"],
            "venue": row["event_venue"],
            "startsAt": row["event_starts"],
            "endsAt": row["event_ends"],
            "timezone": row["event_timezone"],
        },
        "serverNow": timeutil.iso_now(),
    }


def _public_guest_row(conn, token: str):
    row = conn.execute(
        "SELECT g.*, gl.name AS list_name, gl.is_blacklist, e.slug AS event_slug, "
        "e.title AS event_title, e.city AS event_city, e.venue AS event_venue, "
        "e.starts_at AS event_starts, "
        "e.ends_at AS event_ends, e.timezone AS event_timezone, e.published "
        "FROM guests g "
        "JOIN guest_lists gl ON gl.id = g.list_id "
        "JOIN events e ON e.id = g.event_id "
        "WHERE g.share_token = ?",
        (token,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Приглашение не найдено.")
    if int(row["is_blacklist"]):
        raise APIError(404, "not_found", "Приглашение не найдено.")
    if not int(row["published"]):
        raise APIError(404, "not_found", "Приглашение не найдено.")
    if row["event_ends"] < timeutil.iso_now():
        raise APIError(404, "not_found", "Мероприятие уже прошло.")
    return row


def _door_link_row(conn, token: str):
    row = conn.execute(
        "SELECT d.event_id AS link_event_id, d.token AS link_token, "
        "d.created_at AS link_created_at, e.* "
        "FROM event_door_links d JOIN events e ON e.id = d.event_id "
        "WHERE d.token = ?",
        (token,),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found",
                       "Ссылка для охраны или персонала недействительна.")
    if row["ends_at"] < timeutil.iso_now():
        raise APIError(404, "not_found", "Мероприятие уже прошло.")
    return row


def _door_event_stats(conn, event_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN g.status = 'checked_in' THEN 1 ELSE 0 END), 0) "
        "AS arrived "
        "FROM guests g JOIN guest_lists gl ON gl.id = g.list_id "
        "WHERE g.event_id = ? AND gl.is_blacklist = 0", (event_id,)
    ).fetchone()
    return {
        "guestCount": row["total"],
        "checkedInCount": row["arrived"],
    }


def _door_event_json(conn, row) -> dict:
    return {
        "event": {
            "id": row["link_event_id"],
            "title": row["title"],
            "slug": row["slug"],
            "startsAt": row["starts_at"],
            "endsAt": row["ends_at"],
            "timezone": row["timezone"],
        },
        "stats": _door_event_stats(conn, row["link_event_id"]),
        "serverNow": timeutil.iso_now(),
    }


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if len(digits) <= 4:
        return digits
    return f"•••• {digits[-4:]}"


def _door_guest_json(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "phoneMasked": _mask_phone(row["phone"]),
        "listName": row["list_name"],
        "blacklisted": bool(row["is_blacklist"]),
        "tableName": row["table_name"],
        "notes": row["notes"],
        "status": row["status"],
        "checkedInAt": row["checked_in_at"],
    }


def _door_guest_row(conn, event_id: int, guest_id: int):
    row = conn.execute(
        "SELECT g.*, gl.name AS list_name, gl.is_blacklist FROM guests g "
        "JOIN guest_lists gl ON gl.id = g.list_id "
        "WHERE g.id = ? AND g.event_id = ?",
        (guest_id, event_id),
    ).fetchone()
    if not row:
        raise APIError(404, "not_found", "Гость не найден.")
    return row


def _blacklist_match(conn, event_id: int, guest):
    """Возвращает запись из чёрного списка, совпавшую с гостем."""
    if int(guest["is_blacklist"]):
        return guest
    phone = re.sub(r"\D", "", guest["phone"] or "")
    name = (guest["name"] or "").strip().casefold()
    rows = conn.execute(
        "SELECT g.*, gl.name AS list_name, gl.is_blacklist FROM guests g "
        "JOIN guest_lists gl ON gl.id = g.list_id "
        "WHERE g.event_id = ? AND gl.is_blacklist = 1 AND g.id != ?",
        (event_id, guest["id"]),
    ).fetchall()
    for row in rows:
        row_phone = re.sub(r"\D", "", row["phone"] or "")
        row_name = (row["name"] or "").strip().casefold()
        if len(phone) >= 4 and phone == row_phone:
            return row
        if len(name) >= 3 and name == row_name:
            return row
    return None


def _door_checkin(conn, door_row, guest) -> bool:
    if guest["status"] == "checked_in":
        return False
    now = timeutil.iso_now()
    conn.execute(
        "UPDATE guests SET status = 'checked_in', checked_in_at = ?, "
        "checked_in_by = NULL, updated_at = ? "
        "WHERE id = ? AND event_id = ? AND status != 'checked_in'",
        (now, now, guest["id"], door_row["link_event_id"]),
    )
    _audit(conn, {"user_id": None, "username": "door"},
           "guest.checkin", entity="guest", entity_id=guest["id"],
           event_id=door_row["link_event_id"],
           detail=(f"{door_row['title']} · {guest['list_name']} · "
                   f"{guest['name']} · вход"))
    conn.commit()
    return True


def _door_undo(conn, door_row, guest) -> None:
    now = timeutil.iso_now()
    conn.execute(
        "UPDATE guests SET status = 'invited', checked_in_at = NULL, "
        "checked_in_by = NULL, updated_at = ? WHERE id = ? AND event_id = ?",
        (now, guest["id"], door_row["link_event_id"]),
    )
    _audit(conn, {"user_id": None, "username": "door"},
           "guest.return", entity="guest", entity_id=guest["id"],
           event_id=door_row["link_event_id"],
           detail=f"{door_row['title']} · {guest['name']} · вход отменён")
    conn.commit()


def _new_door_token(conn) -> str:
    for _ in range(5):
        token = secrets.token_urlsafe(24)
        exists = conn.execute(
            "SELECT 1 FROM event_door_links WHERE token = ?", (token,)
        ).fetchone()
        if not exists:
            return token
    raise APIError(500, "server_error",
                   "Не удалось создать ссылку для персонала.")


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
                "SELECT DISTINCT e.* FROM events e "
                "LEFT JOIN guest_list_promoters glp ON glp.user_id = ? "
                "LEFT JOIN guest_lists gl ON gl.id = glp.list_id "
                "AND gl.event_id = e.id AND gl.is_blacklist = 0 "
                "WHERE e.owner_id = ? OR gl.id IS NOT NULL "
                "ORDER BY e.starts_at DESC",
                (session["user_id"], session["user_id"]),
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
                "guestRole": (
                    "owner"
                    if session["is_super"]
                    or row["owner_id"] == session["user_id"]
                    else "promoter"
                ),
            })
            events.append(item)
        return json_response({"events": events, "timezones": TIMEZONES,
                              "serverNow": timeutil.iso_now()})
    finally:
        conn.close()


@route(r"/api/admin/events", methods=("POST",))
def api_admin_event_create(req):
    session = _require_admin(req)
    if session["is_promoter"]:
        raise APIError(403, "forbidden",
                       "Промоутер не может создавать мероприятия.")
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
        db.ensure_blacklist(conn, event_id, session["user_id"])
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


@route(r"/api/admin/events/(\d{1,12})/qr\.png")
def api_admin_event_qr(req, event_id):
    """PNG с QR-кодом публичной страницы мероприятия.

    Файл не хранится на диске: код генерируется на каждый запрос и перестаёт
    отдаваться, когда мероприятие удалено, не опубликовано или уже прошло.
    """
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        row = _admin_event_row(conn, session, int(event_id))
        if not int(row["published"]):
            raise APIError(404, "not_found",
                           "QR-код доступен только для опубликованного мероприятия.")
        if timeutil.parse_iso(row["ends_at"]) <= timeutil.now_utc():
            raise APIError(404, "not_found",
                           "Мероприятие уже прошло — QR-код больше не доступен.")
        url = f"{req.scheme}://{req.host}/e/{row['slug']}"
        qr = segno.make(url, error="m")
        buffer = io.BytesIO()
        qr.save(buffer, kind="png", scale=20, border=4)
        filename = f"lineup-{row['slug']}-qr.png"
        return Response(
            buffer.getvalue(), 200, content_type="image/png",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
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


def _human_local(value, tz: str) -> str:
    """UTC-момент -> «ДД.ММ.ГГГГ ЧЧ:ММ» в поясе мероприятия."""
    date_part, time_part = timeutil.to_local_input(value, tz).split("T")
    year, month, day = date_part.split("-")
    return f"{day}.{month}.{year} {time_part}"


@route(r"/api/admin/events/(\d{1,12})/shift", methods=("POST",))
def api_admin_event_shift(req, event_id):
    """Переносит мероприятие на новое начало, сдвигая все сеты на тот же срок."""
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        row = _admin_event_row(conn, session, int(event_id))
        raw = data.get("startsAt")
        if not isinstance(raw, str) or not raw.strip():
            raise APIError(400, "validation",
                           "Укажите новое время начала мероприятия.")
        tz = row["timezone"]
        try:
            new_start = timeutil.parse_local(raw, tz)
        except TimeError as exc:
            raise APIError(400, "validation", str(exc)) from None
        old_start = timeutil.parse_iso(row["starts_at"])
        old_end = timeutil.parse_iso(row["ends_at"])
        delta = new_start - old_start
        if delta == timedelta(0):
            raise APIError(400, "validation",
                           "Новое время совпадает с текущим.")
        if new_start.year < 2000 or (old_end + delta).year > 2100:
            raise APIError(400, "validation", "Проверьте год начала.")

        rows = conn.execute(
            "SELECT p.id, p.starts_at, p.ends_at FROM performances p "
            "JOIN halls h ON h.id = p.hall_id WHERE h.event_id = ?",
            (row["id"],),
        ).fetchall()
        lineup_at = row["lineup_publish_at"]
        conn.execute(
            "UPDATE events SET starts_at = ?, ends_at = ?, lineup_publish_at = ? "
            "WHERE id = ?",
            (timeutil.to_iso(new_start), timeutil.to_iso(old_end + delta),
             (timeutil.to_iso(timeutil.parse_iso(lineup_at) + delta)
              if lineup_at else None),
             row["id"]),
        )
        for perf in rows:
            conn.execute(
                "UPDATE performances SET starts_at = ?, ends_at = ? WHERE id = ?",
                (timeutil.to_iso(timeutil.parse_iso(perf["starts_at"]) + delta),
                 timeutil.to_iso(timeutil.parse_iso(perf["ends_at"]) + delta),
                 perf["id"]),
            )
        _audit(conn, session, "event.shift", entity="event",
               entity_id=row["id"], event_id=row["id"],
               detail=(f"{row['title']} · {_human_local(old_start, tz)} → "
                       f"{_human_local(new_start, tz)} · "
                       f"сетов: {len(rows)}"))
        conn.commit()
        row = _event_row(conn, row["id"])
        log.info("Мероприятие #%s перенесено на %s (сетов: %s)",
                 row["id"], timeutil.to_iso(new_start), len(rows))
        return json_response({"event": _event_json(conn, row, admin=True),
                              "shiftedPerformances": len(rows)})
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
                "SELECT performer, replacement_performer, is_replacement "
                "FROM performances "
                "WHERE hall_id = ? ORDER BY starts_at, id", (hall["id"],),
            ).fetchall()
            names = ", ".join(_perf_display_name(p) for p in perfs)
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
# Админка: гостевые списки
# --------------------------------------------------------------------------

@route(r"/api/admin/events/(\d{1,12})/registration-links")
def api_admin_registration_links(req, event_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Ссылки на регистрацию доступны организатору.")
        rows = conn.execute(
            "SELECT rl.*, e.title AS event_title, e.starts_at AS event_starts, "
            "e.ends_at AS event_ends, e.timezone AS event_timezone, e.published, "
            "gl.name AS list_name, gl.is_blacklist "
            "FROM guest_registration_links rl "
            "JOIN events e ON e.id = rl.event_id "
            "JOIN guest_lists gl ON gl.id = rl.list_id "
            "WHERE rl.event_id = ? ORDER BY rl.id DESC",
            (event["id"],),
        ).fetchall()
        return json_response({
            "links": [_registration_link_json(conn, row) for row in rows],
        })
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/registration-links",
       methods=("POST",))
def api_admin_registration_link_create(req, event_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Создавать ссылки может только организатор.")
        list_id = data.get("listId")
        if isinstance(list_id, bool) or not isinstance(list_id, int):
            raise APIError(400, "validation", "Выберите гостевой список.")
        target = _guest_list_row(conn, list_id)
        if target["event_id"] != event["id"]:
            raise APIError(400, "validation",
                           "Список относится к другому мероприятию.")
        if int(target["is_blacklist"]):
            raise APIError(400, "validation",
                           "В чёрный список саморегистрация недоступна.")
        count = conn.execute(
            "SELECT COUNT(*) FROM guest_registration_links WHERE event_id = ?",
            (event["id"],),
        ).fetchone()[0]
        if count >= 100:
            raise APIError(400, "validation",
                           "Слишком много ссылок на регистрацию.")
        comment = data.get("comment", "")
        if comment is None:
            comment = ""
        if not isinstance(comment, str):
            raise APIError(400, "validation",
                           "Комментарий должен быть строкой.")
        comment = comment.strip()
        if len(comment) > 500:
            raise APIError(400, "validation",
                           "Комментарий — до 500 символов.")
        limit = _registration_limit(data.get("maxRegistrations"))
        expires_at = _registration_expiry(data, event)
        token = _new_registration_token(conn)
        now = timeutil.iso_now()
        cur = conn.execute(
            "INSERT INTO guest_registration_links (event_id, list_id, token, "
            "comment, max_registrations, expires_at, is_active, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (event["id"], target["id"], token, comment, limit, expires_at,
             session["user_id"], now),
        )
        _audit(conn, session, "registration.link.create",
               entity="registration_link", entity_id=int(cur.lastrowid),
               event_id=event["id"],
               detail=f"{event['title']} · {target['name']} · {comment or 'без комментария'}")
        conn.commit()
        row = _registration_link_row(conn, int(cur.lastrowid))
        return json_response({"link": _registration_link_json(conn, row)}, 201)
    finally:
        conn.close()


@route(r"/api/admin/registration-links/(\d{1,12})",
       methods=("PATCH", "PUT"))
def api_admin_registration_link_update(req, link_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        row = _registration_link_row(conn, int(link_id))
        event, role = _event_guest_role(conn, session, row["event_id"])
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Менять ссылку может только организатор.")
        if "isActive" in data:
            if not isinstance(data["isActive"], bool):
                raise APIError(400, "validation",
                               "Некорректный статус ссылки.")
            conn.execute(
                "UPDATE guest_registration_links SET is_active = ? WHERE id = ?",
                (1 if data["isActive"] else 0, row["id"]),
            )
        if "comment" in data:
            comment = data.get("comment")
            if not isinstance(comment, str):
                raise APIError(400, "validation",
                               "Комментарий должен быть строкой.")
            comment = comment.strip()
            if len(comment) > 500:
                raise APIError(400, "validation",
                               "Комментарий — до 500 символов.")
            conn.execute(
                "UPDATE guest_registration_links SET comment = ? WHERE id = ?",
                (comment, row["id"]),
            )
        _audit(conn, session, "registration.link.update",
               entity="registration_link", entity_id=row["id"],
               event_id=row["event_id"],
               detail=f"{event['title']} · {row['comment']}")
        conn.commit()
        updated = _registration_link_row(conn, row["id"])
        return json_response({"link": _registration_link_json(conn, updated)})
    finally:
        conn.close()


@route(r"/api/admin/registration-links/(\d{1,12})", methods=("DELETE",))
def api_admin_registration_link_delete(req, link_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        row = _registration_link_row(conn, int(link_id))
        event, role = _event_guest_role(conn, session, row["event_id"])
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Удалять ссылку может только организатор.")
        count = conn.execute(
            "SELECT COUNT(*) FROM guests WHERE registration_link_id = ?",
            (row["id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE guests SET registration_link_id = NULL "
            "WHERE registration_link_id = ?", (row["id"],))
        conn.execute(
            "DELETE FROM guest_registration_links WHERE id = ?", (row["id"],))
        _audit(conn, session, "registration.link.delete",
               entity="registration_link", entity_id=row["id"],
               event_id=row["event_id"],
               detail=(f"{event['title']} · {row['comment'] or 'без комментария'}"
                       f" · регистраций: {count}"))
        conn.commit()
        return json_response({
            "ok": True,
            "deletedId": row["id"],
            "registrations": count,
        })
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/guest-lists")
def api_admin_event_guest_lists(req, event_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role == "owner":
            db.ensure_blacklist(conn, event["id"], event["owner_id"])
            conn.commit()
            rows = conn.execute(
                "SELECT gl.*, e.owner_id, e.slug AS event_slug, "
                "e.title AS event_title, e.starts_at AS event_starts, "
                "e.ends_at AS event_ends, e.timezone AS event_timezone, "
                "e.published "
                "FROM guest_lists gl JOIN events e ON e.id = gl.event_id "
                "WHERE gl.event_id = ? "
                "ORDER BY gl.is_blacklist, gl.position, gl.id",
                (event["id"],),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT gl.*, e.owner_id, e.slug AS event_slug, "
                "e.title AS event_title, e.starts_at AS event_starts, "
                "e.ends_at AS event_ends, e.timezone AS event_timezone, "
                "e.published "
                "FROM guest_lists gl JOIN events e ON e.id = gl.event_id "
                "JOIN guest_list_promoters glp ON glp.list_id = gl.id "
                "WHERE gl.event_id = ? AND glp.user_id = ? "
                "AND gl.is_blacklist = 0 "
                "ORDER BY gl.position, gl.id",
                (event["id"], session["user_id"]),
            ).fetchall()
        return json_response({
            "event": {
                "id": event["id"],
                "title": event["title"],
                "slug": event["slug"],
                "startsAt": event["starts_at"],
                "endsAt": event["ends_at"],
                "timezone": event["timezone"],
                "published": bool(event["published"]),
            },
            "role": role,
            "lists": [_guest_list_json(conn, row) for row in rows],
        })
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/guest-lists", methods=("POST",))
def api_admin_guest_list_create(req, event_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Создавать списки может только организатор мероприятия.")
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        name = _str_field(data, "name", "название списка", maxlen=60)
        count = conn.execute(
            "SELECT COUNT(*) FROM guest_lists WHERE event_id = ?", (event["id"],)
        ).fetchone()[0]
        if count >= MAX_GUEST_LISTS:
            raise APIError(400, "validation",
                           f"Можно создать не больше {MAX_GUEST_LISTS} списков.")
        promoter_username = data.get("promoterUsername", "")
        if promoter_username is None:
            promoter_username = ""
        if not isinstance(promoter_username, str):
            raise APIError(400, "validation", "Логин промоутера должен быть строкой.")
        promoter_username = promoter_username.strip()
        promoter_user_id = None
        if promoter_username:
            user = conn.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                (promoter_username,),
            ).fetchone()
            if not user:
                raise APIError(400, "validation",
                               "Пользователь с таким логином не найден.")
            promoter_user_id = user["id"]
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM guest_lists "
            "WHERE event_id = ?", (event["id"],)
        ).fetchone()[0]
        now = timeutil.iso_now()
        cur = conn.execute(
            "INSERT INTO guest_lists (event_id, name, created_by, "
            "created_at, position) VALUES (?, ?, ?, ?, ?)",
            (event["id"], name, session["user_id"], now, position),
        )
        list_id = int(cur.lastrowid)
        if promoter_user_id:
            conn.execute(
                "INSERT OR IGNORE INTO guest_list_promoters "
                "(list_id, user_id, created_at) VALUES (?, ?, ?)",
                (list_id, promoter_user_id, now),
            )
        _audit(conn, session, "guest_list.create", entity="guest_list",
               entity_id=list_id, event_id=event["id"],
               detail=(f"{event['title']} · {name}"
                       + (f" · промоутер: {promoter_username}"
                          if promoter_username else "")))
        conn.commit()
        row = _guest_list_row(conn, list_id)
        return json_response({"list": _guest_list_json(conn, row)}, 201)
    finally:
        conn.close()


@route(r"/api/admin/guest-lists/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_guest_list_update(req, list_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        row, role = _guest_list_for_session(conn, session, int(list_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Менять список может только организатор мероприятия.")
        if int(row["is_blacklist"]):
            raise APIError(400, "validation",
                           "Чёрный список нельзя переименовать или назначить промоутерам.")
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        name = row["name"]
        if "name" in data:
            name = _str_field(data, "name", "название списка", maxlen=60,
                              current=row["name"])
        promoter_usernames = None
        if "promoterUsernames" in data:
            raw = data.get("promoterUsernames")
            if not isinstance(raw, list):
                raise APIError(400, "validation",
                               "Список промоутеров должен быть массивом логинов.")
            promoter_usernames = raw
        elif "promoterUsername" in data:
            raw = data.get("promoterUsername")
            if raw is None:
                raw = ""
            if not isinstance(raw, str):
                raise APIError(400, "validation",
                               "Логин промоутера должен быть строкой.")
            promoter_usernames = [raw] if raw.strip() else []
        if promoter_usernames is not None:
            if len(promoter_usernames) > 50:
                raise APIError(400, "validation", "Слишком много промоутеров.")
            resolved = []
            seen = set()
            for raw in promoter_usernames:
                if not isinstance(raw, str):
                    raise APIError(400, "validation",
                                   "Логин промоутера должен быть строкой.")
                username = raw.strip()
                if not username or username.casefold() in seen:
                    continue
                seen.add(username.casefold())
                user = conn.execute(
                    "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
                    (username,),
                ).fetchone()
                if not user:
                    raise APIError(400, "validation",
                                   f"Пользователь «{username}» не найден.")
                resolved.append(user)
            conn.execute(
                "DELETE FROM guest_list_promoters WHERE list_id = ?", (row["id"],))
            now = timeutil.iso_now()
            for user in resolved:
                conn.execute(
                    "INSERT INTO guest_list_promoters "
                    "(list_id, user_id, created_at) VALUES (?, ?, ?)",
                    (row["id"], user["id"], now),
                )
        conn.execute(
            "UPDATE guest_lists SET name = ? WHERE id = ?",
            (name, row["id"]),
        )
        _audit(conn, session, "guest_list.update", entity="guest_list",
               entity_id=row["id"], event_id=row["event_id"],
               detail=f"{row['event_title']} · {name}")
        conn.commit()
        return json_response({"list": _guest_list_json(
            conn, _guest_list_row(conn, row["id"]))})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/promoters", methods=("GET",))
def api_admin_event_promoters_list(req, event_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Список промоутеров доступен организатору.")
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        base = (
            "SELECT u.*, c.username AS created_by_username FROM users u "
            "LEFT JOIN users c ON c.id = u.created_by "
            "WHERE u.is_promoter = 1 "
        )
        if session["is_super"]:
            rows = conn.execute(base + "ORDER BY u.username COLLATE NOCASE"
                                ).fetchall()
        else:
            rows = conn.execute(
                base + "AND u.created_by = ? ORDER BY u.username COLLATE NOCASE",
                (session["user_id"],),
            ).fetchall()
        return json_response({"promoters": [_user_json(row) for row in rows]})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/promoters", methods=("POST",))
def api_admin_event_promoter_add(req, event_id):
    session = _require_admin(req)
    data = req.json()
    username = str(data.get("username") or "").strip()
    if not username:
        raise APIError(400, "validation", "Укажите логин промоутера.")
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Назначать промоутеров может только организатор.")
        purged = db.purge_expired_promoters(conn, timeutil.iso_now()[:10])
        if purged:
            conn.commit()
        user = conn.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not user:
            raise APIError(400, "validation",
                           f"Пользователь «{username}» не найден.")
        list_id = data.get("listId")
        if list_id is not None:
            if isinstance(list_id, bool) or not isinstance(list_id, int):
                raise APIError(400, "validation", "Некорректный список.")
            target = _guest_list_row(conn, list_id)
            if target["event_id"] != event["id"]:
                raise APIError(400, "validation",
                               "Список относится к другому мероприятию.")
            if int(target["is_blacklist"]):
                raise APIError(400, "validation",
                               "В чёрный список промоутеров не назначают.")
            targets = [target]
        else:
            targets = conn.execute(
                "SELECT * FROM guest_lists WHERE event_id = ? "
                "AND is_blacklist = 0 ORDER BY position, id",
                (event["id"],),
            ).fetchall()
        now = timeutil.iso_now()
        for target in targets:
            conn.execute(
                "INSERT OR IGNORE INTO guest_list_promoters "
                "(list_id, user_id, created_at) VALUES (?, ?, ?)",
                (target["id"], user["id"], now),
            )
        scope = "во все списки" if list_id is None else "в список"
        _audit(conn, session, "guest_list.promoter.add", entity="guest_list",
               event_id=event["id"],
               detail=f"{event['title']} · {user['username']} · {scope}")
        conn.commit()
        return json_response({
            "ok": True,
            "username": user["username"],
            "lists": len(targets),
        })
    finally:
        conn.close()


@route(r"/api/admin/guest-lists/(\d{1,12})/promoters/(\d{1,12})",
       methods=("DELETE",))
def api_admin_guest_list_promoter_remove(req, list_id, user_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        row, role = _guest_list_for_session(conn, session, int(list_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Убирать промоутеров может только организатор.")
        if int(row["is_blacklist"]):
            raise APIError(400, "validation",
                           "В чёрном списке нет промоутеров.")
        user = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if not user:
            raise APIError(404, "not_found", "Пользователь не найден.")
        deleted = conn.execute(
            "DELETE FROM guest_list_promoters WHERE list_id = ? AND user_id = ?",
            (row["id"], user["id"]),
        ).rowcount
        _audit(conn, session, "guest_list.promoter.remove", entity="guest_list",
               entity_id=row["id"], event_id=row["event_id"],
               detail=f"{row['event_title']} · {row['name']} · {user['username']}")
        conn.commit()
        return json_response({"ok": True, "deleted": bool(deleted)})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/promoters/(\d{1,12})",
       methods=("DELETE",))
def api_admin_event_promoter_remove(req, event_id, user_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Убирать промоутеров может только организатор.")
        user = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if not user:
            raise APIError(404, "not_found", "Пользователь не найден.")
        deleted = conn.execute(
            "DELETE FROM guest_list_promoters WHERE user_id = ? AND list_id IN "
            "(SELECT id FROM guest_lists WHERE event_id = ? AND is_blacklist = 0)",
            (user["id"], event["id"]),
        ).rowcount
        _audit(conn, session, "guest_list.promoter.remove", entity="guest_list",
               event_id=event["id"],
               detail=f"{event['title']} · {user['username']} · со всех списков")
        conn.commit()
        return json_response({"ok": True, "deleted": deleted})
    finally:
        conn.close()


@route(r"/api/admin/guest-lists/(\d{1,12})", methods=("DELETE",))
def api_admin_guest_list_delete(req, list_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        row, role = _guest_list_for_session(conn, session, int(list_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Удалять список может только организатор мероприятия.")
        if int(row["is_blacklist"]):
            raise APIError(400, "validation",
                           "Чёрный список удалить нельзя.")
        count = conn.execute(
            "SELECT COUNT(*) FROM guests WHERE list_id = ?", (row["id"],)
        ).fetchone()[0]
        _audit(conn, session, "guest_list.delete", entity="guest_list",
               entity_id=row["id"], event_id=row["event_id"],
               detail=f"{row['event_title']} · {row['name']} · гостей: {count}")
        conn.execute("DELETE FROM guest_lists WHERE id = ?", (row["id"],))
        conn.commit()
        return json_response({"ok": True, "deletedId": row["id"]})
    finally:
        conn.close()


@route(r"/api/admin/guest-lists/(\d{1,12})/guests")
def api_admin_guest_list_guests(req, list_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        row, role = _guest_list_for_session(conn, session, int(list_id))
        query = (parse_qs(req.query).get("q") or [""])[0].strip()[:80]
        sql = (
            "SELECT g.*, gl.name AS list_name, gl.is_blacklist "
            "FROM guests g JOIN guest_lists gl ON gl.id = g.list_id "
            "WHERE g.list_id = ?"
        )
        args = [row["id"]]
        if query:
            clause, extra = _guest_search_clause(query, "g.")
            sql += clause
            args.extend(extra)
        sql += (" ORDER BY CASE g.status WHEN 'checked_in' THEN 1 ELSE 0 END, "
                "g.name COLLATE NOCASE, g.id DESC")
        guests = conn.execute(sql, args).fetchall()
        return json_response({
            "list": _guest_list_json(conn, row),
            "role": role,
            "guests": [_guest_json(guest) for guest in guests],
        })
    finally:
        conn.close()


@route(r"/api/admin/guest-lists/(\d{1,12})/guests", methods=("POST",))
def api_admin_guest_create(req, list_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        row, role = _guest_list_for_session(conn, session, int(list_id))
        count = conn.execute(
            "SELECT COUNT(*) FROM guests WHERE list_id = ?", (row["id"],)
        ).fetchone()[0]
        if count >= MAX_GUESTS_PER_LIST:
            raise APIError(400, "validation",
                           f"В списке уже {MAX_GUESTS_PER_LIST} гостей.")
        fields = _normalize_guest(data)
        token = _new_guest_token(conn)
        now = timeutil.iso_now()
        cur = conn.execute(
            "INSERT INTO guests (list_id, event_id, name, phone, "
            "table_name, notes, status, share_token, created_by, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'invited', ?, ?, ?, ?)",
            (row["id"], row["event_id"], fields["name"], fields["phone"],
             fields["table_name"], fields["notes"],
             token, session["user_id"], now, now),
        )
        _audit(conn, session, "guest.create", entity="guest",
               entity_id=int(cur.lastrowid), event_id=row["event_id"],
               detail=(f"{row['event_title']} · {row['name']} · "
                       f"{fields['name']}"))
        conn.commit()
        guest = _guest_row(conn, int(cur.lastrowid))
        return json_response({
            "guest": _guest_json(guest),
            "list": _guest_list_json(conn, _guest_list_row(conn, row["id"])),
        }, 201)
    finally:
        conn.close()


@route(r"/api/admin/guests/(\d{1,12})", methods=("PATCH", "PUT"))
def api_admin_guest_update(req, guest_id):
    session = _require_admin(req)
    data = req.json()
    conn = db.connect()
    try:
        guest, row, role = _guest_for_session(conn, session, int(guest_id))
        fields = _normalize_guest(data, current=guest)
        target_id = guest["list_id"]
        target_list = row
        if "listId" in data:
            raw_list = data.get("listId")
            if isinstance(raw_list, bool) or not isinstance(raw_list, int):
                raise APIError(400, "validation", "Некорректный список.")
            target_list, _target_role = _guest_list_for_session(
                conn, session, raw_list)
            if target_list["event_id"] != guest["event_id"]:
                raise APIError(400, "validation",
                               "Список относится к другому мероприятию.")
            target_id = target_list["id"]
        now = timeutil.iso_now()
        old_status = guest["status"]
        new_status = fields["status"]
        checked_in_at = guest["checked_in_at"]
        checked_in_by = guest["checked_in_by"]
        target_blacklist = bool(int(target_list["is_blacklist"]))
        if new_status == "checked_in" and target_blacklist:
            raise APIError(400, "validation",
                           "Гостя из чёрного списка нельзя отметить как пришедшего.")
        if new_status == "checked_in" and old_status != "checked_in":
            checked_in_at = now
            checked_in_by = session["user_id"]
        elif new_status != "checked_in" or target_blacklist:
            new_status = "invited" if target_blacklist else new_status
            checked_in_at = None
            checked_in_by = None
        conn.execute(
            "UPDATE guests SET list_id = ?, name = ?, phone = ?, "
            "table_name = ?, notes = ?, status = ?, checked_in_at = ?, "
            "checked_in_by = ?, updated_at = ? WHERE id = ?",
            (target_id, fields["name"], fields["phone"],
             fields["table_name"], fields["notes"], new_status,
             checked_in_at, checked_in_by, now, guest["id"]),
        )
        moved = ""
        if target_id != guest["list_id"]:
            moved = f" · перенос: {guest['list_name']} → {target_list['name']}"
        if new_status == "checked_in" and old_status != "checked_in":
            action = "guest.checkin"
            detail = (f"{row['event_title']} · {row['name']} · "
                      f"{fields['name']}{moved}")
        elif new_status != "checked_in" and old_status == "checked_in":
            action = "guest.return"
            detail = (f"{row['event_title']} · {row['name']} · "
                      f"{fields['name']}{moved}")
        else:
            action = "guest.update"
            detail = (f"{row['event_title']} · {row['name']} · "
                      f"{fields['name']}{moved}")
        _audit(conn, session, action, entity="guest", entity_id=guest["id"],
               event_id=row["event_id"], detail=detail)
        conn.commit()
        updated = _guest_row(conn, guest["id"])
        return json_response({
            "guest": _guest_json(updated),
            "list": _guest_list_json(conn, _guest_list_row(conn, row["id"])),
        })
    finally:
        conn.close()


@route(r"/api/admin/guests/(\d{1,12})", methods=("DELETE",))
def api_admin_guest_delete(req, guest_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        guest, row, role = _guest_for_session(conn, session, int(guest_id))
        _audit(conn, session, "guest.delete", entity="guest",
               entity_id=guest["id"], event_id=row["event_id"],
               detail=f"{row['event_title']} · {row['name']} · {guest['name']}")
        conn.execute("DELETE FROM guests WHERE id = ?", (guest["id"],))
        conn.commit()
        return json_response({"ok": True, "deletedId": guest["id"]})
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/door-link")
def api_admin_door_link_get(req, event_id):
    session = _require_admin(req, csrf=False)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Ссылку для персонала создаёт организатор мероприятия.")
        row = conn.execute(
            "SELECT token, created_at FROM event_door_links WHERE event_id = ?",
            (event["id"],),
        ).fetchone()
        return json_response({
            "link": ({
                "path": f"/door/{row['token']}",
                "createdAt": row["created_at"],
            } if row else None),
        })
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/door-link", methods=("POST",))
def api_admin_door_link_create(req, event_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Ссылку для персонала создаёт организатор мероприятия.")
        token = _new_door_token(conn)
        now = timeutil.iso_now()
        conn.execute(
            "INSERT INTO event_door_links (event_id, token, created_by, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET "
            "token = excluded.token, created_by = excluded.created_by, "
            "created_at = excluded.created_at",
            (event["id"], token, session["user_id"], now),
        )
        _audit(conn, session, "door.link", entity="event",
               entity_id=event["id"], event_id=event["id"],
               detail=f"{event['title']} · ссылка для персонала обновлена")
        conn.commit()
        return json_response({
            "link": {"path": f"/door/{token}", "createdAt": now},
        }, 201)
    finally:
        conn.close()


@route(r"/api/admin/events/(\d{1,12})/door-link", methods=("DELETE",))
def api_admin_door_link_delete(req, event_id):
    session = _require_admin(req)
    conn = db.connect()
    try:
        event, role = _event_guest_role(conn, session, int(event_id))
        if role != "owner":
            raise APIError(403, "forbidden",
                           "Ссылку для персонала отзывает организатор мероприятия.")
        deleted = conn.execute(
            "DELETE FROM event_door_links WHERE event_id = ?", (event["id"],)
        ).rowcount
        if deleted:
            _audit(conn, session, "door.link", entity="event",
                   entity_id=event["id"], event_id=event["id"],
                   detail=f"{event['title']} · ссылка для персонала отозвана")
        conn.commit()
        return json_response({"ok": True, "deleted": bool(deleted)})
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


def _forget_performer_if_unused(conn: sqlite3.Connection, owner_id,
                                name: str) -> None:
    """Убирает имя из подсказок, если им больше не подписан ни один сет.

    У замены в подсказках участвует имя заменяющего диджея, а исходный
    артист остаётся только в истории и не показывается публично.
    """
    name = (name or "").strip()
    if not owner_id or not name:
        return
    still_used = conn.execute(
        "SELECT 1 FROM performances p "
        "JOIN halls h ON h.id = p.hall_id "
        "JOIN events e ON e.id = h.event_id "
        "WHERE e.owner_id = ? AND ("
        "(p.is_replacement = 0 AND p.performer = ? COLLATE NOCASE) OR "
        "(p.is_replacement = 1 AND p.replacement_performer = ? COLLATE NOCASE)"
        ") LIMIT 1",
        (owner_id, name, name),
    ).fetchone()
    if still_used:
        return
    conn.execute(
        "DELETE FROM performers WHERE owner_id = ? AND name = ? COLLATE NOCASE",
        (owner_id, name),
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
        name = _perf_display_name(conflict)
        raise APIError(
            409, "overlap",
            f"Время пересекается с «{name}» ({label}).",
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
        if "isReplacement" not in data:
            replacement = bool(perf["is_replacement"])
        else:
            value = data.get("isReplacement")
            if not isinstance(value, bool):
                raise APIError(400, "validation",
                               "Некорректный признак замены.")
            replacement = value
        old_replacement = _replacement_name(perf)
        if replacement:
            # Исходный артист остаётся в базе, но публично скрыт. В поле
            # замены можно указать, кто отыграет сет, или оставить пусто.
            performer = perf["performer"]
            replacement_performer = _str_field(
                data, "replacementPerformer", "имя замены", maxlen=120,
                required=False, current=old_replacement)
        else:
            performer = _str_field(data, "performer", "имя исполнителя",
                                   maxlen=120, current=perf["performer"])
            replacement_performer = ""
        starts, ends = _performance_times(
            data, tz, hall["event_starts"], hall["event_ends"], current=perf)
        _check_hall_overlap(conn, hall["id"], starts, ends, tz,
                            exclude_id=perf["id"])
        conn.execute(
            "UPDATE performances SET performer = ?, replacement_performer = ?, "
            "starts_at = ?, ends_at = ?, is_replacement = ? WHERE id = ?",
            (performer, replacement_performer or None, timeutil.to_iso(starts),
             timeutil.to_iso(ends), int(replacement), perf["id"]),
        )
        if replacement:
            _forget_performer_if_unused(conn, event["owner_id"], perf["performer"])
            if replacement_performer:
                _remember_performer(conn, event["owner_id"], replacement_performer)
        else:
            _remember_performer(conn, event["owner_id"], performer)
        if old_replacement and (not replacement
                                or old_replacement.casefold()
                                != replacement_performer.casefold()):
            _forget_performer_if_unused(conn, event["owner_id"], old_replacement)
        changes = []
        if perf["performer"] != performer:
            changes.append(f"{perf['performer']} → {performer}")
        if bool(perf["is_replacement"]) != replacement:
            changes.append("замена" if replacement else "артист вернулся")
        if old_replacement != replacement_performer:
            changes.append(
                f"замена: {old_replacement or '—'} → {replacement_performer or '—'}")
        label = (replacement_performer if replacement and replacement_performer
                 else ("Замена" if replacement else performer))
        _audit(conn, session, "performance.update", entity="performance",
               entity_id=perf["id"], event_id=event["id"],
               detail=(f"{event['title']} · {hall['name']} · "
                       + ("; ".join(changes) or label)))
        conn.commit()
        row = conn.execute("SELECT * FROM performances WHERE id = ?",
                           (perf["id"],)).fetchone()
        return json_response({"performance": _perf_admin_json(row, tz)})
    finally:
        conn.close()


def _perf_id_field(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise APIError(400, "validation", "Укажите два сета для обмена.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise APIError(400, "validation", "Укажите два сета для обмена.") from None
    if number <= 0:
        raise APIError(400, "validation", "Укажите два сета для обмена.")
    return number


@route(r"/api/admin/performances/swap", methods=("POST",))
def api_admin_perf_swap(req):
    """Меняет артистов местами, не трогая забронированное время сетов."""
    session = _require_admin(req)
    data = req.json()
    first_id = _perf_id_field(data, "firstId")
    second_id = _perf_id_field(data, "secondId")
    if first_id == second_id:
        raise APIError(400, "validation", "Выберите два разных сета.")
    conn = db.connect()
    try:
        first = _perf_row(conn, first_id)
        second = _perf_row(conn, second_id)
        if first["event_id"] != second["event_id"]:
            raise APIError(400, "validation",
                           "Менять местами можно сеты одного мероприятия.")
        event = _admin_event_row(conn, session, first["event_id"])
        if (first["performer"] == second["performer"]
                and _replacement_name(first) == _replacement_name(second)
                and bool(first["is_replacement"]) == bool(second["is_replacement"])):
            raise APIError(400, "validation",
                           "У выбранных сетов одинаковые артисты.")
        first_hall = _hall_row(conn, first["hall_id"])["name"]
        second_hall = _hall_row(conn, second["hall_id"])["name"]
        conn.execute(
            "UPDATE performances SET performer = ?, replacement_performer = ?, "
            "is_replacement = ? WHERE id = ?",
            (second["performer"], second["replacement_performer"],
             second["is_replacement"], first["id"]),
        )
        conn.execute(
            "UPDATE performances SET performer = ?, replacement_performer = ?, "
            "is_replacement = ? WHERE id = ?",
            (first["performer"], first["replacement_performer"],
             first["is_replacement"], second["id"]),
        )
        _audit(conn, session, "performance.swap", entity="performance",
               entity_id=first["id"], event_id=event["id"],
               detail=(f"{event['title']} · "
                       f"{_perf_display_name(first)} ({first_hall}) ↔ "
                       f"{_perf_display_name(second)} ({second_hall})"))
        conn.commit()
        rows = [_perf_row(conn, first_id), _perf_row(conn, second_id)]
        log.info("Артисты #%s и #%s поменялись местами (мероприятие #%s)",
                 first_id, second_id, event["id"])
        return json_response({
            "performances": [
                _perf_admin_json(row, row["event_timezone"]) for row in rows
            ],
        })
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
               detail=f"{event['title']} · {hall['name']} · "
                      f"{_perf_display_name(perf)}")
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
