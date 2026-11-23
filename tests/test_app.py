"""Интеграционные тесты LineUP (stdlib unittest, без внешних зависимостей)."""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.TemporaryDirectory()
os.environ["LINEUP_DB"] = str(Path(_tmp.name) / "test.db")
os.environ["LINEUP_AUTO_INIT"] = "1"
os.environ["LINEUP_COOKIE_SECURE"] = "auto"
os.environ["LINEUP_TRUST_PROXY"] = "0"
os.environ["LINEUP_LOGIN_MAX_FAILURES"] = "3"
os.environ["LINEUP_LOGIN_MAX_USER_FAILURES"] = "100"
os.environ["LINEUP_LOGIN_WINDOW"] = "900"

import app as appmod  # noqa: E402
import db  # noqa: E402
import security  # noqa: E402
import timeutil  # noqa: E402

PASSWORD = "correct-horse-battery"

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Client:
    """Мини-клиент поверх WSGI-приложения."""

    def __init__(self, testcase):
        self.testcase = testcase
        self.cookies = {}
        self.csrf = None
        self.last_body = b""

    def request(self, method, path, body=None, *, csrf=None,
                scheme="http", ip="127.0.0.1"):
        if "?" in path:
            path, query = path.split("?", 1)
        else:
            query = ""
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": scheme,
            "wsgi.input": io.BytesIO(payload),
            "wsgi.errors": io.StringIO(),
            "REMOTE_ADDR": ip,
            "HTTP_HOST": "testserver",
            "CONTENT_LENGTH": str(len(payload)),
            "CONTENT_TYPE": "application/json",
        }
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        if csrf:
            environ["HTTP_X_CSRF_TOKEN"] = csrf

        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        chunks = appmod.app(environ, start_response)
        raw = b"".join(chunks)
        self.last_body = raw
        headers = {name.lower(): value for name, value in captured["headers"]}

        cookie = headers.get("set-cookie")
        if cookie:
            pair = cookie.split(";", 1)[0]
            if "=" in pair:
                name, value = pair.split("=", 1)
                if value:
                    self.cookies[name] = value
                else:
                    self.cookies.pop(name, None)

        data = None
        if raw and "json" in headers.get("content-type", ""):
            data = json.loads(raw.decode("utf-8"))
        return int(captured["status"].split()[0]), headers, data

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body, **kwargs)

    def patch(self, path, body=None, **kwargs):
        return self.request("PATCH", path, body, **kwargs)

    def put(self, path, body=None, **kwargs):
        return self.request("PUT", path, body, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


class LineUPTestCase(unittest.TestCase):
    def setUp(self):
        db.init()
        conn = db.connect()
        for table in ("site_links", "site_settings", "audit_log", "performers",
                      "performances", "halls", "event_links", "events",
                      "sessions", "login_attempts", "users"):
            conn.execute(f"DELETE FROM {table}")
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, is_super, created_at) "
            "VALUES (?, ?, 1, ?)",
            ("admin", security.hash_password(PASSWORD), timeutil.iso_now()),
        )
        self.super_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        self.client = Client(self)

    # -- helpers ---------------------------------------------------------

    def login(self, password=PASSWORD, scheme="http"):
        status, headers, data = self.client.post(
            "/api/admin/login", {"username": "admin", "password": password},
            scheme=scheme,
        )
        self.assertEqual(status, 200, data)
        self.client.csrf = data["csrfToken"]
        return headers

    def create_event_as(self, client, **overrides):
        payload = {
            "title": "DNB NIGHT",
            "city": "Москва",
            "venue": "бар Звезда",
            "address": "ул. Тверская, 12",
            "description": "Ночь драм-н-бейса",
            "timezone": "Europe/Moscow",
            "startsAt": "2027-11-04T20:00",
            "endsAt": "2027-11-05T06:00",
            "published": True,
        }
        payload.update(overrides)
        status, _, data = client.post(
            "/api/admin/events", payload, csrf=client.csrf)
        self.assertEqual(status, 201, data)
        return data["event"]

    def create_event(self, **overrides):
        self.login()
        return self.create_event_as(self.client, **overrides)

    def add_hall(self, event_id, name="Зал 1"):
        status, _, data = self.client.post(
            f"/api/admin/events/{event_id}/halls",
            {"name": name}, csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        return data["hall"]

    def add_perf(self, hall_id, performer, starts, ends, expect=201):
        status, _, data = self.client.post(
            f"/api/admin/halls/{hall_id}/performances",
            {"performer": performer, "startsAt": starts, "endsAt": ends},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, expect, data)
        return data

    def create_admin(self, username="helper", password="helper-password-1"):
        self.login()
        status, _, data = self.client.post(
            "/api/admin/users",
            {"username": username, "password": password},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        return data["user"]

    def create_promoter(self, username="promo", password="promo-password-1",
                        expires_at=None, client=None):
        agent = client or self.client
        if client is None:
            self.login()
        payload = {
            "username": username,
            "password": password,
            "isPromoter": True,
        }
        if expires_at:
            payload["expiresAt"] = expires_at
        status, _, data = agent.post(
            "/api/admin/users", payload, csrf=agent.csrf)
        self.assertEqual(status, 201, data)
        return data["user"]

    def login_as(self, username, password):
        client = Client(self)
        status, _, data = client.post(
            "/api/admin/login", {"username": username, "password": password})
        self.assertEqual(status, 200, data)
        client.csrf = data["csrfToken"]
        return client

    # -- public ----------------------------------------------------------

    def test_empty_site(self):
        status, headers, data = self.client.get("/api/public/featured")
        self.assertEqual(status, 200)
        self.assertIsNone(data["event"])
        self.assertEqual(data["others"], [])
        self.assertIn("serverNow", data)
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_pages_are_served(self):
        for path in ("/", "/e/dnb-night", "/admin", "/artists"):
            status, headers, _ = self.client.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers["content-type"])
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])

    def test_no_csrf_is_rejected(self):
        self.login()
        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "X", "venue": "Y", "startsAt": "2027-01-01T20:00",
             "endsAt": "2027-01-01T23:00"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "csrf")

    def test_foreign_origin_is_rejected(self):
        self.login()

        def bad_origin_request():
            payload = json.dumps({"title": "X", "venue": "Y",
                                  "startsAt": "2027-01-01T20:00",
                                  "endsAt": "2027-01-01T23:00"}).encode()
            environ = {
                "REQUEST_METHOD": "POST", "PATH_INFO": "/api/admin/events",
                "QUERY_STRING": "", "SERVER_NAME": "testserver",
                "SERVER_PORT": "80", "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(payload), "REMOTE_ADDR": "127.0.0.1",
                "HTTP_HOST": "testserver", "CONTENT_LENGTH": str(len(payload)),
                "CONTENT_TYPE": "application/json",
                "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.client.cookies.items()),
                "HTTP_X_CSRF_TOKEN": self.client.csrf,
                "HTTP_ORIGIN": "https://evil.example",
            }
            captured = {}
            appmod.app(environ, lambda s, h, e=None: captured.update(status=s))
            return int(captured["status"].split()[0])

        self.assertEqual(bad_origin_request(), 403)

    # -- auth ------------------------------------------------------------

    def test_login_rejects_bad_password(self):
        status, _, data = self.client.post(
            "/api/admin/login", {"username": "admin", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "invalid_credentials")

    def test_login_cookie_flags(self):
        headers = self.login()
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)

        secure_headers = self.login(scheme="https")
        self.assertIn("Secure", secure_headers["set-cookie"])

    def test_session_endpoint(self):
        status, _, _ = self.client.get("/api/admin/session")
        self.assertEqual(status, 401)
        self.login()
        status, _, data = self.client.get("/api/admin/session")
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["user"]["username"], "admin")
        self.assertTrue(data["csrfToken"])

    def test_admin_requires_session(self):
        status, _, data = self.client.get("/api/admin/events")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

    def test_login_rate_limit(self):
        for _ in range(3):
            status, _, _ = self.client.post(
                "/api/admin/login", {"username": "admin", "password": "nope"})
            self.assertEqual(status, 401)
        status, headers, data = self.client.post(
            "/api/admin/login", {"username": "admin", "password": "nope"})
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)

    def test_password_change_revokes_other_sessions(self):
        self.login()
        other = Client(self)
        status, _, data = other.post(
            "/api/admin/login", {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        other.csrf = data["csrfToken"]

        status, _, data = self.client.post(
            "/api/admin/password",
            {"currentPassword": PASSWORD, "newPassword": "brand-new-secret-1"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)

        status, _, _ = other.get("/api/admin/events")
        self.assertEqual(status, 401)
        status, _, _ = self.client.get("/api/admin/events")
        self.assertEqual(status, 200)

    # -- CRUD ------------------------------------------------------------

    def test_event_create_and_timezone_conversion(self):
        event = self.create_event()
        self.assertEqual(event["startsAt"], "2027-11-04T17:00:00Z")
        self.assertEqual(event["endsAt"], "2027-11-05T03:00:00Z")
        self.assertEqual(event["startsAtLocal"], "2027-11-04T20:00")
        self.assertEqual(event["slug"], "dnb-night")

    def test_full_schedule_flow(self):
        event = self.create_event()
        hall1 = self.add_hall(event["id"], "Зал 1")
        hall2 = self.add_hall(event["id"], "Зал 2")

        self.add_perf(hall1["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(hall1["id"], "DJ Smash", "2027-11-04T21:00", "2027-11-04T22:00")
        self.add_perf(hall2["id"], "DJ Rob", "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/public/featured")
        self.assertEqual(status, 200)
        public_event = data["event"]
        self.assertEqual(public_event["title"], "DNB NIGHT")
        self.assertEqual(len(public_event["halls"]), 2)
        self.assertEqual(public_event["halls"][0]["name"], "Зал 1")
        self.assertEqual(len(public_event["halls"][0]["performances"]), 2)
        self.assertEqual(
            public_event["halls"][0]["performances"][0]["startsAt"],
            "2027-11-04T17:00:00Z",
        )
        self.assertNotIn("startsAtLocal", public_event["halls"][0]["performances"][0])

        status, _, data = self.client.get("/e/dnb-night")
        self.assertEqual(status, 200)

        status, _, data = self.client.get("/api/public/events/dnb-night")
        self.assertEqual(status, 200)
        self.assertEqual(data["event"]["slug"], "dnb-night")

    def test_overlap_is_rejected(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T22:00")
        status, _, data = self.client.post(
            f"/api/admin/halls/{hall['id']}/performances",
            {"performer": "DJ Rob", "startsAt": "2027-11-04T21:00",
             "endsAt": "2027-11-04T23:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "overlap")
        self.assertIn("DJ Vik", data["error"]["message"])

    def test_performance_must_be_inside_event(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        status, _, data = self.client.post(
            f"/api/admin/halls/{hall['id']}/performances",
            {"performer": "Too Late", "startsAt": "2027-11-05T05:00",
             "endsAt": "2027-11-05T07:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400)
        self.assertIn("внутри", data["error"]["message"])

    def test_performance_requires_end(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        status, _, data = self.client.post(
            f"/api/admin/halls/{hall['id']}/performances",
            {"performer": "No End", "startsAt": "2027-11-04T21:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400)
        self.assertIn("окончания", data["error"]["message"])

    def test_swap_keeps_times_and_swaps_artists(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        first = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        second = self.add_perf(
            hall["id"], "DJ Rob", "2027-11-04T21:00", "2027-11-04T22:00"
        )["performance"]

        status, _, data = self.client.post(
            "/api/admin/performances/swap",
            {"firstId": first["id"], "secondId": second["id"]},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        by_id = {item["id"]: item for item in data["performances"]}
        self.assertEqual(by_id[first["id"]]["performer"], "DJ Rob")
        self.assertEqual(by_id[second["id"]]["performer"], "DJ Vik")
        self.assertEqual(by_id[first["id"]]["startsAt"], first["startsAt"])
        self.assertEqual(by_id[first["id"]]["endsAt"], first["endsAt"])
        self.assertEqual(by_id[second["id"]]["startsAt"], second["startsAt"])
        self.assertEqual(by_id[second["id"]]["endsAt"], second["endsAt"])

        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertEqual(status, 200, data)
        rows = data["event"]["halls"][0]["performances"]
        self.assertEqual([row["performer"] for row in rows], ["DJ Rob", "DJ Vik"])
        self.assertEqual([row["startsAt"] for row in rows],
                         [first["startsAt"], second["startsAt"]])

    def test_swap_rejects_other_event_and_same_set(self):
        first_event = self.create_event(title="FIRST NIGHT")
        first_hall = self.add_hall(first_event["id"])
        first = self.add_perf(
            first_hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        second_event = self.create_event(
            title="SECOND NIGHT",
            startsAt="2027-12-04T20:00",
            endsAt="2027-12-05T02:00",
        )
        second_hall = self.add_hall(second_event["id"])
        second = self.add_perf(
            second_hall["id"], "DJ Rob", "2027-12-04T20:00", "2027-12-04T21:00"
        )["performance"]

        status, _, data = self.client.post(
            "/api/admin/performances/swap",
            {"firstId": first["id"], "secondId": second["id"]},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)

        status, _, data = self.client.post(
            "/api/admin/performances/swap",
            {"firstId": first["id"], "secondId": first["id"]},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)

    def test_replacement_hides_name_from_public_artists(self):
        event = self.create_event(title="REPLACED NIGHT")
        hall = self.add_hall(event["id"])
        perf = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]

        status, _, data = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {
                "isReplacement": True,
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        updated = data["performance"]
        self.assertTrue(updated["isReplacement"])
        self.assertEqual(updated["performer"], "DJ Vik")
        self.assertEqual(updated["displayPerformer"], "Замена")

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        public_perf = data["event"]["halls"][0]["performances"][0]
        self.assertEqual(public_perf["displayPerformer"], "Замена")
        self.assertTrue(public_perf["isReplacement"])

        status, _, data = self.client.get("/api/public/artists")
        names = [item["name"] for item in data["artists"]]
        self.assertNotIn("DJ Vik", names)
        self.assertNotIn("Замена", names)

        status, _, data = self.client.get("/api/admin/performers")
        self.assertNotIn("DJ Vik", data["performers"])
        self.assertNotIn("Замена", data["performers"])

        # Возврат артиста снова показывает его публично и в подсказках.
        status, _, data = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {
                "isReplacement": False,
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["performance"]["displayPerformer"], "DJ Vik")
        status, _, data = self.client.get("/api/public/artists")
        self.assertIn("DJ Vik", [item["name"] for item in data["artists"]])

    def test_replacement_keeps_shared_artist_in_database(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        first = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T21:00", "2027-11-04T22:00"
        )
        status, _, data = self.client.patch(
            f"/api/admin/performances/{first['id']}",
            {
                "isReplacement": True,
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("DJ Vik", data["performers"])

    def test_replacement_can_name_the_new_artist(self):
        event = self.create_event(title="REPLACEMENT NAME")
        hall = self.add_hall(event["id"])
        perf = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]

        status, _, data = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {
                "isReplacement": True,
                "replacementPerformer": "DJ Nova",
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        updated = data["performance"]
        self.assertEqual(updated["performer"], "DJ Vik")
        self.assertEqual(updated["replacementPerformer"], "DJ Nova")
        self.assertEqual(updated["displayPerformer"], "DJ Nova")
        self.assertTrue(updated["isReplacement"])

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        public_perf = data["event"]["halls"][0]["performances"][0]
        self.assertEqual(public_perf["displayPerformer"], "DJ Nova")
        self.assertEqual(public_perf["replacementPerformer"], "DJ Nova")
        self.assertTrue(public_perf["isReplacement"])

        status, _, data = self.client.get("/api/public/artists")
        names = [item["name"] for item in data["artists"]]
        self.assertIn("DJ Nova", names)
        self.assertNotIn("DJ Vik", names)

        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("DJ Nova", data["performers"])
        self.assertNotIn("DJ Vik", data["performers"])

        # Пустое имя: замена ещё не найдена, публично показываем «Замена».
        status, _, data = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {
                "isReplacement": True,
                "replacementPerformer": "",
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["performance"]["displayPerformer"], "Замена")
        self.assertEqual(data["performance"]["replacementPerformer"], "")
        status, _, data = self.client.get("/api/public/artists")
        self.assertNotIn("DJ Nova", [item["name"] for item in data["artists"]])

        # Возврат артиста очищает замену и возвращает исходное имя.
        status, _, data = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {
                "isReplacement": False,
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["performance"]["displayPerformer"], "DJ Vik")
        self.assertEqual(data["performance"]["replacementPerformer"], "")
        status, _, data = self.client.get("/api/public/artists")
        self.assertIn("DJ Vik", [item["name"] for item in data["artists"]])

    def test_swap_moves_replacement_state_and_name(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        first = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        second = self.add_perf(
            hall["id"], "DJ Rob", "2027-11-04T21:00", "2027-11-04T22:00"
        )["performance"]

        status, _, data = self.client.patch(
            f"/api/admin/performances/{first['id']}",
            {
                "isReplacement": True,
                "replacementPerformer": "DJ Nova",
                "startsAt": "2027-11-04T20:00",
                "endsAt": "2027-11-04T21:00",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)

        status, _, data = self.client.post(
            "/api/admin/performances/swap",
            {"firstId": first["id"], "secondId": second["id"]},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        by_id = {item["id"]: item for item in data["performances"]}
        self.assertEqual(by_id[first["id"]]["performer"], "DJ Rob")
        self.assertFalse(by_id[first["id"]]["isReplacement"])
        self.assertEqual(by_id[first["id"]]["replacementPerformer"], "")
        self.assertEqual(by_id[second["id"]]["performer"], "DJ Vik")
        self.assertTrue(by_id[second["id"]]["isReplacement"])
        self.assertEqual(by_id[second["id"]]["replacementPerformer"], "DJ Nova")

    def test_foreign_admin_cannot_swap_or_replace(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        first = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        second = self.add_perf(
            hall["id"], "DJ Rob", "2027-11-04T21:00", "2027-11-04T22:00"
        )["performance"]

        self.create_admin()
        client = self.login_as("helper", "helper-password-1")

        status, _, data = client.post(
            "/api/admin/performances/swap",
            {"firstId": first["id"], "secondId": second["id"]},
            csrf=client.csrf,
        )
        self.assertEqual(status, 404, data)

        status, _, data = client.patch(
            f"/api/admin/performances/{first['id']}",
            {"isReplacement": True},
            csrf=client.csrf,
        )
        self.assertEqual(status, 404, data)

        status, _, _ = client.get(f"/api/admin/events/{event['id']}/qr.png")
        self.assertEqual(status, 404)

    def test_shift_moves_event_and_all_sets(self):
        event = self.create_event()
        hall1 = self.add_hall(event["id"], "Зал 1")
        hall2 = self.add_hall(event["id"], "Зал 2")
        first = self.add_perf(
            hall1["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00"
        )["performance"]
        second = self.add_perf(
            hall1["id"], "DJ Rob", "2027-11-04T21:00", "2027-11-04T22:00"
        )["performance"]
        third = self.add_perf(
            hall2["id"], "DJ Smash", "2027-11-04T20:00", "2027-11-04T22:00"
        )["performance"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "2027-11-11T22:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        shifted = data["event"]
        self.assertEqual(data["shiftedPerformances"], 3)
        self.assertEqual(shifted["startsAt"], "2027-11-11T19:00:00Z")
        self.assertEqual(shifted["endsAt"], "2027-11-12T05:00:00Z")
        self.assertEqual(shifted["startsAtLocal"], "2027-11-11T22:00")
        self.assertEqual(shifted["endsAtLocal"], "2027-11-12T08:00")

        rows = {
            row["id"]: row
            for hall in shifted["halls"]
            for row in hall["performances"]
        }
        # Порядок и длительность сохранились, время сдвинулось на 7 дней 2 часа.
        self.assertEqual(rows[first["id"]]["startsAtLocal"], "2027-11-11T22:00")
        self.assertEqual(rows[first["id"]]["endsAtLocal"], "2027-11-11T23:00")
        self.assertEqual(rows[second["id"]]["startsAtLocal"], "2027-11-11T23:00")
        self.assertEqual(rows[second["id"]]["endsAtLocal"], "2027-11-12T00:00")
        self.assertEqual(rows[third["id"]]["startsAtLocal"], "2027-11-11T22:00")
        self.assertEqual(rows[third["id"]]["endsAtLocal"], "2027-11-12T00:00")
        self.assertEqual(rows[first["id"]]["performer"], "DJ Vik")
        self.assertEqual(rows[second["id"]]["performer"], "DJ Rob")

        status, _, log = self.client.get("/api/admin/audit")
        self.assertEqual(status, 200, log)
        self.assertEqual(log["entries"][0]["action"], "event.shift")
        self.assertIn("04.11.2027 20:00", log["entries"][0]["detail"])
        self.assertIn("11.11.2027 22:00", log["entries"][0]["detail"])

        status, _, public = self.client.get("/api/public/events/dnb-night")
        self.assertEqual(status, 200, public)
        self.assertEqual(public["event"]["startsAt"], "2027-11-11T19:00:00Z")

    def test_shift_keeps_relative_schedule_inside_new_bounds(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "Early", "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(hall["id"], "Late", "2027-11-05T05:00", "2027-11-05T05:45")

        # Сдвиг на сутки назад: сеты остаются внутри нового интервала.
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "2027-11-03T20:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        rows = data["event"]["halls"][0]["performances"]
        self.assertEqual(
            [(row["startsAtLocal"], row["endsAtLocal"]) for row in rows],
            [("2027-11-03T20:00", "2027-11-03T21:00"),
             ("2027-11-04T05:00", "2027-11-04T05:45")],
        )

    def test_shift_validation(self):
        event = self.create_event()
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "2027-11-04T20:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)
        self.assertIn("совпадает", data["error"]["message"])

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "завтра"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {}, csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)
        self.assertIn("Укажите новое время", data["error"]["message"])

    def test_shift_is_scoped_to_owner(self):
        event = self.create_event()
        self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        status, _, data = client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "2027-12-01T20:00"},
            csrf=client.csrf,
        )
        self.assertEqual(status, 404, data)

    def test_event_validation(self):
        self.login()
        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "Bad", "venue": "Place", "timezone": "Europe/Moscow",
             "startsAt": "2027-11-04T20:00", "endsAt": "2027-11-04T19:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400)
        self.assertIn("позже", data["error"]["message"])

        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "Bad", "venue": "Place", "timezone": "Mars/Olympus",
             "startsAt": "2027-11-04T20:00", "endsAt": "2027-11-04T21:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400)

    def test_slug_uniqueness(self):
        self.create_event()
        self.login()
        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "DNB NIGHT", "venue": "Другой бар",
             "timezone": "Europe/Moscow",
             "startsAt": "2027-12-04T20:00", "endsAt": "2027-12-05T02:00"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        self.assertNotEqual(data["event"]["slug"], "dnb-night")

    def test_draft_is_hidden_from_public(self):
        self.create_event(published=False)
        status, _, data = self.client.get("/api/public/featured")
        self.assertIsNone(data["event"])
        status, _, _ = self.client.get("/api/public/events/dnb-night")
        self.assertEqual(status, 404)

        status, _, data = self.client.get("/api/admin/events")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["events"]), 1)
        self.assertFalse(data["events"][0]["published"])

    def test_delete_cascades(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.delete(
            f"/api/admin/events/{event['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        conn = db.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM halls").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM performances").fetchone()[0], 0)
        finally:
            conn.close()

    def test_guest_search_is_case_insensitive_for_cyrillic(self):
        event = self.create_event(title="GUEST SEARCH")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        list_id = data["list"]["id"]
        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{list_id}/guests",
            {"name": "Федот"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)

        for query in ("федот", "Федот", "ФЕДОТ"):
            status, _, data = self.client.get(
                f"/api/admin/guest-lists/{list_id}/guests?q={query}")
            self.assertEqual(status, 200, data)
            self.assertEqual([item["name"] for item in data["guests"]],
                             ["Федот"], query)

    def test_door_link_scans_and_searches_guests(self):
        self.create_admin("promo", "promo-password-1")
        event = self.create_event(title="DOOR NIGHT")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости", "promoterUsername": "promo"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        list_id = data["list"]["id"]
        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{list_id}/guests",
            {"name": "Федот", "phone": "+7 999 123-45-67",
             "tableName": "7"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        guest = data["guest"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/door-link", {},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        path = data["link"]["path"]
        door_token = path.rsplit("/", 1)[-1]

        status, headers, _ = self.client.get(f"/door/{door_token}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn("camera=(self)", headers["permissions-policy"])

        status, headers, _ = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("camera=()", headers["permissions-policy"])

        status, _, data = self.client.get(f"/api/door/{door_token}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["title"], "DOOR NIGHT")
        self.assertEqual(data["stats"]["guestCount"], 1)
        self.assertEqual(data["stats"]["checkedInCount"], 0)

        for query in ("федот", "Федот", "ФЕДОТ", "4567", "999"):
            status, _, data = self.client.get(
                f"/api/door/{door_token}/search?q={query}")
            self.assertEqual(status, 200, data)
            self.assertEqual([item["name"] for item in data["guests"]],
                             ["Федот"], query)
            item = data["guests"][0]
            self.assertEqual(item["phoneMasked"], "•••• 4567", query)
            self.assertEqual(item["listName"], "Гости", query)
            self.assertNotIn("phone", item)
            self.assertNotIn("123-45-67",
                             json.dumps(data, ensure_ascii=False), query)

        status, _, data = self.client.post(
            f"/api/door/{door_token}/scan",
            {"code": f"https://nightevents.ru/g/{guest['shareToken']}"},
            csrf=None)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["already"])
        self.assertEqual(data["guest"]["status"], "checked_in")
        self.assertEqual(data["guest"]["phoneMasked"], "•••• 4567")
        self.assertEqual(data["guest"]["listName"], "Гости")
        self.assertNotIn("phone", data["guest"])
        self.assertEqual(data["stats"]["checkedInCount"], 1)

        status, _, data = self.client.post(
            f"/api/door/{door_token}/scan",
            {"code": guest["shareToken"]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["already"])

        status, _, data = self.client.post(
            f"/api/door/{door_token}/undo",
            {"guestId": guest["id"]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["guest"]["status"], "invited")

        # Промоутер не может управлять ссылкой для охраны.
        promoter = self.login_as("promo", "promo-password-1")
        status, _, data = promoter.get(
            f"/api/admin/events/{event['id']}/door-link")
        self.assertEqual(status, 403, data)

        status, _, data = self.client.delete(
            f"/api/admin/events/{event['id']}/door-link",
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, _ = self.client.get(f"/api/door/{door_token}")
        self.assertEqual(status, 404)
        status, _, _ = self.client.get("/api/door/not-a-real-token-12345")
        self.assertEqual(status, 404)

    def test_blacklist_guest_editing_and_door_warning(self):
        event = self.create_event(title="BLACKLIST NIGHT")
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 200, data)
        blacklist = next(item for item in data["lists"] if item["isBlacklist"])
        self.assertEqual(blacklist["name"], "Чёрный список")
        self.assertEqual(len(data["lists"]), 1)

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "VIP посетители"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        vip = data["list"]

        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{blacklist['id']}/guests",
            {"name": "Плохой Гость", "phone": "+7 999 000-11-22"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        bad = data["guest"]
        self.assertTrue(bad["listIsBlacklist"])
        status, _, _ = self.client.get(f"/api/public/guests/{bad['shareToken']}")
        self.assertEqual(status, 404)

        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{vip['id']}/guests",
            {"name": "Хороший Гость", "phone": "+7 999 000-11-22",
             "tableName": "1"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        good = data["guest"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/door-link", {},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        door_token = data["link"]["path"].rsplit("/", 1)[-1]

        status, _, data = self.client.get(
            f"/api/door/{door_token}/search?q=Плохой")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["guests"]), 1)
        self.assertTrue(data["guests"][0]["blacklisted"])
        self.assertEqual(data["guests"][0]["listName"], "Чёрный список")
        self.assertEqual(data["stats"]["guestCount"], 1)

        # Обычный гость с тем же телефоном тоже подсвечивается в поиске.
        status, _, data = self.client.get(
            f"/api/door/{door_token}/search?q=Хороший")
        self.assertEqual(status, 200, data)
        good_card = next(
            item for item in data["guests"] if item["name"] == "Хороший Гость")
        self.assertFalse(good_card["blacklisted"])
        self.assertEqual(good_card["blacklistMatch"]["name"], "Плохой Гость")

        # Обычный гость с тем же телефоном блокируется при сканировании.
        status, _, data = self.client.post(
            f"/api/door/{door_token}/scan",
            {"code": good["shareToken"]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["blacklisted"])
        self.assertEqual(data["blacklistMatch"]["name"], "Плохой Гость")
        self.assertEqual(data["guest"]["status"], "invited")

        # QR из чёрного списка тоже не отмечает вход.
        status, _, data = self.client.post(
            f"/api/door/{door_token}/scan",
            {"code": bad["shareToken"]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["blacklisted"])
        self.assertEqual(data["guest"]["status"], "invited")

        status, _, data = self.client.post(
            f"/api/door/{door_token}/checkin",
            {"guestId": bad["id"]}, csrf=None)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["code"], "blacklisted")

        # В чёрном списке нельзя отметить приход вручную из админки.
        status, _, data = self.client.patch(
            f"/api/admin/guests/{bad['id']}", {"status": "checked_in"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

        # Перемещение в чёрный список и обратно.
        status, _, data = self.client.patch(
            f"/api/admin/guests/{good['id']}", {"listId": blacklist["id"]},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["guest"]["listIsBlacklist"])
        self.assertEqual(data["guest"]["status"], "invited")
        status, _, _ = self.client.get(f"/api/public/guests/{good['shareToken']}")
        self.assertEqual(status, 404)

        status, _, data = self.client.patch(
            f"/api/admin/guests/{good['id']}",
            {"listId": vip["id"], "name": "Хороший Гость 2",
             "phone": "+7 999 000-33-44"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["guest"]["name"], "Хороший Гость 2")
        self.assertEqual(data["guest"]["phone"], "+7 999 000-33-44")
        self.assertFalse(data["guest"]["listIsBlacklist"])
        status, _, public = self.client.get(
            f"/api/public/guests/{good['shareToken']}")
        self.assertEqual(status, 200, public)
        self.assertEqual(public["guest"]["name"], "Хороший Гость 2")

        # Чёрный список нельзя удалить или переименовать.
        status, _, data = self.client.patch(
            f"/api/admin/guest-lists/{blacklist['id']}",
            {"name": "Новый список"}, csrf=self.client.csrf)
        self.assertEqual(status, 400, data)
        status, _, data = self.client.delete(
            f"/api/admin/guest-lists/{blacklist['id']}",
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

    def test_promoter_suggestions_follow_creator(self):
        helper = self.create_admin("helper", "helper-password-1")
        helper_client = self.login_as("helper", "helper-password-1")
        event = self.create_event_as(helper_client, title="SUGGEST NIGHT")

        status, _, data = helper_client.post(
            "/api/admin/users",
            {"username": "own_promo", "password": "own-promo-pass-1",
             "isPromoter": True},
            csrf=helper_client.csrf,
        )
        self.assertEqual(status, 201, data)
        own = data["user"]
        other = self.create_promoter("other_promo", "other-promo-pass-1")

        status, _, data = helper_client.get(
            f"/api/admin/events/{event['id']}/promoters")
        self.assertEqual(status, 200, data)
        names = [item["username"] for item in data["promoters"]]
        self.assertIn(own["username"], names)
        self.assertNotIn(other["username"], names)

        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/promoters")
        self.assertEqual(status, 200, data)
        names = [item["username"] for item in data["promoters"]]
        self.assertIn(own["username"], names)
        self.assertIn(other["username"], names)

        status, _, data = helper_client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости"}, csrf=helper_client.csrf)
        self.assertEqual(status, 201, data)
        status, _, data = helper_client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": "own_promo", "listId": data["list"]["id"]},
            csrf=helper_client.csrf)
        self.assertEqual(status, 200, data)

        promo_client = self.login_as("own_promo", "own-promo-pass-1")
        status, _, data = promo_client.get(
            f"/api/admin/events/{event['id']}/promoters")
        self.assertEqual(status, 403, data)

    def test_self_registration_links(self):
        event = self.create_event(title="REGISTRATION NIGHT")
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        blacklist = next(item for item in data["lists"] if item["isBlacklist"])
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Посетители"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        guest_list = data["list"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": guest_list["id"], "comment": "Для Telegram-канала",
             "maxRegistrations": 2, "expiryMode": "start+1"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        link = data["link"]
        self.assertEqual(link["comment"], "Для Telegram-канала")
        self.assertEqual(link["maxRegistrations"], 2)
        self.assertEqual(link["registrationCount"], 0)
        self.assertTrue(link["canRegister"])
        self.assertTrue(link["path"].startswith("/r/"))

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": blacklist["id"], "expiryMode": "never"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": guest_list["id"], "expiryMode": "custom",
             "expiresAtLocal": "2020-01-01T12:00"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

        status, _, public = self.client.get(link["path"].replace("/r/", "/api/public/registration/"))
        self.assertEqual(status, 200, public)
        self.assertTrue(public["canRegister"])
        self.assertNotIn("comment", public)
        self.assertEqual(public["event"]["title"], "REGISTRATION NIGHT")

        for index, name in enumerate(("Первый Гость", "Второй Гость"), start=1):
            status, _, data = self.client.post(
                f"/api/public/registration/{link['token']}",
                {"name": name, "phone": f"+7 999 100-00-0{index}"},
                csrf=None)
            self.assertEqual(status, 201, data)
            self.assertEqual(data["guest"]["name"], name)
            self.assertTrue(data["guest"]["shareToken"])

        status, _, public = self.client.get(
            f"/api/public/registration/{link['token']}")
        self.assertEqual(status, 200, public)
        self.assertFalse(public["canRegister"])
        self.assertEqual(public["reason"], "limit")
        self.assertEqual(public["registrationCount"], 2)

        status, _, data = self.client.post(
            f"/api/public/registration/{link['token']}",
            {"name": "Третий Гость"}, csrf=None)
        self.assertEqual(status, 410, data)
        self.assertEqual(data["error"]["code"], "registration_closed")

        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/registration-links")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["links"]), 1)
        saved = data["links"][0]
        self.assertEqual(saved["registrationCount"], 2)
        self.assertFalse(saved["canRegister"])
        self.assertEqual(saved["reason"], "limit")
        self.assertEqual(saved["listName"], "Посетители")

        # Регистрация попала в список как обычный гость с личным QR.
        status, _, data = self.client.get(
            f"/api/admin/guest-lists/{guest_list['id']}/guests")
        self.assertEqual(status, 200, data)
        names = [item["name"] for item in data["guests"]]
        self.assertIn("Первый Гость", names)
        self.assertIn("Второй Гость", names)
        first = next(item for item in data["guests"]
                     if item["name"] == "Первый Гость")
        status, _, public_guest = self.client.get(
            f"/api/public/guests/{first['shareToken']}")
        self.assertEqual(status, 200, public_guest)
        status, headers, _ = self.client.get(
            f"/g/{first['shareToken']}/qr.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers["content-type"])

        # Ссылка без лимита и с ручным закрытием/открытием.
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": guest_list["id"], "comment": "Бессрочная",
             "expiryMode": "never"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        forever = data["link"]
        self.assertIsNone(forever["maxRegistrations"])
        self.assertIsNone(forever["expiresAt"])
        self.assertTrue(forever["canRegister"])

        status, _, data = self.client.patch(
            f"/api/admin/registration-links/{forever['id']}",
            {"isActive": False}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["link"]["canRegister"])
        self.assertEqual(data["link"]["reason"], "closed")
        status, _, data = self.client.patch(
            f"/api/admin/registration-links/{forever['id']}",
            {"isActive": True}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["link"]["canRegister"])

        # Истёкшая ссылка закрыта, даже если флаг is_active включён.
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": guest_list["id"], "expiryMode": "start+2"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        expired = data["link"]
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE guest_registration_links SET expiry_mode = 'custom', "
                "expiry_offset_minutes = NULL, "
                "expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
                (expired["id"],))
            conn.commit()
        finally:
            conn.close()
        status, _, public = self.client.get(
            f"/api/public/registration/{expired['token']}")
        self.assertEqual(status, 200, public)
        self.assertFalse(public["canRegister"])
        self.assertEqual(public["reason"], "expired")
        status, _, data = self.client.post(
            f"/api/public/registration/{expired['token']}",
            {"name": "Поздний Гость"}, csrf=None)
        self.assertEqual(status, 410, data)

        # Удаление ссылки: гости остаются, публичная ссылка исчезает.
        status, _, data = self.client.delete(
            f"/api/admin/registration-links/{forever['id']}",
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, _ = self.client.get(
            f"/api/public/registration/{forever['token']}")
        self.assertEqual(status, 404)
        status, _, data = self.client.delete(
            f"/api/admin/registration-links/{link['id']}",
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["registrations"], 2)
        status, _, _ = self.client.get(
            f"/api/public/registration/{link['token']}")
        self.assertEqual(status, 404)
        status, _, data = self.client.get(
            f"/api/admin/guest-lists/{guest_list['id']}/guests")
        names = [item["name"] for item in data["guests"]]
        self.assertIn("Первый Гость", names)
        self.assertIn("Второй Гость", names)
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/registration-links")
        self.assertNotIn(link["id"], [item["id"] for item in data["links"]])
        self.assertNotIn(forever["id"], [item["id"] for item in data["links"]])

        # Промоутер не управляет ссылками.
        promoter = self.create_promoter("reg_promo", "reg-promo-pass-1")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": promoter["username"], "listId": guest_list["id"]},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        promo_client = self.login_as("reg_promo", "reg-promo-pass-1")
        status, _, data = promo_client.get(
            f"/api/admin/events/{event['id']}/registration-links")
        self.assertEqual(status, 403, data)
        status, _, data = promo_client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": guest_list["id"], "expiryMode": "never"},
            csrf=promo_client.csrf)
        self.assertEqual(status, 403, data)

    def test_registration_link_expiry_follows_event_shift(self):
        event = self.create_event(
            title="SHIFT REGISTRATION",
            startsAt="2027-03-10T20:00",
            endsAt="2027-03-11T06:00",
        )
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        list_id = data["list"]["id"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": list_id, "comment": "shift+1",
             "expiryMode": "start+1"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        relative = data["link"]
        self.assertEqual(relative["expiryMode"], "start")
        self.assertEqual(relative["expiryOffsetMinutes"], 60)
        old_expiry = relative["expiresAt"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": list_id, "comment": "custom",
             "expiryMode": "custom",
             "expiresAtLocal": "2027-03-10T22:00"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        custom = data["link"]
        self.assertEqual(custom["expiryMode"], "custom")
        custom_expiry = custom["expiresAt"]

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/registration-links",
            {"listId": list_id, "comment": "never",
             "expiryMode": "never"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        never = data["link"]
        self.assertEqual(never["expiryMode"], "never")
        self.assertIsNone(never["expiresAt"])

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/shift",
            {"startsAt": "2027-03-10T23:00"}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        status, _, data = self.client.get(
            f"/api/public/registration/{relative['token']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["startsAt"], "2027-03-10T20:00:00Z")
        self.assertEqual(data["expiresAt"], "2027-03-10T21:00:00Z")
        self.assertNotEqual(data["expiresAt"], old_expiry)

        status, _, data = self.client.get(
            f"/api/public/registration/{custom['token']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["expiresAt"], custom_expiry)
        self.assertEqual(data["expiresAt"], "2027-03-10T19:00:00Z")

        status, _, data = self.client.get(
            f"/api/public/registration/{never['token']}")
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["expiresAt"])

        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/registration-links")
        self.assertEqual(status, 200, data)
        saved = {item["comment"]: item for item in data["links"]}
        self.assertEqual(saved["shift+1"]["expiresAt"],
                         "2027-03-10T21:00:00Z")
        self.assertEqual(saved["custom"]["expiresAt"],
                         "2027-03-10T19:00:00Z")

    def test_multiple_promoters_on_lists(self):
        event = self.create_event(title="PROMOTER NIGHT")
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        blacklist = next(item for item in data["lists"] if item["isBlacklist"])

        lists = []
        for name in ("Гости", "VIP"):
            status, _, data = self.client.post(
                f"/api/admin/events/{event['id']}/guest-lists",
                {"name": name}, csrf=self.client.csrf)
            self.assertEqual(status, 201, data)
            lists.append(data["list"])

        promo1 = self.create_promoter("promo_one", "promo-one-pass-1")
        promo2 = self.create_promoter("promo_two", "promo-two-pass-1")

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": "promo_one", "listId": lists[0]["id"]},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": "promo_two"}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        by_name = {item["name"]: item for item in data["lists"]}
        self.assertEqual(
            [item["username"] for item in by_name["Гости"]["promoters"]],
            ["promo_one", "promo_two"])
        self.assertEqual(
            [item["username"] for item in by_name["VIP"]["promoters"]],
            ["promo_two"])
        self.assertEqual(by_name["Чёрный список"]["promoters"], [])

        client_one = self.login_as("promo_one", "promo-one-pass-1")
        status, _, data = client_one.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["name"] for item in data["lists"]], ["Гости"])
        status, _, data = client_one.get("/api/admin/events")
        self.assertEqual(status, 200, data)
        self.assertIn(event["id"], [item["id"] for item in data["events"]])

        client_two = self.login_as("promo_two", "promo-two-pass-1")
        status, _, data = client_two.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["name"] for item in data["lists"]], ["Гости", "VIP"])

        # Нельзя назначить промоутера в чёрный список.
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": "promo_one", "listId": blacklist["id"]},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

        # Убираем одного промоутера из списка и со всех списков.
        status, _, data = self.client.delete(
            f"/api/admin/guest-lists/{lists[0]['id']}/promoters/{promo2['id']}",
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = client_two.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual([item["name"] for item in data["lists"]], ["VIP"])

        status, _, data = self.client.delete(
            f"/api/admin/events/{event['id']}/promoters/{promo2['id']}",
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = client_two.get("/api/admin/events")
        self.assertEqual(status, 200, data)
        self.assertNotIn(event["id"], [item["id"] for item in data["events"]])
        self.create_admin("promo", "promo-password-1")
        event = self.create_event(title="GUEST NIGHT")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости", "promoterUsername": "promo"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        guest_list = data["list"]
        self.assertEqual(guest_list["name"], "Гости")
        self.assertEqual([item["username"] for item in guest_list["promoters"]],
                         ["promo"])

        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{guest_list['id']}/guests",
            {
                "name": "Иван Петров",
                "phone": "+7 999 123-45-67",
                "tableName": "Стол 7",
                "notes": "День рождения",
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        guest = data["guest"]
        self.assertEqual(guest["name"], "Иван Петров")
        self.assertNotIn("plusOnes", guest)
        self.assertEqual(guest["status"], "invited")
        self.assertTrue(guest["shareToken"])
        self.assertEqual(data["list"]["guestCount"], 1)
        self.assertEqual(data["list"]["checkedInCount"], 0)

        token = guest["shareToken"]
        status, _, public = self.client.get(f"/api/public/guests/{token}")
        self.assertEqual(status, 200, public)
        self.assertEqual(public["guest"]["name"], "Иван Петров")
        self.assertNotIn("plusOnes", public["guest"])
        self.assertEqual(public["guest"]["tableName"], "Стол 7")
        self.assertEqual(public["event"]["title"], "GUEST NIGHT")
        self.assertNotIn("phone", public["guest"])
        self.assertNotIn("notes", public["guest"])
        self.assertNotIn("shareToken", public["guest"])

        status, headers, _ = self.client.get(f"/g/{token}/qr.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers["content-type"])
        self.assertTrue(self.client.last_body.startswith(b"\x89PNG\r\n\x1a\n"))
        status, headers, _ = self.client.get(f"/g/{token}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])

        status, _, data = self.client.patch(
            f"/api/admin/guests/{guest['id']}",
            {"status": "checked_in"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["guest"]["status"], "checked_in")
        self.assertIsNotNone(data["guest"]["checkedInAt"])
        self.assertEqual(data["list"]["checkedInCount"], 1)
        status, _, public = self.client.get(f"/api/public/guests/{token}")
        self.assertEqual(public["guest"]["status"], "checked_in")

        status, _, data = self.client.patch(
            f"/api/admin/guests/{guest['id']}",
            {"status": "invited"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["guest"]["checkedInAt"])

        promoter = self.login_as("promo", "promo-password-1")
        status, _, data = promoter.get("/api/admin/events")
        self.assertEqual(status, 200, data)
        promoted = next(item for item in data["events"] if item["id"] == event["id"])
        self.assertEqual(promoted["guestRole"], "promoter")

        status, _, data = promoter.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["id"] for item in data["lists"]], [guest_list["id"]])

        status, _, data = promoter.post(
            f"/api/admin/guest-lists/{guest_list['id']}/guests",
            {"name": "Гость промоутера"},
            csrf=promoter.csrf,
        )
        self.assertEqual(status, 201, data)
        self.assertEqual(data["guest"]["name"], "Гость промоутера")

        # Промоутер не может создавать и удалять списки.
        status, _, data = promoter.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Чужой список"},
            csrf=promoter.csrf,
        )
        self.assertEqual(status, 403, data)
        status, _, data = promoter.delete(
            f"/api/admin/guest-lists/{guest_list['id']}", csrf=promoter.csrf)
        self.assertEqual(status, 403, data)

        # Удаление мероприятия удаляет списки и гостей: ссылка перестаёт работать.
        status, _, _ = self.client.delete(
            f"/api/admin/events/{event['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200)
        status, _, _ = self.client.get(f"/api/public/guests/{token}")
        self.assertEqual(status, 404)

    def test_door_link_works_for_draft_event(self):
        event = self.create_event(title="DRAFT DOOR", published=False)
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/door-link", {},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        token = data["link"]["path"].rsplit("/", 1)[-1]
        status, _, data = self.client.get(f"/api/door/{token}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["title"], "DRAFT DOOR")
        status, headers, _ = self.client.get(f"/door/{token}")
        self.assertEqual(status, 200)
        self.assertIn("camera=(self)", headers["permissions-policy"])

    def test_door_offline_snapshot_and_sync(self):
        event = self.create_event(title="OFFLINE DOOR")
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        blacklist = next(item for item in data["lists"] if item["isBlacklist"])
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        list_id = data["list"]["id"]
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/door-link", {},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        door_token = data["link"]["path"].rsplit("/", 1)[-1]

        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{list_id}/guests",
            {"name": "Sync Guest", "phone": "+7 999 111-22-33"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        guest = data["guest"]
        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{blacklist['id']}/guests",
            {"name": "Blocked Sync", "phone": "+7 999 111-22-34"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        blocked = data["guest"]

        status, _, snap = self.client.get(f"/api/door/{door_token}/snapshot")
        self.assertEqual(status, 200, snap)
        self.assertEqual(snap["event"]["title"], "OFFLINE DOOR")
        self.assertEqual(snap["stats"]["guestCount"], 1)
        by_name = {item["name"]: item for item in snap["guests"]}
        self.assertEqual(by_name["Sync Guest"]["status"], "invited")
        self.assertTrue(by_name["Sync Guest"]["phoneHash"])
        self.assertTrue(by_name["Sync Guest"]["shareToken"])
        self.assertTrue(by_name["Blocked Sync"]["blacklisted"])
        self.assertTrue(by_name["Blocked Sync"]["phoneHash"])

        status, _, data = self.client.post(
            f"/api/door/{door_token}/sync",
            {"operations": [
                {"id": "offline-op-1", "guestId": guest["id"],
                 "action": "checkin", "at": "2027-11-04T22:15:00.482Z"},
            ]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"][0]["status"], "applied")
        self.assertEqual(data["updated"][0]["status"], "checked_in")
        self.assertEqual(data["updated"][0]["checkedInAt"], "2027-11-04T22:15:00Z")
        self.assertEqual(data["stats"]["checkedInCount"], 1)

        status, _, data = self.client.post(
            f"/api/door/{door_token}/sync",
            {"operations": [
                {"id": "offline-op-1", "guestId": guest["id"],
                 "action": "checkin", "at": "2027-11-04T22:15:00Z"},
            ]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"][0]["status"], "duplicate")
        self.assertEqual(data["stats"]["checkedInCount"], 1)

        status, _, data = self.client.post(
            f"/api/door/{door_token}/sync",
            {"operations": [
                {"id": "offline-op-2", "guestId": blocked["id"],
                 "action": "checkin"},
            ]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"][0]["status"], "conflict")
        self.assertIn("чёрном списке", data["results"][0]["message"])

        status, _, data = self.client.post(
            f"/api/door/{door_token}/sync",
            {"operations": [
                {"id": "offline-op-3", "guestId": guest["id"],
                 "action": "undo"},
            ]}, csrf=None)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"][0]["status"], "applied")
        self.assertEqual(data["updated"][0]["status"], "invited")
        self.assertEqual(data["stats"]["checkedInCount"], 0)

    def test_guest_csv_export_sorting_and_import_creates_lists(self):
        self.assertEqual(appmod.MAX_GUESTS_IMPORT_BYTES, 1024 * 1024)
        event = self.create_event(title="CSV NIGHT")
        status, _, data = self.client.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["importLimitBytes"], 1024 * 1024)
        blacklist = next(item for item in data["lists"] if item["isBlacklist"])
        lists = {}
        for name in ("VIP", "Гости"):
            status, _, data = self.client.post(
                f"/api/admin/events/{event['id']}/guest-lists",
                {"name": name}, csrf=self.client.csrf)
            self.assertEqual(status, 201, data)
            lists[name] = data["list"]["id"]

        def add_guest(list_id, name, phone):
            status, _, data = self.client.post(
                f"/api/admin/guest-lists/{list_id}/guests",
                {"name": name, "phone": phone}, csrf=self.client.csrf)
            self.assertEqual(status, 201, data)
            return data["guest"]

        anna_phone = "+7 900 000-00-02"
        add_guest(lists["VIP"], "Яна", "+7 900 000-00-01")
        add_guest(lists["VIP"], "Анна", anna_phone)
        add_guest(lists["Гости"], "Борис", "+7 900 000-00-03")
        add_guest(lists["Гости"], "Алина", "+7 900 000-00-04")
        add_guest(blacklist["id"], "Виктор", "+7 900 000-00-05")

        status, headers, _ = self.client.get(
            f"/api/admin/events/{event['id']}/guests.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertIn("attachment", headers["content-disposition"])
        exported = self.client.last_body.decode("utf-8")
        self.assertTrue(exported.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(exported.lstrip("\ufeff")),
                               delimiter=";"))
        self.assertEqual(rows[0], ["Имя", "Телефон", "Список"])
        self.assertEqual(
            [row[0] for row in rows[1:]],
            ["Анна", "Яна", "Алина", "Борис", "Виктор"])
        self.assertEqual(
            [row[2] for row in rows[1:]],
            ["VIP", "VIP", "Гости", "Гости", "Чёрный список"])

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Имя", "Телефон", "Список"])
        names = ["VIP", "Гости", "Фото", "Пресса"]
        for index in range(1, 31):
            writer.writerow([
                f"Импорт {index:02d}",
                f"+7 901 000-{index:04d}",
                names[(index - 1) % len(names)],
            ])
        writer.writerow(["Анна", anna_phone, "VIP"])
        writer.writerow(["", "+7 900 000-00-99", "VIP"])
        csv_text = buffer.getvalue()

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guests/import",
            {"csv": csv_text}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["created"], 30)
        self.assertEqual(data["duplicates"], 1)
        self.assertEqual(data["errorCount"], 1)
        self.assertEqual(data["listsCreated"], ["Фото", "Пресса"])
        self.assertEqual(data["totalRows"], 32)

        conn = db.connect()
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM guests WHERE event_id = ?",
                (event["id"],)).fetchone()[0], 35)
            for list_name, expected in (
                    ("VIP", 10), ("Гости", 10), ("Фото", 7), ("Пресса", 7)):
                count = conn.execute(
                    "SELECT COUNT(*) FROM guests g JOIN guest_lists gl "
                    "ON gl.id = g.list_id WHERE gl.event_id = ? "
                    "AND gl.name = ?", (event["id"], list_name)).fetchone()[0]
                self.assertEqual(count, expected, list_name)
            imported = conn.execute(
                "SELECT status, share_token FROM guests "
                "WHERE event_id = ? AND name = 'Импорт 01'",
                (event["id"],)).fetchone()
            self.assertEqual(imported["status"], "invited")
            self.assertTrue(imported["share_token"])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM guest_lists WHERE event_id = ? "
                "AND is_blacklist = 0", (event["id"],)).fetchone()[0], 4)
        finally:
            conn.close()

        # Повторный импорт не создаёт дублей.
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guests/import",
            {"csv": csv_text}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["created"], 0)
        self.assertEqual(data["duplicates"], 31)
        self.assertEqual(data["errorCount"], 1)
        self.assertEqual(data["listsCreated"], [])

        # Запятая, английские заголовки и строка sep=, от Excel.
        comma_csv = (
            "sep=,\r\n"
            "Name,Phone,List\r\n"
            '"Гость, с запятой",+7 902 000-00-01,Пресса\r\n'
        )
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guests/import",
            {"csv": comma_csv}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["created"], 1)
        self.assertEqual(data["listsCreated"], [])

        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guests/import",
            {"csv": "Имя;Телефон\nБез списка;+7 000"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)
        self.assertIn("Список", data["error"]["message"])

        original_limit = appmod.MAX_GUESTS_IMPORT_BYTES
        try:
            appmod.MAX_GUESTS_IMPORT_BYTES = 60
            status, _, data = self.client.post(
                f"/api/admin/events/{event['id']}/guests/import",
                {"csv": "Имя;Телефон;Список\n" + "А" * 100 + ";+7 000;VIP"},
                csrf=self.client.csrf)
            self.assertEqual(status, 413, data)
            self.assertIn("CSV больше", data["error"]["message"])
        finally:
            appmod.MAX_GUESTS_IMPORT_BYTES = original_limit

        # Промоутер видит только свои списки и не может выгрузить весь CSV.
        self.create_admin("csv_promo", "promo-password-2")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/promoters",
            {"username": "csv_promo", "listId": lists["VIP"]},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        promo = Client(self)
        status, _, data = promo.post(
            "/api/admin/login",
            {"username": "csv_promo", "password": "promo-password-2"})
        self.assertEqual(status, 200, data)
        promo.csrf = data["csrfToken"]
        status, _, _ = promo.get(
            f"/api/admin/events/{event['id']}/guests.csv")
        self.assertEqual(status, 403)
        status, _, _ = promo.post(
            f"/api/admin/events/{event['id']}/guests/import",
            {"csv": csv_text}, csrf=promo.csrf)
        self.assertEqual(status, 403)

    def test_guest_list_validation_and_public_visibility(self):
        event = self.create_event(title="GUEST VALIDATION")
        status, _, data = self.client.post(
            f"/api/admin/events/{event['id']}/guest-lists",
            {"name": "Гости"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 201, data)
        list_id = data["list"]["id"]

        bad_guests = [
            {"name": ""},
            {"name": "x", "phone": 42},
        ]
        for payload in bad_guests:
            status, _, data = self.client.post(
                f"/api/admin/guest-lists/{list_id}/guests", payload,
                csrf=self.client.csrf)
            self.assertEqual(status, 400, (payload, data))

        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{list_id}/guests",
            {"name": "Гость"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        guest = data["guest"]

        # У черновика событие скрыто из афиши, но прямые ссылки работают.
        draft = self.create_event(title="DRAFT GUEST", published=False)
        status, _, data = self.client.post(
            f"/api/admin/events/{draft['id']}/guest-lists",
            {"name": "Гости"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        draft_list = data["list"]
        status, _, data = self.client.post(
            f"/api/admin/guest-lists/{draft_list['id']}/guests",
            {"name": "Draft"}, csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        draft_guest = data["guest"]
        status, _, public = self.client.get(
            f"/api/public/guests/{draft_guest['shareToken']}")
        self.assertEqual(status, 200, public)
        self.assertEqual(public["guest"]["name"], "Draft")
        status, _, _ = self.client.get(
            f"/g/{draft_guest['shareToken']}/qr.png")
        self.assertEqual(status, 200)

        status, _, data = self.client.post(
            f"/api/admin/events/{draft['id']}/registration-links",
            {"listId": draft_list["id"], "expiryMode": "never"},
            csrf=self.client.csrf)
        self.assertEqual(status, 201, data)
        draft_link = data["link"]
        status, _, public = self.client.get(
            f"/api/public/registration/{draft_link['token']}")
        self.assertEqual(status, 200, public)
        self.assertTrue(public["canRegister"])
        status, _, data = self.client.post(
            f"/api/public/registration/{draft_link['token']}",
            {"name": "Draft Registered"}, csrf=None)
        self.assertEqual(status, 201, data)
        status, _, public = self.client.get(
            f"/api/public/guests/{data['guest']['shareToken']}")
        self.assertEqual(status, 200, public)

        # Чужой администратор не видит список.
        self.create_admin("other", "other-password-1")
        other = self.login_as("other", "other-password-1")
        status, _, data = other.get(
            f"/api/admin/events/{event['id']}/guest-lists")
        self.assertEqual(status, 404, data)
        status, _, data = other.get(
            f"/api/admin/guest-lists/{list_id}/guests")
        self.assertEqual(status, 404, data)

        # Неверный токен и неизвестный список.
        status, _, _ = self.client.get("/api/public/guests/not-a-real-token-12345")
        self.assertEqual(status, 404)
        status, _, data = self.client.delete(
            f"/api/admin/guests/{guest['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, _ = self.client.get(
            f"/api/public/guests/{guest['shareToken']}")
        self.assertEqual(status, 404)

        status, _, data = self.client.delete(
            f"/api/admin/guest-lists/{list_id}", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, _ = self.client.get(
            f"/api/admin/guest-lists/{list_id}/guests")
        self.assertEqual(status, 404)

    def test_event_qr_is_generated_on_demand(self):
        event = self.create_event(title="QR NIGHT")
        status, headers, _ = self.client.get(
            f"/api/admin/events/{event['id']}/qr.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers["content-type"])
        self.assertIn(f"lineup-{event['slug']}-qr.png",
                      headers["content-disposition"])
        self.assertIn("no-store", headers["cache-control"])
        self.assertTrue(self.client.last_body.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(self.client.last_body), 500)

        # Черновик: публичной страницы нет, QR не отдаём.
        draft = self.create_event(title="QR DRAFT", published=False)
        status, _, _ = self.client.get(
            f"/api/admin/events/{draft['id']}/qr.png")
        self.assertEqual(status, 404)

        # Прошедшее мероприятие: QR больше не доступен.
        now = timeutil.now_utc()
        tz = "Europe/Moscow"
        past = self.create_event(
            title="QR PAST",
            startsAt=timeutil.to_local_input(now - timedelta(hours=3), tz),
            endsAt=timeutil.to_local_input(now - timedelta(hours=2), tz),
        )
        status, _, _ = self.client.get(
            f"/api/admin/events/{past['id']}/qr.png")
        self.assertEqual(status, 404)

        # После удаления мероприятия QR тоже исчезает.
        status, _, _ = self.client.delete(
            f"/api/admin/events/{event['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200)
        status, _, _ = self.client.get(
            f"/api/admin/events/{event['id']}/qr.png")
        self.assertEqual(status, 404)

    def test_admin_event_payload_has_local_times(self):
        event = self.create_event()
        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["event"]["startsAtLocal"], "2027-11-04T20:00")
        self.assertIn("timezones", data)

    # -- ссылки мероприятия ---------------------------------------------

    def test_event_links_roundtrip(self):
        event = self.create_event(links=[
            {"title": "Билеты", "url": "https://tickets.example/dnb"},
            {"title": "", "url": "https://t.me/dnb"},
        ])
        self.assertEqual(len(event["links"]), 2)
        self.assertEqual(event["links"][0]["title"], "Билеты")

        status, _, data = self.client.get("/api/public/featured")
        self.assertEqual(status, 200)
        self.assertEqual(
            [link["url"] for link in data["event"]["links"]],
            ["https://tickets.example/dnb", "https://t.me/dnb"],
        )

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}",
            {"links": [{"title": "Сайт", "url": "http://example.com"}]},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertEqual([link["title"] for link in data["event"]["links"]], ["Сайт"])

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"links": []},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["links"], [])

    def test_event_links_validation(self):
        self.login()
        bad_variants = [
            [{"title": "x", "url": "javascript:alert(1)"}],
            [{"title": "x", "url": "ftp://example.com"}],
            [{"title": "x" * 81, "url": "https://example.com"}],
            [{"title": str(i), "url": f"https://example.com/{i}"} for i in range(13)],
        ]
        for links in bad_variants:
            status, _, data = self.client.post(
                "/api/admin/events",
                {"title": "Links", "venue": "Place",
                 "timezone": "Europe/Moscow",
                 "startsAt": "2027-11-04T20:00",
                 "endsAt": "2027-11-05T02:00", "links": links},
                csrf=self.client.csrf,
            )
            self.assertEqual(status, 400, (links, data))

    def test_delete_removes_links(self):
        event = self.create_event(links=[
            {"title": "Билеты", "url": "https://example.com"}])
        status, _, _ = self.client.delete(
            f"/api/admin/events/{event['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200)
        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0], 0)
        finally:
            conn.close()

    # -- срок жизни и артисты -------------------------------------------

    def test_expired_events_hide_after_seven_days(self):
        now = timeutil.now_utc()
        tz = "Europe/Moscow"

        def local(dt):
            return timeutil.to_local_input(dt, tz)

        expired_end = now - timedelta(days=8)
        expired = self.create_event(
            title="OLD NIGHT",
            startsAt=local(expired_end - timedelta(hours=2)),
            endsAt=local(expired_end),
        )
        recent_end = now - timedelta(days=1)
        recent = self.create_event(
            title="RECENT NIGHT",
            startsAt=local(recent_end - timedelta(hours=2)),
            endsAt=local(recent_end),
        )

        status, _, data = self.client.get("/api/public/events")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in data["events"]], [recent["id"]])

        status, _, _ = self.client.get("/api/public/events/old-night")
        self.assertEqual(status, 404)

        status, _, data = self.client.get("/api/public/featured")
        self.assertEqual(data["event"]["id"], recent["id"])

        status, _, data = self.client.get("/api/admin/events")
        flags = {item["id"]: item["publiclyVisible"] for item in data["events"]}
        self.assertFalse(flags[expired["id"]])
        self.assertTrue(flags[recent["id"]])

        # Перенос времени в будущее публикует мероприятие снова.
        future_start = now + timedelta(days=2)
        status, _, data = self.client.patch(
            f"/api/admin/events/{expired['id']}",
            {
                "startsAt": local(future_start),
                "endsAt": local(future_start + timedelta(hours=3)),
            },
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertTrue(data["event"]["publiclyVisible"])

        status, _, data = self.client.get("/api/public/events")
        self.assertIn(expired["id"], [item["id"] for item in data["events"]])

    def test_public_artists_and_search(self):
        event = self.create_event(title="ART NIGHT")
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(hall["id"], "DJ Smash", "2027-11-04T21:00", "2027-11-04T22:00")

        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200)
        self.assertEqual([a["name"] for a in data["artists"]], ["DJ Smash", "DJ Vik"])
        vik = next(a for a in data["artists"] if a["name"] == "DJ Vik")
        self.assertEqual(vik["eventCount"], 1)
        self.assertEqual(vik["events"][0]["slug"], "art-night")
        self.assertEqual(vik["events"][0]["sets"][0]["hall"], "Зал 1")

        status, _, data = self.client.get("/api/public/artists?q=vik")
        self.assertEqual([a["name"] for a in data["artists"]], ["DJ Vik"])

        # Артист без актуальных мероприятий не показывается даже в поиске.
        now = timeutil.now_utc()
        tz = "Europe/Moscow"
        expired_end = now - timedelta(days=9)
        old = self.create_event(
            title="OLD ART",
            startsAt=timeutil.to_local_input(
                expired_end - timedelta(hours=3), tz),
            endsAt=timeutil.to_local_input(expired_end, tz),
        )
        old_hall = self.add_hall(old["id"])
        self.add_perf(
            old_hall["id"], "Vintage DJ",
            timeutil.to_local_input(expired_end - timedelta(hours=2), tz),
            timeutil.to_local_input(expired_end - timedelta(hours=1), tz),
        )

        status, _, data = self.client.get("/api/public/artists")
        self.assertNotIn("Vintage DJ", [a["name"] for a in data["artists"]])
        status, _, data = self.client.get("/api/public/artists?q=vintage")
        self.assertEqual(data["artists"], [])

        # Артистов из скрытого лайнапа не раскрываем даже в поиске.
        hidden = self.create_event(title="SECRET NIGHT", lineupVisible=False)
        hidden_hall = self.add_hall(hidden["id"])
        self.add_perf(
            hidden_hall["id"], "Secret DJ",
            "2027-11-04T20:00", "2027-11-04T21:00",
        )
        status, _, data = self.client.get("/api/public/artists")
        self.assertNotIn("Secret DJ", [a["name"] for a in data["artists"]])
        status, _, data = self.client.get("/api/public/artists?q=secret")
        self.assertEqual(data["artists"], [])

    def test_artist_collaborations_split_into_separate_artists(self):
        event = self.create_event(title="B2B NIGHT")
        hall = self.add_hall(event["id"])
        self.add_perf(
            hall["id"], "ZVUK (B2B) RAZNOS",
            "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(
            hall["id"], "LUMEN (vs) MIRA",
            "2027-11-04T21:00", "2027-11-04T22:00")
        self.add_perf(
            hall["id"], "KIRA (f2f) NORD",
            "2027-11-04T22:00", "2027-11-04T23:00")
        self.add_perf(
            hall["id"], "DJ (Live)",
            "2027-11-04T23:00", "2027-11-05T00:00")

        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200, data)
        artists = {item["name"]: item for item in data["artists"]}
        for name in ("ZVUK", "RAZNOS", "LUMEN", "MIRA", "KIRA", "NORD"):
            self.assertIn(name, artists)
        self.assertIn("DJ (Live)", artists)
        for collaboration in ("ZVUK (B2B) RAZNOS", "LUMEN (vs) MIRA",
                              "KIRA (f2f) NORD"):
            self.assertNotIn(collaboration, artists)

        # У каждого участника совместного сета — то же мероприятие и время.
        self.assertEqual(artists["ZVUK"]["eventCount"], 1)
        self.assertEqual(artists["ZVUK"]["events"][0]["title"], "B2B NIGHT")
        self.assertEqual(artists["ZVUK"]["events"][0]["sets"][0]["hall"], "Зал 1")
        self.assertEqual(artists["RAZNOS"]["events"][0]["sets"],
                         artists["ZVUK"]["events"][0]["sets"])

        # В расписании маркеры показываются без скобок и в верхнем регистре,
        # а сохранённое имя остаётся как ввёл организатор.
        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        performances = data["event"]["halls"][0]["performances"]
        self.assertEqual(
            [item["displayPerformer"] for item in performances],
            ["ZVUK B2B RAZNOS", "LUMEN VS MIRA", "KIRA F2F NORD", "DJ (Live)"],
        )
        self.assertEqual(performances[0]["performer"], "ZVUK (B2B) RAZNOS")
        self.assertEqual(performances[1]["performer"], "LUMEN (vs) MIRA")

        status, _, data = self.client.get("/api/public/artists?q=raznos")
        self.assertEqual([item["name"] for item in data["artists"]],
                         ["RAZNOS"])
        status, _, data = self.client.get("/api/public/artists?q=lumen")
        self.assertEqual([item["name"] for item in data["artists"]],
                         ["LUMEN"])

    def test_artist_collaboration_ampersand_and_and(self):
        event = self.create_event(title="AND NIGHT")
        hall = self.add_hall(event["id"])
        self.add_perf(
            hall["id"], "SOL (&) VERA",
            "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(
            hall["id"], "RIFT (and) NOVA",
            "2027-11-04T21:00", "2027-11-04T22:00")

        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["name"] for item in data["artists"]],
                         ["NOVA", "RIFT", "SOL", "VERA"])

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(
            [item["displayPerformer"]
             for item in data["event"]["halls"][0]["performances"]],
            ["SOL & VERA", "RIFT AND NOVA"],
        )

    def test_artist_split_ignores_unknown_parentheses(self):
        event = self.create_event(title="MIXED NIGHT")
        hall = self.add_hall(event["id"])
        self.add_perf(
            hall["id"], "ASTER (Backstage) SOL",
            "2027-11-04T20:00", "2027-11-04T21:00")
        self.add_perf(
            hall["id"], "MIRA (B2B) NORD (F2F) VERA",
            "2027-11-04T21:00", "2027-11-04T22:00")

        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200, data)
        names = [item["name"] for item in data["artists"]]
        self.assertIn("ASTER (Backstage) SOL", names)
        self.assertNotIn("ASTER", names)
        for name in ("MIRA", "NORD", "VERA"):
            self.assertIn(name, names)

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(
            [item["displayPerformer"]
             for item in data["event"]["halls"][0]["performances"]],
            ["ASTER (Backstage) SOL", "MIRA B2B NORD F2F VERA"],
        )

    def test_artists_hide_times_for_filtered_sets(self):
        now = timeutil.now_utc().replace(second=0, microsecond=0)
        tz = "Europe/Moscow"

        def local(dt):
            return timeutil.to_local_input(dt, tz)

        event = self.create_event(
            title="FILTERED ARTISTS",
            startsAt=local(now - timedelta(minutes=90)),
            endsAt=local(now + timedelta(hours=4)),
            lineupFilter="played",
        )
        hall = self.add_hall(event["id"])
        self.add_perf(
            hall["id"], "Past DJ",
            local(now - timedelta(minutes=80)),
            local(now - timedelta(minutes=20)),
        )
        self.add_perf(
            hall["id"], "Past DJ",
            local(now + timedelta(minutes=130)),
            local(now + timedelta(minutes=190)),
        )
        self.add_perf(
            hall["id"], "Live DJ",
            local(now - timedelta(minutes=10)),
            local(now + timedelta(minutes=50)),
        )
        self.add_perf(
            hall["id"], "Future DJ",
            local(now + timedelta(minutes=60)),
            local(now + timedelta(minutes=120)),
        )

        # Режим «отыграли и играют»: будущий сет показываем без времени,
        # а отыгравший сет в раздел артистов уже не попадает.
        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200, data)
        artists = {item["name"]: item for item in data["artists"]}
        self.assertIn("Past DJ", artists)
        self.assertIn("Live DJ", artists)
        self.assertIn("Future DJ", artists)
        self.assertEqual(len(artists["Future DJ"]["events"]), 1)
        self.assertEqual(artists["Future DJ"]["events"][0]["sets"], [])
        self.assertEqual(artists["Past DJ"]["events"][0]["sets"], [])

        status, _, data = self.client.get("/api/public/artists?q=future")
        self.assertEqual([item["name"] for item in data["artists"]],
                         ["Future DJ"])
        self.assertEqual(data["artists"][0]["events"][0]["sets"], [])

        # Режим «сейчас играют»: прошедшие и будущие сеты без времени,
        # но артисты и мероприятия остаются на странице.
        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "live"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = self.client.get("/api/public/artists")
        artists = {item["name"]: item for item in data["artists"]}
        self.assertIn("Past DJ", artists)
        self.assertIn("Live DJ", artists)
        self.assertIn("Future DJ", artists)
        self.assertEqual(artists["Past DJ"]["events"][0]["sets"], [])
        self.assertEqual(len(artists["Live DJ"]["events"][0]["sets"]), 1)
        self.assertEqual(artists["Future DJ"]["events"][0]["sets"], [])

        # Без жёсткого фильтра виден актуальный (будущий) сет;
        # отыгравший сет в раздел артистов не возвращается.
        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "all"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = self.client.get("/api/public/artists")
        artists = {item["name"]: item for item in data["artists"]}
        self.assertIn("Past DJ", artists)
        self.assertIn("Live DJ", artists)
        self.assertIn("Future DJ", artists)
        self.assertEqual(len(artists["Past DJ"]["events"][0]["sets"]), 1)

    def test_artists_require_actual_set_in_unfinished_event(self):
        now = timeutil.now_utc().replace(second=0, microsecond=0)
        tz = "Europe/Moscow"

        def local(dt):
            return timeutil.to_local_input(dt, tz)

        event = self.create_event(
            title="ONGOING ARTISTS",
            startsAt=local(now - timedelta(hours=2)),
            endsAt=local(now + timedelta(hours=4)),
        )
        hall = self.add_hall(event["id"])
        self.add_perf(
            hall["id"], "Already Played",
            local(now - timedelta(minutes=110)),
            local(now - timedelta(minutes=30)),
        )
        self.add_perf(
            hall["id"], "Still Playing",
            local(now - timedelta(minutes=10)),
            local(now + timedelta(minutes=50)),
        )
        self.add_perf(
            hall["id"], "Upcoming DJ",
            local(now + timedelta(hours=1)),
            local(now + timedelta(hours=2)),
        )

        status, _, data = self.client.get("/api/public/artists")
        self.assertEqual(status, 200, data)
        self.assertEqual(
            [item["name"] for item in data["artists"]],
            ["Still Playing", "Upcoming DJ"],
        )

        status, _, data = self.client.get("/api/public/artists?q=played")
        self.assertEqual(data["artists"], [])

    def test_performer_database_survives_perf_delete(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        created = self.add_perf(
            hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/admin/performers")
        self.assertEqual(status, 200)
        self.assertIn("DJ Vik", data["performers"])

        status, _, _ = self.client.delete(
            f"/api/admin/performances/{created['performance']['id']}",
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200)

        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("DJ Vik", data["performers"])

    def test_performer_hints_are_owner_scoped(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "Super DJ", "2027-11-04T20:00", "2027-11-04T21:00")

        self.create_admin()
        helper = self.login_as("helper", "helper-password-1")
        own = self.create_event_as(helper, title="HELPER NIGHT")
        status, _, data = helper.post(
            f"/api/admin/events/{own['id']}/halls", {"name": "Зал 1"},
            csrf=helper.csrf,
        )
        self.assertEqual(status, 201, data)
        status, _, data = helper.post(
            f"/api/admin/halls/{data['hall']['id']}/performances",
            {"performer": "Helper DJ",
             "startsAt": "2027-11-04T20:00", "endsAt": "2027-11-04T21:00"},
            csrf=helper.csrf,
        )
        self.assertEqual(status, 201, data)

        status, _, data = helper.get("/api/admin/performers")
        self.assertIn("Helper DJ", data["performers"])
        self.assertNotIn("Super DJ", data["performers"])

        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("Super DJ", data["performers"])
        self.assertIn("Helper DJ", data["performers"])

    # -- журнал: очистка -------------------------------------------------

    def test_only_super_can_clear_audit(self):
        self.create_admin()
        helper = self.login_as("helper", "helper-password-1")
        status, _, data = helper.delete("/api/admin/audit", csrf=helper.csrf)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["code"], "forbidden")

    def test_super_clears_audit_without_new_entry(self):
        self.create_event()
        self.login()
        status, _, data = self.client.get("/api/admin/audit")
        self.assertEqual(status, 200)
        self.assertGreater(len(data["entries"]), 0)

        status, _, data = self.client.delete(
            "/api/admin/audit", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertGreater(data["deleted"], 0)

        status, _, data = self.client.get("/api/admin/audit")
        self.assertEqual(data["entries"], [])

    # -- роли, владение и журнал -----------------------------------------

    def test_regular_admin_sees_only_own_events(self):
        super_event = self.create_event()
        self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        own = self.create_event_as(client, title="HELPER NIGHT")

        status, _, data = client.get("/api/admin/events")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["id"] for item in data["events"]], [own["id"]])

        status, _, data = self.client.get("/api/admin/events")
        self.assertEqual(status, 200, data)
        self.assertEqual({item["id"] for item in data["events"]},
                         {super_event["id"], own["id"]})
        owners = {item["id"]: item["ownerUsername"] for item in data["events"]}
        self.assertEqual(owners[super_event["id"]], "admin")
        self.assertEqual(owners[own["id"]], "helper")

        status, _, data = self.client.get(f"/api/admin/events/{own['id']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["ownerUsername"], "helper")

    def test_foreign_event_is_invisible_to_regular_admin(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        perf = self.add_perf(hall["id"], "DJ Vik",
                             "2027-11-04T20:00", "2027-11-04T21:00")["performance"]
        self.create_admin()
        client = self.login_as("helper", "helper-password-1")

        attempts = [
            ("GET", f"/api/admin/events/{event['id']}", None),
            ("PATCH", f"/api/admin/events/{event['id']}", {"title": "Взлом"}),
            ("DELETE", f"/api/admin/events/{event['id']}", None),
            ("POST", f"/api/admin/events/{event['id']}/halls",
             {"name": "Чужой зал"}),
            ("PATCH", f"/api/admin/halls/{hall['id']}", {"name": "Чужой зал"}),
            ("DELETE", f"/api/admin/halls/{hall['id']}", None),
            ("POST", f"/api/admin/halls/{hall['id']}/performances",
             {"performer": "Чужой", "startsAt": "2027-11-04T21:00",
              "endsAt": "2027-11-04T22:00"}),
            ("PATCH", f"/api/admin/performances/{perf['id']}",
             {"performer": "Чужой"}),
            ("DELETE", f"/api/admin/performances/{perf['id']}", None),
        ]
        for method, path, body in attempts:
            status, _, data = client.request(method, path, body, csrf=client.csrf)
            self.assertEqual(status, 404, (method, path, data))
            self.assertEqual(data["error"]["code"], "not_found")

        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["title"], "DNB NIGHT")
        self.assertEqual(len(data["event"]["halls"]), 1)
        self.assertEqual(
            data["event"]["halls"][0]["performances"][0]["performer"], "DJ Vik")

    def test_performer_hints_are_scoped_to_owner(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik", "2027-11-04T20:00", "2027-11-04T21:00")

        self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        status, _, data = client.get("/api/admin/performers")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["performers"], [])

        own = self.create_event_as(client, title="OWN NIGHT")
        status, _, data = client.post(
            f"/api/admin/events/{own['id']}/halls", {"name": "Зал"},
            csrf=client.csrf)
        self.assertEqual(status, 201, data)
        hall_id = data["hall"]["id"]
        status, _, data = client.post(
            f"/api/admin/halls/{hall_id}/performances",
            {"performer": "DJ Solo", "startsAt": "2027-11-04T20:00",
             "endsAt": "2027-11-04T21:00"},
            csrf=client.csrf)
        self.assertEqual(status, 201, data)

        status, _, data = client.get("/api/admin/performers")
        self.assertEqual(data["performers"], ["DJ Solo"])
        status, _, data = self.client.get("/api/admin/performers")
        self.assertEqual(sorted(data["performers"]), ["DJ Solo", "DJ Vik"])

    def test_super_deletes_performer_hint(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik",
                      "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("DJ Vik", data["performers"])

        # Имя ищется без учёта регистра и удаляется из всех владельцев.
        status, _, data = self.client.delete(
            "/api/admin/performers?name=dj%20vik", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["deleted"], 1)

        status, _, data = self.client.get("/api/admin/performers")
        self.assertNotIn("DJ Vik", data["performers"])

        status, _, data = self.client.get("/api/admin/audit")
        actions = [entry["action"] for entry in data["entries"]]
        self.assertIn("performer.delete", actions)

        # Повторное удаление уже отсутствующего имени — 404.
        status, _, data = self.client.delete(
            "/api/admin/performers?name=DJ%20Vik", csrf=self.client.csrf)
        self.assertEqual(status, 404, data)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_only_super_can_delete_performer_hint(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik",
                      "2027-11-04T20:00", "2027-11-04T21:00")

        self.create_admin()
        helper = self.login_as("helper", "helper-password-1")
        status, _, data = helper.delete(
            "/api/admin/performers?name=DJ%20Vik", csrf=helper.csrf)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["code"], "forbidden")

        status, _, data = self.client.get("/api/admin/performers")
        self.assertIn("DJ Vik", data["performers"])

    def test_deleting_performer_hides_artist_from_public_search(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "DJ Vik",
                      "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/public/artists?q=vik")
        self.assertEqual([a["name"] for a in data["artists"]], ["DJ Vik"])

        status, _, data = self.client.delete(
            "/api/admin/performers?name=DJ%20Vik", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        status, _, data = self.client.get("/api/public/artists?q=vik")
        self.assertEqual(data["artists"], [])

        # Удалённый артист пропадает и из списка «кто играет».
        status, _, data = self.client.get("/api/public/artists")
        self.assertNotIn("DJ Vik", [a["name"] for a in data["artists"]])

    def test_audit_log_records_actions(self):
        event = self.create_event()
        hall = self.add_hall(event["id"])
        perf = self.add_perf(hall["id"], "DJ Vik",
                             "2027-11-04T20:00", "2027-11-04T21:00")["performance"]
        status, _, _ = self.client.patch(
            f"/api/admin/performances/{perf['id']}",
            {"performer": "DJ Vik 2"}, csrf=self.client.csrf)
        self.assertEqual(status, 200)
        status, _, _ = self.client.delete(
            f"/api/admin/events/{event['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200)

        status, _, data = self.client.get("/api/admin/audit")
        self.assertEqual(status, 200, data)
        entries = data["entries"]
        actions = [entry["action"] for entry in entries]
        self.assertEqual(actions[0], "event.delete")
        for action in ("event.create", "hall.create", "performance.create",
                       "performance.update", "event.delete"):
            self.assertIn(action, actions)
        self.assertTrue(all(entry["actor"] == "admin" for entry in entries))
        self.assertIn("DNB NIGHT", entries[0]["detail"])
        self.assertIn("DJ Vik 2", entries[0]["detail"])

    def test_audit_log_is_super_only(self):
        self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        status, _, data = client.get("/api/admin/audit")
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "forbidden")

    def test_deleting_admin_transfers_events_to_super(self):
        helper = self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        own = self.create_event_as(client, title="HELPER NIGHT")

        status, _, data = self.client.delete(
            f"/api/admin/users/{helper['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        status, _, data = self.client.get(f"/api/admin/events/{own['id']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["ownerUsername"], "admin")

        status, _, data = self.client.get("/api/admin/audit")
        user_entries = [entry for entry in data["entries"]
                        if entry["action"] == "user.delete"]
        self.assertEqual(len(user_entries), 1)
        self.assertIn("helper", user_entries[0]["detail"])
        self.assertIn("мероприятий передано: 1", user_entries[0]["detail"])

    # -- анонс и скрытый лайнап ------------------------------------------

    def test_hidden_lineup_is_announced_without_schedule(self):
        event = self.create_event(
            lineupVisible=False,
            description="Скоро объявим лайнап",
            links=[{"title": "Билеты", "url": "https://tickets.example/x"}])
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "Secret DJ",
                      "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/public/featured")
        self.assertEqual(status, 200, data)
        public = data["event"]
        self.assertEqual(public["id"], event["id"])
        self.assertFalse(public["lineupVisible"])
        self.assertEqual(public["halls"], [])
        self.assertEqual(public["description"], "Скоро объявим лайнап")
        self.assertEqual(len(public["links"]), 1)
        self.assertNotIn("lineupPublishAt", public)

        status, _, data = self.client.get("/api/public/events")
        summary = next(item for item in data["events"] if item["id"] == event["id"])
        self.assertEqual(summary["hallCount"], 0)
        self.assertEqual(summary["performanceCount"], 0)
        self.assertFalse(summary["lineupVisible"])

        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertFalse(data["event"]["lineupVisible"])
        self.assertEqual(len(data["event"]["halls"]), 1)
        self.assertEqual(data["event"]["stats"]["performances"], 1)

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupVisible": True},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = self.client.get("/api/public/featured")
        self.assertTrue(data["event"]["lineupVisible"])
        self.assertEqual(len(data["event"]["halls"]), 1)

    def test_lineup_opens_at_scheduled_time(self):
        event = self.create_event(lineupVisible=True,
                                  lineupPublishAt="2027-11-04T19:00")
        hall = self.add_hall(event["id"])
        self.add_perf(hall["id"], "Later DJ",
                      "2027-11-04T20:00", "2027-11-04T21:00")

        status, _, data = self.client.get("/api/public/featured")
        self.assertFalse(data["event"]["lineupVisible"])
        self.assertEqual(data["event"]["halls"], [])
        self.assertEqual(data["event"]["lineupPublishAt"], "2027-11-04T16:00:00Z")

        conn = db.connect()
        try:
            conn.execute("UPDATE events SET lineup_publish_at = ? WHERE id = ?",
                         ("2020-01-01T00:00:00Z", event["id"]))
            conn.commit()
        finally:
            conn.close()

        status, _, data = self.client.get("/api/public/featured")
        self.assertTrue(data["event"]["lineupVisible"])
        self.assertEqual(len(data["event"]["halls"]), 1)

    def test_invalid_lineup_schedule_is_rejected(self):
        self.login()
        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "Bad lineup", "venue": "Place",
             "timezone": "Europe/Moscow",
             "startsAt": "2027-11-04T20:00",
             "endsAt": "2027-11-05T02:00",
             "lineupPublishAt": "not-a-date"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

    # -- жёсткий фильтр расписания ---------------------------------------

    def test_lineup_filter_defaults_and_persistence(self):
        event = self.create_event()
        self.assertEqual(event["lineupFilter"], "all")

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["lineupFilter"], "all")

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "live"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["lineupFilter"], "live")

        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertEqual(data["event"]["lineupFilter"], "live")

        status, _, data = self.client.get("/api/admin/events")
        admin_item = next(item for item in data["events"]
                          if item["id"] == event["id"])
        self.assertEqual(admin_item["lineupFilter"], "live")

        status, _, data = self.client.get("/api/public/events")
        summary = next(item for item in data["events"]
                       if item["id"] == event["id"])
        self.assertEqual(summary["lineupFilter"], "live")

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "played"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["lineupFilter"], "played")

        status, _, data = self.client.get(f"/api/public/events/{event['slug']}")
        self.assertEqual(data["event"]["lineupFilter"], "played")

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "all"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["event"]["lineupFilter"], "all")

    def test_invalid_lineup_filter_is_rejected(self):
        event = self.create_event()

        status, _, data = self.client.patch(
            f"/api/admin/events/{event['id']}", {"lineupFilter": "future"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

        status, _, data = self.client.get(f"/api/admin/events/{event['id']}")
        self.assertEqual(data["event"]["lineupFilter"], "all")

        status, _, data = self.client.post(
            "/api/admin/events",
            {"title": "Bad filter", "venue": "Place",
             "timezone": "Europe/Moscow",
             "startsAt": "2027-11-04T20:00",
             "endsAt": "2027-11-05T02:00",
             "lineupFilter": "hidden"},
            csrf=self.client.csrf)
        self.assertEqual(status, 400, data)

    def test_old_database_gets_lineup_filter_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(
                    "CREATE TABLE events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "slug TEXT NOT NULL UNIQUE, "
                    "title TEXT NOT NULL, "
                    "city TEXT NOT NULL DEFAULT '', "
                    "venue TEXT NOT NULL, "
                    "address TEXT NOT NULL DEFAULT '', "
                    "description TEXT NOT NULL DEFAULT '', "
                    "timezone TEXT NOT NULL, "
                    "starts_at TEXT NOT NULL, "
                    "ends_at TEXT NOT NULL, "
                    "published INTEGER NOT NULL DEFAULT 1, "
                    "created_at TEXT NOT NULL"
                    ");"
                    "INSERT INTO events "
                    "(slug, title, venue, timezone, starts_at, ends_at, created_at) "
                    "VALUES ('old', 'OLD', 'Club', 'Europe/Moscow', "
                    "'2027-11-04T20:00:00Z', '2027-11-05T02:00:00Z', "
                    "'2027-01-01T00:00:00Z');"
                )
                db.init(conn)
                columns = {row["name"]
                           for row in conn.execute("PRAGMA table_info(events)")}
                self.assertIn("lineup_filter", columns)
                value = conn.execute(
                    "SELECT lineup_filter FROM events").fetchone()[0]
                self.assertEqual(value, "all")
            finally:
                conn.close()

    def test_old_database_gets_replacement_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(
                    "CREATE TABLE performances ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "hall_id INTEGER NOT NULL, "
                    "performer TEXT NOT NULL, "
                    "starts_at TEXT NOT NULL, "
                    "ends_at TEXT NOT NULL"
                    ");"
                    "INSERT INTO performances "
                    "(hall_id, performer, starts_at, ends_at) "
                    "VALUES (1, 'DJ Old', '2027-11-04T20:00:00Z', "
                    "'2027-11-04T21:00:00Z');"
                )
                db.init(conn)
                columns = {row["name"]
                           for row in conn.execute("PRAGMA table_info(performances)")}
                self.assertIn("is_replacement", columns)
                value = conn.execute(
                    "SELECT is_replacement FROM performances").fetchone()[0]
                self.assertEqual(value, 0)
            finally:
                conn.close()

    # -- администраторы --------------------------------------------------

    def test_super_can_manage_admins(self):
        helper = self.create_admin()
        self.assertFalse(helper["isSuper"])
        self.assertFalse(helper["isPromoter"])

        status, _, data = self.client.get("/api/admin/users")
        self.assertEqual(status, 200)
        self.assertEqual([user["username"] for user in data["users"]],
                         ["admin", "helper"])
        self.assertTrue(data["users"][0]["isSuper"])
        self.assertFalse(data["users"][1]["isSuper"])

        status, _, data = self.client.patch(
            f"/api/admin/users/{helper['id']}",
            {"password": "another-secret-2"}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)

        helper_client = self.login_as("helper", "another-secret-2")
        status, _, _ = helper_client.get("/api/admin/events")
        self.assertEqual(status, 200)

        status, _, data = self.client.delete(
            f"/api/admin/users/{helper['id']}", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        status, _, _ = helper_client.get("/api/admin/events")
        self.assertEqual(status, 401)

    def test_regular_admin_manages_only_own_promoters(self):
        helper = self.create_admin("helper", "helper-password-1")
        other = self.create_admin("other", "other-password-1")
        helper_client = self.login_as("helper", "helper-password-1")

        status, _, data = helper_client.get("/api/admin/users")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["users"], [])

        # Обычный администратор создаёт только промоутера, даже если просит админа.
        status, _, data = helper_client.post(
            "/api/admin/users",
            {"username": "promo1", "password": "promo-password-1",
             "isPromoter": False, "expiresAt": "2030-12-31"},
            csrf=helper_client.csrf,
        )
        self.assertEqual(status, 201, data)
        promoter = data["user"]
        self.assertTrue(promoter["isPromoter"])
        self.assertFalse(promoter["isSuper"])
        self.assertEqual(promoter["createdByUsername"], "helper")
        self.assertEqual(promoter["expiresAt"], "2030-12-31")

        # Главный видит метку промоутера и автора.
        status, _, data = self.client.get("/api/admin/users")
        self.assertEqual(status, 200, data)
        seen = next(item for item in data["users"]
                    if item["id"] == promoter["id"])
        self.assertTrue(seen["isPromoter"])
        self.assertEqual(seen["createdByUsername"], "helper")

        # Другой админ не видит и не может удалить чужого промоутера.
        other_client = self.login_as("other", "other-password-1")
        status, _, data = other_client.get("/api/admin/users")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["users"], [])
        status, _, data = other_client.delete(
            f"/api/admin/users/{promoter['id']}", csrf=other_client.csrf)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["code"], "forbidden")

        # Создатель может сменить пароль и удалить своего промоутера.
        status, _, data = helper_client.patch(
            f"/api/admin/users/{promoter['id']}",
            {"password": "promo-password-2"}, csrf=helper_client.csrf)
        self.assertEqual(status, 200, data)
        status, _, data = helper_client.delete(
            f"/api/admin/users/{promoter['id']}", csrf=helper_client.csrf)
        self.assertEqual(status, 200, data)

        # Чужого администратора обычный админ не трогает.
        status, _, data = helper_client.patch(
            f"/api/admin/users/{other['id']}",
            {"password": "other-password-2"}, csrf=helper_client.csrf)
        self.assertEqual(status, 403, data)
        status, _, data = helper_client.delete(
            f"/api/admin/users/{other['id']}", csrf=helper_client.csrf)
        self.assertEqual(status, 403, data)

    def test_promoter_expiry_and_limited_access(self):
        self.login()
        promoter = self.create_promoter(
            "expiring", "expiring-password-1", expires_at="2030-01-01")
        self.assertTrue(promoter["isPromoter"])
        client = self.login_as("expiring", "expiring-password-1")
        status, _, data = client.get("/api/admin/events")
        self.assertEqual(status, 200, data)

        # Промоутер не управляет пользователями и не создаёт мероприятия.
        status, _, data = client.get("/api/admin/users")
        self.assertEqual(status, 403, data)
        status, _, data = client.post(
            "/api/admin/users",
            {"username": "another", "password": "another-password-1"},
            csrf=client.csrf,
        )
        self.assertEqual(status, 403, data)
        status, _, data = client.post(
            "/api/admin/events", {}, csrf=client.csrf)
        self.assertEqual(status, 403, data)

        # Просроченная дата не принимается при создании.
        status, _, data = self.client.post(
            "/api/admin/users",
            {"username": "past", "password": "past-password-1",
             "isPromoter": True, "expiresAt": "2020-01-01"},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 400, data)

        # Срок действия проверяется и при активной сессии, и при входе.
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE users SET expires_at = '2020-01-01' WHERE id = ?",
                (promoter["id"],))
            conn.commit()
        finally:
            conn.close()
        status, _, _ = client.get("/api/admin/events")
        self.assertEqual(status, 401)
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (promoter["id"],)
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()
        status, _, data = self.client.post(
            "/api/admin/login",
            {"username": "expiring", "password": "expiring-password-1"})
        self.assertEqual(status, 401, data)
        self.assertEqual(data["error"]["code"], "invalid_credentials")
        status, _, data = self.client.get("/api/admin/users")
        self.assertNotIn("expiring",
                         [item["username"] for item in data["users"]])

    def test_super_account_is_protected(self):
        self.login()
        status, _, data = self.client.post(
            "/api/admin/users",
            {"username": "Admin", "password": "duplicate-password"},
            csrf=self.client.csrf)
        self.assertEqual(status, 409, data)

        status, _, data = self.client.patch(
            f"/api/admin/users/{self.super_id}", {"password": "another-secret-2"},
            csrf=self.client.csrf)
        self.assertEqual(status, 403, data)

        status, _, data = self.client.delete(
            f"/api/admin/users/{self.super_id}", csrf=self.client.csrf)
        self.assertEqual(status, 403, data)

    def test_session_reports_super(self):
        self.login()
        status, _, data = self.client.get("/api/admin/session")
        self.assertEqual(status, 200)
        self.assertTrue(data["user"]["isSuper"])

    # -- оформление сайта ------------------------------------------------

    def test_public_site_defaults(self):
        status, _, data = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        self.assertIsNone(data["logoUrl"])
        self.assertEqual(data["links"], [])
        self.assertIn("LineUP", data["copyright"])
        self.assertIn("расписание мероприятий", data["copyright"])
        self.assertEqual(data["theme"]["mode"], "preset")
        self.assertEqual(data["theme"]["id"], "default")
        self.assertEqual(data["theme"]["colors"]["accent"], "#2bff88")
        self.assertIn("siteVersion", data)
        self.assertIn("serverNow", data)

        status, _, _ = self.client.get("/logo")
        self.assertEqual(status, 404)

    def test_super_manages_site_copyright(self):
        self.login()
        status, _, data = self.client.get("/api/admin/site")
        self.assertEqual(status, 200, data)
        default_text = data["copyrightDefault"]
        self.assertTrue(default_text.startswith("© "))
        self.assertIn("LineUP", default_text)
        self.assertEqual(data["copyright"], default_text)
        self.assertEqual(data["themes"][0]["id"], "default")
        self.assertGreaterEqual(len(data["themes"]), 11)

        custom = "© 2027 NIGHT CLUB — все права защищены"
        status, _, data = self.client.put(
            "/api/admin/site/copyright", {"text": custom},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["copyright"], custom)
        self.assertFalse(data["isDefault"])

        status, _, public = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        self.assertEqual(public["copyright"], custom)

        # Пустой текст возвращает стандартный копирайт.
        status, _, data = self.client.put(
            "/api/admin/site/copyright", {"text": ""},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["isDefault"])
        self.assertEqual(data["copyright"], default_text)

        for payload in ({"text": None}, {"text": 42},
                        {"text": "x" * 201}, {"text": "a\nb"}):
            status, _, data = self.client.put(
                "/api/admin/site/copyright", payload,
                csrf=self.client.csrf)
            self.assertEqual(status, 400, (payload, data))

    def test_super_manages_site_theme(self):
        self.login()
        status, _, data = self.client.get("/api/admin/site")
        self.assertEqual(status, 200, data)
        theme_ids = [item["id"] for item in data["themes"]]
        self.assertIn("default", theme_ids)
        self.assertIn("neon", theme_ids)
        self.assertIn("mono", theme_ids)

        status, _, public = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        default_version = public["siteVersion"]

        status, _, data = self.client.put(
            "/api/admin/site/theme", {"mode": "preset", "id": "neon"},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["theme"]["mode"], "preset")
        self.assertEqual(data["theme"]["id"], "neon")
        self.assertEqual(data["theme"]["colors"]["accent"], "#b66cff")

        status, _, public = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        self.assertEqual(public["theme"]["id"], "neon")
        self.assertEqual(public["theme"]["colors"]["timing"], "#d08cff")
        self.assertNotEqual(public["siteVersion"], default_version)

        custom = {
            "background": "#101010",
            "panel": "#202020",
            "text": "#fafafa",
            "muted": "#999999",
            "accent": "#ff8800",
            "timing": "#00ccff",
        }
        status, _, data = self.client.put(
            "/api/admin/site/theme", {"mode": "custom", "colors": custom},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["theme"]["mode"], "custom")
        self.assertEqual(data["theme"]["colors"], custom)

        status, _, public = self.client.get("/api/public/site")
        self.assertEqual(public["theme"]["mode"], "custom")
        self.assertEqual(public["theme"]["colors"], custom)

        bad_variants = [
            {"mode": "preset", "id": "unknown"},
            {"mode": "custom", "colors": {}},
            {"mode": "custom", "colors": {**custom, "accent": "green"}},
            {"mode": "preset"},
            {},
        ]
        for payload in bad_variants:
            status, _, data = self.client.put(
                "/api/admin/site/theme", payload, csrf=self.client.csrf)
            self.assertEqual(status, 400, (payload, data))

    def test_super_manages_site_links(self):
        self.login()
        payload = {"links": [
            {"label": "О проекте", "description": "LineUP — афиша и расписание.",
             "url": ""},
            {"label": "Контакты", "description": "Напишите нам.",
             "url": "https://t.me/lineup"},
        ]}
        status, _, data = self.client.put(
            "/api/admin/site/links", payload, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual([link["label"] for link in data["links"]],
                         ["О проекте", "Контакты"])

        status, _, data = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        self.assertEqual([link["label"] for link in data["links"]],
                         ["О проекте", "Контакты"])
        self.assertEqual(data["links"][1]["url"], "https://t.me/lineup")

        status, _, data = self.client.put(
            "/api/admin/site/links",
            {"links": [{"label": "Контакты 2",
                        "description": "Новое описание.", "url": ""}]},
            csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual([link["label"] for link in data["links"]],
                         ["Контакты 2"])

        status, _, data = self.client.put(
            "/api/admin/site/links", {"links": []}, csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["links"], [])

    def test_site_links_validation(self):
        self.login()
        bad_variants = [
            [{"label": "", "description": "x", "url": ""}],
            [{"label": "x", "description": "", "url": ""}],
            [{"label": "x" * 61, "description": "x", "url": ""}],
            [{"label": "x", "description": "x" * 1201, "url": ""}],
            [{"label": "x", "description": "x", "url": "javascript:alert(1)"}],
            [{"label": "x", "description": "x", "url": "ftp://example.com"}],
            [{"label": f"Кнопка {i}", "description": "x", "url": ""}
             for i in range(9)],
        ]
        for links in bad_variants:
            status, _, data = self.client.put(
                "/api/admin/site/links", {"links": links},
                csrf=self.client.csrf)
            self.assertEqual(status, 400, (links, data))

    def test_regular_admin_cannot_manage_site(self):
        helper = self.create_admin()
        client = self.login_as("helper", "helper-password-1")
        requests = [
            ("GET", "/api/admin/site", None),
            ("PUT", "/api/admin/site/links",
             {"links": [{"label": "x", "description": "y", "url": ""}]}),
            ("PUT", "/api/admin/site/logo", {"data": "x"}),
            ("DELETE", "/api/admin/site/logo", None),
            ("PUT", "/api/admin/site/copyright", {"text": "x"}),
            ("PUT", "/api/admin/site/theme", {"mode": "preset", "id": "mono"}),
        ]
        for method, path, body in requests:
            status, _, data = client.request(
                method, path, body, csrf=client.csrf)
            self.assertEqual(status, 403, (method, path, data))
            self.assertEqual(data["error"]["code"], "forbidden")

    def test_logo_upload_and_remove(self):
        self.login()
        status, _, data = self.client.put(
            "/api/admin/site/logo",
            {"data": base64.b64encode(PNG_BYTES).decode("ascii")},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 200, data)
        self.assertIsNotNone(data["logoUrl"])
        logo_url = data["logoUrl"]

        status, headers, _ = self.client.get(logo_url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(self.client.last_body, PNG_BYTES)

        status, _, public = self.client.get("/api/public/site")
        self.assertEqual(status, 200)
        self.assertEqual(public["logoUrl"], logo_url)

        status, _, data = self.client.delete(
            "/api/admin/site/logo", csrf=self.client.csrf)
        self.assertEqual(status, 200, data)
        self.assertIsNone(data["logoUrl"])

        status, _, _ = self.client.get(logo_url)
        self.assertEqual(status, 404)
        status, _, public = self.client.get("/api/public/site")
        self.assertIsNone(public["logoUrl"])

    def test_logo_validation(self):
        self.login()
        bad_variants = [
            {"data": ""},
            {"data": base64.b64encode(b"not a png").decode("ascii")},
            {"data": "data:text/plain;base64,"
                     + base64.b64encode(PNG_BYTES).decode("ascii")},
            {"data": "%%%"},
        ]
        for payload in bad_variants:
            status, _, data = self.client.put(
                "/api/admin/site/logo", payload, csrf=self.client.csrf)
            self.assertEqual(status, 400, (payload, data))

        too_big = PNG_BYTES + b"0" * (
            appmod.MAX_LOGO_BYTES + 1 - len(PNG_BYTES))
        status, _, data = self.client.put(
            "/api/admin/site/logo",
            {"data": base64.b64encode(too_big).decode("ascii")},
            csrf=self.client.csrf,
        )
        self.assertEqual(status, 413, data)

    def test_site_tables_are_created(self):
        conn = db.connect()
        try:
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        self.assertIn("site_links", tables)
        self.assertIn("site_settings", tables)

    def test_xss_payload_stored_as_json(self):
        event = self.create_event(title="<script>alert(1)</script>")
        status, headers, data = self.client.get("/api/public/featured")
        self.assertEqual(status, 200)
        self.assertEqual(data["event"]["title"], "<script>alert(1)</script>")
        self.assertIn("application/json", headers["content-type"])

    def test_unknown_api_returns_json_404(self):
        status, headers, data = self.client.get("/api/public/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(data["error"]["code"], "not_found")

    def test_method_not_allowed(self):
        status, headers, _ = self.client.delete("/api/public/featured")
        self.assertEqual(status, 405)
        self.assertIn("allow", headers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
