#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ГУАП • Отзывы — сервер с базой данных SQLite.

Запуск:            python server.py
Сайт:              http://localhost:8000
Админка:           http://localhost:8000/admin.html  (пароль печатается при запуске)
Очистить базу:     python server.py --reset   (удалит ВСЕ отзывы)
Хостинг (WSGI):    wsgi.py — например, PythonAnywhere (см. инструкцию в wsgi.py)

Отзывы хранятся в файле reviews.db и не пропадают при перезапуске сервера.
На сайте только настоящие отзывы посетителей — никаких заготовленных примеров.

Публичное API:
  GET  /api/reviews                 — видимые отзывы с ответами
  POST /api/reviews                 — добавить отзыв
  POST /api/reviews/<id>/helpful    — «Полезно» (+1)
  POST /api/reviews/<id>/replies    — ответить на отзыв
  POST /api/reviews/<id>/report     — пожаловаться (после 3 жалоб отзыв скрывается)

API админки (заголовок X-Admin-Password):
  GET  /api/admin/reviews                     — все отзывы, включая скрытые
  POST /api/admin/reviews/<id>/hide|show|delete
  POST /api/admin/replies/<id>/delete

Защита от спама:
  • с одного устройства — не больше 1 отзыва на корпус за сутки;
  • с одного IP-адреса — не больше 15 отзывов в час (учитываем общий Wi-Fi);
  • отзывы и ответы с матом или ссылками не принимаются.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
PASSWORD_FILE = os.path.join(BASE_DIR, "admin_password.txt")
PORT = int(os.environ.get("PORT", 8000))

CAMPUSES = [
    "Главный корпус на Большой Морской",
    "Корпус на Гастелло",
    "Корпус на Ленсовета",
]

# Критерии оценки: ключ в базе → название на сайте
CRITERIA = {
    "r_rooms": "Аудитории",
    "r_food": "Столовая",
    "r_tech": "Техника и Wi-Fi",
    "r_clean": "Чистота и уют",
    "r_transport": "Транспорт",
}

ROLES = ["Студент", "Абитуриент", "Выпускник"]

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MAX_TEXT = 2000
MAX_SHORT = 120
MAX_REPLY = 600

# Время по Москве (UTC+3, без перехода на летнее) — на хостинге часы идут по UTC
MSK = timezone(timedelta(hours=3))


def _now():
    return datetime.now(MSK).replace(tzinfo=None)


REPORTS_TO_HIDE = 3          # после стольких жалоб отзыв скрывается до проверки
DEVICE_LIMIT_HOURS = 24      # 1 отзыв на корпус с устройства за это время
IP_LIMIT_PER_HOUR = 15       # отзывов с одного IP за час

# ===================== ФИЛЬТР КОНТЕНТА =====================

# Корни нецензурных слов (проверяем начало слова)
_BAD_ROOTS = [
    "хуй", "хуе", "хуё", "хуя", "хуи", "пизд", "ебат", "ебан", "ебал", "ебл", "еби", "ёб",
    "заеб", "выеб", "наеб", "уеб", "отъеб", "бляд", "блят", "сука", "суки", "сучк",
    "мудак", "мудил", "пидор", "пидар", "гандон", "залуп", "шлюх", "дебил",
]
_BAD_RE = re.compile(r"(?<![а-яё])(" + "|".join(_BAD_ROOTS) + r")", re.IGNORECASE)
_LINK_RE = re.compile(
    r"(https?://|www\.|\b[\w-]+\.(ru|com|net|org|рф|su|io|me|info|xyz|site|online|shop)\b|t\.me/|@\w{4,})",
    re.IGNORECASE,
)


def _normalize(text):
    # Ловим простые обходы: латинские буквы-двойники и «х.у.й»
    table = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м", "b": "в", "h": "н", "t": "т"})
    t = text.lower().translate(table)
    return re.sub(
        r"(?<![а-яё])(?:[а-яё][\s.*_\-]+){2,}[а-яё](?![а-яё])",
        lambda m: re.sub(r"[\s.*_\-]", "", m.group()),
        t,
    )


def check_content(*texts):
    for t in texts:
        if not t:
            continue
        if _LINK_RE.search(t):
            return "Ссылки и контакты в отзывах запрещены."
        if _BAD_RE.search(t) or _BAD_RE.search(_normalize(t)):
            return "Пожалуйста, без нецензурных выражений."
    return None


# ===================== БАЗА ДАННЫХ =====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    """Создаёт таблицы или обновляет старую базу, не теряя отзывы."""
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL DEFAULT '',
            faculty TEXT NOT NULL DEFAULT '',
            campus  TEXT NOT NULL,
            rating  INTEGER NOT NULL,
            text    TEXT NOT NULL,
            date    TEXT NOT NULL,
            helpful INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    new_columns = {
        "role": "TEXT NOT NULL DEFAULT ''",
        "course": "TEXT NOT NULL DEFAULT ''",
        "institute": "TEXT NOT NULL DEFAULT ''",
        "direction": "TEXT NOT NULL DEFAULT ''",
        "pros": "TEXT NOT NULL DEFAULT ''",
        "cons": "TEXT NOT NULL DEFAULT ''",
        "rating_avg": "REAL",
        "created": "TEXT NOT NULL DEFAULT ''",
        "is_demo": "INTEGER NOT NULL DEFAULT 0",
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        "reports": "INTEGER NOT NULL DEFAULT 0",
        "device": "TEXT NOT NULL DEFAULT ''",
        "ip": "TEXT NOT NULL DEFAULT ''",
    }
    for key in CRITERIA:
        new_columns[key] = "INTEGER"
    existing = _columns(conn, "reviews")
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {decl}")
    conn.execute("UPDATE reviews SET rating_avg = rating WHERE rating_avg IS NULL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replies (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            role      TEXT NOT NULL DEFAULT '',
            text      TEXT NOT NULL,
            date      TEXT NOT NULL
        )
        """
    )
    # Кто уже жаловался / отмечал «Полезно» — чтобы нельзя было накрутить
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            review_id INTEGER NOT NULL,
            voter     TEXT NOT NULL,
            kind      TEXT NOT NULL,
            PRIMARY KEY (review_id, voter, kind)
        )
        """
    )
    conn.commit()
    conn.close()


def reset_db():
    conn = get_db()
    for table in ("votes", "replies", "reviews"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()
    init_db()


def _hash(value):
    return hashlib.sha256(("guap-reviews:" + value).encode("utf-8")).hexdigest()[:24]


def _review_to_dict(row, replies, admin=False):
    d = dict(row)
    d["criteria"] = {k: d.pop(k) for k in CRITERIA}
    d["rating"] = d.pop("rating_avg") or d["rating"]
    d["hidden"] = bool(d["hidden"])
    d["replies"] = replies.get(d["id"], [])
    # Отзывы анонимные: имя и служебные данные наружу не отдаём
    for key in ("name", "device", "ip", "is_demo"):
        d.pop(key, None)
    if not admin:
        d.pop("reports", None)
        d.pop("hidden", None)
    return d


def _load_replies(conn):
    replies = {}
    for rep in conn.execute("SELECT * FROM replies ORDER BY id"):
        replies.setdefault(rep["review_id"], []).append(dict(rep))
    return replies


def fetch_public():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reviews WHERE hidden = 0 ORDER BY created DESC, id DESC").fetchall()
    replies = _load_replies(conn)
    conn.close()
    return {"reviews": [_review_to_dict(r, replies) for r in rows]}


def fetch_admin():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reviews ORDER BY hidden DESC, reports DESC, id DESC").fetchall()
    replies = _load_replies(conn)
    conn.close()
    return {"reviews": [_review_to_dict(r, replies, admin=True) for r in rows]}


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _today():
    t = _now()
    return f"{t.day} {MONTHS[t.month - 1]} {t.year}"


def add_review(data, device, ip):
    campus = _clean(data.get("campus"), MAX_SHORT)
    role = _clean(data.get("role"), MAX_SHORT)
    course = _clean(data.get("course"), MAX_SHORT)
    institute = _clean(data.get("institute"), MAX_SHORT)
    direction = _clean(data.get("direction"), MAX_SHORT)
    pros = _clean(data.get("pros"), 300)
    cons = _clean(data.get("cons"), 300)
    text = str(data.get("text") or "").strip()[:MAX_TEXT]

    if campus not in CAMPUSES:
        return None, "Выберите корпус из списка."
    if role not in ROLES:
        return None, "Укажите, кто вы: студент, абитуриент или выпускник."
    if role == "Студент" and not course:
        return None, "Укажите курс."
    if len(text) < 20:
        return None, "Напишите отзыв хотя бы из 20 символов."

    raw = data.get("criteria") or {}
    scores = {}
    for key, title in CRITERIA.items():
        try:
            v = int(raw.get(key))
        except (TypeError, ValueError):
            v = 0
        if v < 1 or v > 5:
            return None, f"Поставьте оценку по критерию «{title}»."
        scores[key] = v

    problem = check_content(text, pros, cons, direction)
    if problem:
        return None, problem

    now = _now()
    conn = get_db()
    try:
        since_day = (now - timedelta(hours=DEVICE_LIMIT_HOURS)).isoformat(timespec="seconds")
        if device and conn.execute(
            "SELECT 1 FROM reviews WHERE device = ? AND campus = ? AND created > ?",
            (device, campus, since_day),
        ).fetchone():
            return None, "Вы уже оставили отзыв об этом корпусе сегодня. Попробуйте завтра."
        since_hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        recent = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE ip = ? AND created > ?", (ip, since_hour)
        ).fetchone()[0]
        if recent >= IP_LIMIT_PER_HOUR:
            return None, "Слишком много отзывов из вашей сети. Попробуйте через час."

        avg = round(sum(scores.values()) / len(scores), 1)
        cur = conn.execute(
            "INSERT INTO reviews (name, faculty, campus, rating, rating_avg, text, date, helpful, "
            "role, course, institute, direction, pros, cons, created, device, ip, "
            + ", ".join(CRITERIA) + ") VALUES (" + ", ".join(["?"] * (17 + len(CRITERIA))) + ")",
            ("", "", campus, round(avg), avg, text, _today(), 0,
             role, course if role == "Студент" else "", institute, direction, pros, cons,
             now.isoformat(timespec="seconds"), device, ip, *scores.values()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _review_to_dict(row, {}), None
    finally:
        conn.close()


def _vote(review_id, voter, kind):
    """Возвращает (row, new) — new=False, если этот человек уже голосовал."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM reviews WHERE id = ? AND hidden = 0", (review_id,)).fetchone()
        if not row:
            return None, False
        try:
            conn.execute("INSERT INTO votes (review_id, voter, kind) VALUES (?, ?, ?)", (review_id, voter, kind))
        except sqlite3.IntegrityError:
            return row, False
        if kind == "helpful":
            conn.execute("UPDATE reviews SET helpful = helpful + 1 WHERE id = ?", (review_id,))
        else:
            conn.execute(
                "UPDATE reviews SET reports = reports + 1, "
                "hidden = CASE WHEN reports + 1 >= ? THEN 1 ELSE hidden END WHERE id = ?",
                (REPORTS_TO_HIDE, review_id),
            )
        conn.commit()
        return conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone(), True
    finally:
        conn.close()


def add_reply(review_id, data):
    text = str(data.get("text") or "").strip()[:MAX_REPLY]
    role = _clean(data.get("role"), MAX_SHORT)
    if role not in ROLES:
        role = ""
    if len(text) < 2:
        return None, "Напишите ответ."
    problem = check_content(text)
    if problem:
        return None, problem
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM reviews WHERE id = ? AND hidden = 0", (review_id,)).fetchone():
            return None, "Отзыв не найден."
        cur = conn.execute(
            "INSERT INTO replies (review_id, role, text, date) VALUES (?, ?, ?, ?)",
            (review_id, role, text, _today()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM replies WHERE id = ?", (cur.lastrowid,)).fetchone()), None
    finally:
        conn.close()


def admin_action(kind, item_id, action):
    conn = get_db()
    try:
        if kind == "reviews":
            if action == "hide":
                cur = conn.execute("UPDATE reviews SET hidden = 1 WHERE id = ?", (item_id,))
            elif action == "show":
                # Возвращаем отзыв и обнуляем жалобы
                cur = conn.execute("UPDATE reviews SET hidden = 0, reports = 0 WHERE id = ?", (item_id,))
                conn.execute("DELETE FROM votes WHERE review_id = ? AND kind = 'report'", (item_id,))
            elif action == "delete":
                conn.execute("DELETE FROM replies WHERE review_id = ?", (item_id,))
                conn.execute("DELETE FROM votes WHERE review_id = ?", (item_id,))
                cur = conn.execute("DELETE FROM reviews WHERE id = ?", (item_id,))
            else:
                return False
        elif kind == "replies" and action == "delete":
            cur = conn.execute("DELETE FROM replies WHERE id = ?", (item_id,))
        else:
            return False
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ===================== ПАРОЛЬ АДМИНКИ =====================

def get_admin_password():
    """Пароль берётся из переменной окружения ADMIN_PASSWORD или из файла
    admin_password.txt (создаётся автоматически при первом запуске)."""
    env = os.environ.get("ADMIN_PASSWORD")
    if env:
        return env
    if os.path.exists(PASSWORD_FILE):
        with open(PASSWORD_FILE, encoding="utf-8") as f:
            pw = f.read().strip()
            if pw:
                return pw
    pw = secrets.token_urlsafe(9)
    with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
        f.write(pw + "\n")
    return pw


ADMIN_PASSWORD = None


# ===================== HTTP-СЕРВЕР =====================

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

# Эти файлы не отдаём наружу
PRIVATE_FILES = {"reviews.db", "server.log", "server.py", "admin_password.txt", "wsgi.py"}


MAX_BODY = 50_000

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type, X-Device, X-Admin-Password"),
]


def setup():
    """Готовит базу и пароль админки. Вызывается и из main(), и из wsgi.py."""
    global ADMIN_PASSWORD
    init_db()
    ADMIN_PASSWORD = get_admin_password()
    return ADMIN_PASSWORD


def _json_response(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
    return status, headers + CORS_HEADERS, body


def _text_response(status, text):
    return status, [("Content-Type", "text/plain; charset=utf-8")], text.encode("utf-8")


def _parse_body(raw):
    if not raw or len(raw) > MAX_BODY:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _static_response(path):
    if path == "/":
        path = "/index.html"
    file_path = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
    if not file_path.startswith(BASE_DIR + os.sep):
        return _text_response(403, "Forbidden")
    rel = os.path.relpath(file_path, BASE_DIR)
    if rel in PRIVATE_FILES or rel.startswith((".", "__pycache__")):
        return _text_response(404, "Not found")
    if not os.path.isfile(file_path):
        return _text_response(404, "Not found")
    ext = os.path.splitext(file_path)[1].lower()
    with open(file_path, "rb") as f:
        body = f.read()
    return 200, [("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))] + CORS_HEADERS, body


def handle_request(method, raw_path, headers, body, client_ip):
    """Общая логика сайта для обоих способов запуска.

    method    — "GET" / "POST" / "OPTIONS"
    raw_path  — путь запроса (может содержать ?query)
    headers   — словарь заголовков; get() по имени в нижнем регистре
    body      — тело запроса (bytes)
    client_ip — IP-адрес, с которого пришёл запрос

    Возвращает (код, список заголовков, тело в bytes).
    """
    path = urllib.parse.unquote(urllib.parse.urlparse(raw_path).path)

    # За прокси хостинга настоящий адрес приходит в X-Forwarded-For
    fwd = headers.get("x-forwarded-for") or ""
    ip = _hash((fwd.split(",")[0].strip() if fwd else client_ip) or "unknown")
    device = re.sub(r"[^\w-]", "", headers.get("x-device") or "")[:64]
    device = _hash(device) if device else ""

    given = headers.get("x-admin-password") or ""
    is_admin = bool(given) and bool(ADMIN_PASSWORD) and hmac.compare_digest(
        given.encode(), ADMIN_PASSWORD.encode())

    if method == "OPTIONS":
        # Нужно, чтобы браузер разрешил запросы, когда страница открыта
        # не с нашего порта (Live Server, file:// и т.п.).
        return 204, list(CORS_HEADERS), b""

    if method in ("GET", "HEAD"):
        if path == "/api/reviews":
            return _json_response(fetch_public())
        if path == "/api/admin/reviews":
            if not is_admin:
                return _json_response({"error": "Неверный пароль"}, 401)
            return _json_response(fetch_admin())
        return _static_response(path)

    if method != "POST":
        return _text_response(405, "Method not allowed")

    if path == "/api/reviews":
        review, error = add_review(_parse_body(body), device, ip)
        if error:
            return _json_response({"error": error}, 400)
        return _json_response({"review": review}, 201)

    m = re.fullmatch(r"/api/reviews/(\d+)/(helpful|report)", path)
    if m:
        row, new = _vote(int(m.group(1)), device or ip, m.group(2))
        if row is None:
            return _json_response({"error": "Отзыв не найден"}, 404)
        return _json_response({"helpful": row["helpful"], "hidden": bool(row["hidden"]), "new": new})

    m = re.fullmatch(r"/api/reviews/(\d+)/replies", path)
    if m:
        reply, error = add_reply(int(m.group(1)), _parse_body(body))
        if error:
            return _json_response({"error": error}, 400)
        return _json_response({"reply": reply}, 201)

    m = re.fullmatch(r"/api/admin/(reviews|replies)/(\d+)/(hide|show|delete)", path)
    if m:
        if not is_admin:
            return _json_response({"error": "Неверный пароль"}, 401)
        ok = admin_action(m.group(1), int(m.group(2)), m.group(3))
        return _json_response({"ok": ok}, 200 if ok else 404)

    return _text_response(404, "Not found")


class Handler(BaseHTTPRequestHandler):
    """Локальный запуск: python server.py"""
    server_version = "GUAPReviews/3.0"

    def log_message(self, fmt, *args):
        try:
            with open(os.path.join(BASE_DIR, "server.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {self.command} {self.path}\n")
        except OSError:
            pass

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, resp_headers, resp_body = handle_request(
            self.command, self.path, headers, body, self.client_address[0])
        self.send_response(status)
        for name, value in resp_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)

    do_GET = do_POST = do_OPTIONS = do_HEAD = _handle


# ===================== ЗАПУСК =====================

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    if "--reset" in sys.argv:
        reset_db()
        print("База очищена: все отзывы удалены.")
        return
    setup()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 56)
    print("  ГУАП • Отзывы — сервер запущен")
    print(f"  Локально:   http://localhost:{PORT}")
    print(f"  В сети:     http://{get_lan_ip()}:{PORT}")
    print(f"  Админка:    http://localhost:{PORT}/admin.html")
    where = "переменная ADMIN_PASSWORD" if os.environ.get("ADMIN_PASSWORD") else "файл admin_password.txt"
    print(f"  Пароль:     {ADMIN_PASSWORD}   ({where})")
    print("  База данных: reviews.db — отзывы сохраняются между запусками")
    print("  Остановка:  Ctrl+C")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        server.server_close()


if __name__ == "__main__":
    main()
