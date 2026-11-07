#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ГУАП • Отзывы — сервер с базой данных SQLite.

Запуск:            python server.py
Сайт:              http://localhost:8000
Админка:           http://localhost:8000/admin.html  (пароль печатается при запуске)
Очистить базу:     python server.py --reset   (удалит ВСЕ отзывы)
Хостинг (WSGI):    wsgi.py — например, PythonAnywhere (см. инструкцию в wsgi.py)
Резервная копия:   делается сама раз в сутки (папка backups/); вручную — python backup.py

Отзывы хранятся в файле reviews.db и не пропадают при перезапуске сервера.
На сайте только настоящие отзывы посетителей — никаких заготовленных примеров.

Публичное API:
  GET  /api/reviews                 — видимые отзывы с ответами
  POST /api/reviews                 — добавить отзыв
  POST /api/reviews/<id>/helpful    — «Полезно» (+1)
  POST /api/reviews/<id>/replies    — ответить на отзыв
  POST /api/reviews/<id>/report     — пожаловаться на отзыв
  POST /api/reviews/<id>/delete     — удалить свой отзыв (в течение часа, с того же устройства)
  POST /api/replies/<id>/report     — пожаловаться на ответ

API админки (заголовок X-Admin-Password):
  GET  /api/admin/reviews                     — все отзывы и ответы, включая скрытые
  POST /api/admin/reviews/<id>/hide|show|delete
  POST /api/admin/replies/<id>/hide|show|delete

Защита от спама и накруток:
  • с одного устройства — не больше 1 отзыва на корпус за сутки;
  • с одного IP-адреса — не больше 15 отзывов в час (учитываем общий Wi-Fi);
  • ответы: не больше 10 в час с устройства и 30 в час с IP;
  • «Полезно»: с одного IP засчитывается не больше 5 отметок на отзыв;
  • отзыв или ответ скрывается, когда пожаловались из 3 разных сетей (IP);
  • отзывы и ответы с матом или ссылками не принимаются;
  • 5 неверных паролей админки подряд — вход блокируется на 15 минут.

Анонимность:
  • IP и ID устройства хранятся только как HMAC-хэш с секретным ключом
    (secret_key.txt или переменная SECRET_KEY) — по базе их не восстановить;
  • через 30 дней хэши IP и устройства у отзывов и ответов стираются.
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
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
PASSWORD_FILE = os.path.join(BASE_DIR, "admin_password.txt")
SECRET_FILE = os.path.join(BASE_DIR, "secret_key.txt")
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
MAX_BODY = 50_000

# Время по Москве (UTC+3, без перехода на летнее) — на хостинге часы идут по UTC
MSK = timezone(timedelta(hours=3))


def _now():
    return datetime.now(MSK).replace(tzinfo=None)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


REPORTS_TO_HIDE = 3          # жалобы из стольких разных сетей скрывают отзыв/ответ
DEVICE_LIMIT_HOURS = 24      # 1 отзыв на корпус с устройства за это время
IP_LIMIT_PER_HOUR = 15       # отзывов с одного IP за час
REPLY_DEVICE_PER_HOUR = 10   # ответов с одного устройства за час
REPLY_IP_PER_HOUR = 30       # ответов с одного IP за час
HELPFUL_PER_IP = 5           # отметок «Полезно» с одного IP на один отзыв
OWN_DELETE_MINUTES = 60      # сколько минут автор может удалить свой отзыв
KEEP_TRACE_DAYS = 30         # через сколько дней стираются хэши IP и устройства
ADMIN_MAX_FAILS = 5          # неверных паролей подряд до блокировки
ADMIN_LOCK_MINUTES = 15      # на сколько блокируется вход в админку

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


# ===================== СЕКРЕТНЫЙ КЛЮЧ =====================

def _load_secret():
    """Ключ для хэширования IP и ID устройства. Хранится только на сервере:
    переменная SECRET_KEY или файл secret_key.txt (создаётся автоматически).
    Без ключа хэш IP можно было бы подобрать перебором."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode("utf-8")
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key.encode("utf-8")
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key.encode("utf-8")


SECRET = None


def _hash(value):
    return hmac.new(SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


# ===================== БАЗА ДАННЫХ =====================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(conn, table, columns):
    existing = _columns(conn, table)
    for col, decl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


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
    review_columns = {
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
        review_columns[key] = "INTEGER"
    _add_columns(conn, "reviews", review_columns)
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
    _add_columns(conn, "replies", {
        "created": "TEXT NOT NULL DEFAULT ''",
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        "reports": "INTEGER NOT NULL DEFAULT 0",
        "device": "TEXT NOT NULL DEFAULT ''",
        "ip": "TEXT NOT NULL DEFAULT ''",
    })

    # Кто уже жаловался / отмечал «Полезно» — чтобы нельзя было накрутить.
    # kind: helpful и report — для отзывов (review_id = id отзыва),
    #       reply_report — для ответов (review_id = id ответа).
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
    _add_columns(conn, "votes", {
        "ip": "TEXT NOT NULL DEFAULT ''",
        "created": "TEXT NOT NULL DEFAULT ''",
    })

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_created ON reviews (created)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_review ON replies (review_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_created ON replies (created)")
    purge_old_traces(conn)
    conn.commit()
    conn.close()


def purge_old_traces(conn):
    """Хэши IP и устройства нужны только для антиспама (сутки) и удаления
    своего отзыва (час). Через KEEP_TRACE_DAYS дней стираем их совсем."""
    cutoff = _iso(_now() - timedelta(days=KEEP_TRACE_DAYS))
    conn.execute(
        "UPDATE reviews SET ip = '', device = '' WHERE created < ? AND (ip != '' OR device != '')",
        (cutoff,))
    conn.execute(
        "UPDATE replies SET ip = '', device = '' WHERE created < ? AND (ip != '' OR device != '')",
        (cutoff,))
    conn.execute("UPDATE votes SET ip = '' WHERE created < ? AND ip != ''", (cutoff,))


def reset_db():
    conn = get_db()
    for table in ("votes", "replies", "reviews"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()
    init_db()


REPLY_PUBLIC_FIELDS = ("id", "review_id", "role", "text", "date")
REPLY_ADMIN_FIELDS = REPLY_PUBLIC_FIELDS + ("hidden", "reports")


def _reply_to_dict(row, admin=False):
    d = {k: row[k] for k in (REPLY_ADMIN_FIELDS if admin else REPLY_PUBLIC_FIELDS)}
    if admin:
        d["hidden"] = bool(d["hidden"])
    return d


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


def _load_replies(conn, admin=False):
    replies = {}
    sql = "SELECT * FROM replies" + ("" if admin else " WHERE hidden = 0") + " ORDER BY id"
    for rep in conn.execute(sql):
        replies.setdefault(rep["review_id"], []).append(_reply_to_dict(rep, admin))
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
    replies = _load_replies(conn, admin=True)
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
    if not isinstance(raw, dict):
        raw = {}
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
        since_day = _iso(now - timedelta(hours=DEVICE_LIMIT_HOURS))
        if device and conn.execute(
            "SELECT 1 FROM reviews WHERE device = ? AND campus = ? AND created > ?",
            (device, campus, since_day),
        ).fetchone():
            return None, "Вы уже оставили отзыв об этом корпусе сегодня. Попробуйте завтра."
        since_hour = _iso(now - timedelta(hours=1))
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
             _iso(now), device, ip, *scores.values()),
        )
        purge_old_traces(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
        result = _review_to_dict(row, {})
        result["can_delete_minutes"] = OWN_DELETE_MINUTES if device else 0
        return result, None
    finally:
        conn.close()


def _distinct_report_ips(conn, target_id, kind):
    return conn.execute(
        "SELECT COUNT(DISTINCT ip) FROM votes WHERE review_id = ? AND kind = ?",
        (target_id, kind),
    ).fetchone()[0]


def _vote(review_id, device, ip, kind):
    """Возвращает (row, new) — new=False, если голос не засчитан."""
    voter = device or ip
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM reviews WHERE id = ? AND hidden = 0", (review_id,)).fetchone()
        if not row:
            return None, False
        if kind == "helpful":
            # Против накрутки через очистку браузера: с одной сети — не больше N отметок
            from_ip = conn.execute(
                "SELECT COUNT(*) FROM votes WHERE review_id = ? AND kind = 'helpful' AND ip = ?",
                (review_id, ip),
            ).fetchone()[0]
            if from_ip >= HELPFUL_PER_IP:
                return row, False
        try:
            conn.execute(
                "INSERT INTO votes (review_id, voter, kind, ip, created) VALUES (?, ?, ?, ?, ?)",
                (review_id, voter, kind, ip, _iso(_now())))
        except sqlite3.IntegrityError:
            return row, False
        if kind == "helpful":
            conn.execute("UPDATE reviews SET helpful = helpful + 1 WHERE id = ?", (review_id,))
        else:
            conn.execute("UPDATE reviews SET reports = reports + 1 WHERE id = ?", (review_id,))
            # Скрываем, только если жаловались из нескольких разных сетей:
            # один человек, очищая браузер, не сможет скрыть чужой отзыв
            if _distinct_report_ips(conn, review_id, "report") >= REPORTS_TO_HIDE:
                conn.execute("UPDATE reviews SET hidden = 1 WHERE id = ?", (review_id,))
        conn.commit()
        return conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone(), True
    finally:
        conn.close()


def report_reply(reply_id, device, ip):
    """Возвращает (found, hidden, new)."""
    voter = device or ip
    conn = get_db()
    try:
        rep = conn.execute(
            "SELECT replies.* FROM replies JOIN reviews ON reviews.id = replies.review_id "
            "WHERE replies.id = ? AND replies.hidden = 0 AND reviews.hidden = 0",
            (reply_id,),
        ).fetchone()
        if not rep:
            return False, False, False
        try:
            conn.execute(
                "INSERT INTO votes (review_id, voter, kind, ip, created) VALUES (?, ?, 'reply_report', ?, ?)",
                (reply_id, voter, ip, _iso(_now())))
        except sqlite3.IntegrityError:
            return True, False, False
        conn.execute("UPDATE replies SET reports = reports + 1 WHERE id = ?", (reply_id,))
        hidden = _distinct_report_ips(conn, reply_id, "reply_report") >= REPORTS_TO_HIDE
        if hidden:
            conn.execute("UPDATE replies SET hidden = 1 WHERE id = ?", (reply_id,))
        conn.commit()
        return True, hidden, True
    finally:
        conn.close()


def add_reply(review_id, data, device, ip):
    text = str(data.get("text") or "").strip()[:MAX_REPLY]
    role = _clean(data.get("role"), MAX_SHORT)
    if role not in ROLES:
        role = ""
    if len(text) < 2:
        return None, "Напишите ответ."
    problem = check_content(text)
    if problem:
        return None, problem
    now = _now()
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM reviews WHERE id = ? AND hidden = 0", (review_id,)).fetchone():
            return None, "Отзыв не найден."
        since_hour = _iso(now - timedelta(hours=1))
        if device and conn.execute(
            "SELECT COUNT(*) FROM replies WHERE device = ? AND created > ?", (device, since_hour)
        ).fetchone()[0] >= REPLY_DEVICE_PER_HOUR:
            return None, "Слишком много ответов подряд. Попробуйте через час."
        if conn.execute(
            "SELECT COUNT(*) FROM replies WHERE ip = ? AND created > ?", (ip, since_hour)
        ).fetchone()[0] >= REPLY_IP_PER_HOUR:
            return None, "Слишком много ответов из вашей сети. Попробуйте через час."
        cur = conn.execute(
            "INSERT INTO replies (review_id, role, text, date, created, device, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (review_id, role, text, _today(), _iso(now), device, ip),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM replies WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _reply_to_dict(row), None
    finally:
        conn.close()


def _delete_review(conn, review_id):
    reply_ids = [r[0] for r in conn.execute("SELECT id FROM replies WHERE review_id = ?", (review_id,))]
    conn.executemany("DELETE FROM votes WHERE review_id = ? AND kind = 'reply_report'",
                     [(i,) for i in reply_ids])
    conn.execute("DELETE FROM replies WHERE review_id = ?", (review_id,))
    conn.execute("DELETE FROM votes WHERE review_id = ? AND kind IN ('helpful', 'report')", (review_id,))
    return conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,)).rowcount


def delete_own_review(review_id, device):
    """Автор может удалить свой отзыв в течение часа с того же устройства."""
    if not device:
        return "Удалить отзыв можно только с того устройства, с которого он отправлен."
    since = _iso(_now() - timedelta(minutes=OWN_DELETE_MINUTES))
    conn = get_db()
    try:
        row = conn.execute("SELECT device, created FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if not row or not hmac.compare_digest(row["device"], device):
            return "Удалить можно только свой отзыв с того устройства, с которого он отправлен."
        if row["created"] <= since:
            return f"Удалить свой отзыв можно только в течение {OWN_DELETE_MINUTES} минут после публикации."
        _delete_review(conn, review_id)
        conn.commit()
        return None
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
                deleted = _delete_review(conn, item_id)
                conn.commit()
                return deleted > 0
            else:
                return False
        elif kind == "replies":
            if action == "hide":
                cur = conn.execute("UPDATE replies SET hidden = 1 WHERE id = ?", (item_id,))
            elif action == "show":
                cur = conn.execute("UPDATE replies SET hidden = 0, reports = 0 WHERE id = ?", (item_id,))
                conn.execute("DELETE FROM votes WHERE review_id = ? AND kind = 'reply_report'", (item_id,))
            elif action == "delete":
                conn.execute("DELETE FROM votes WHERE review_id = ? AND kind = 'reply_report'", (item_id,))
                cur = conn.execute("DELETE FROM replies WHERE id = ?", (item_id,))
            else:
                return False
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

# Защита от подбора пароля: неудачные попытки по хэшу IP (в памяти сервера)
_admin_fails = {}
_admin_lock = threading.Lock()


def check_admin(given, ip):
    """Возвращает None, если пароль верный, иначе (код, текст ошибки)."""
    now = time.time()
    window = ADMIN_LOCK_MINUTES * 60
    with _admin_lock:
        fails = [t for t in _admin_fails.get(ip, []) if now - t < window]
        if len(fails) >= ADMIN_MAX_FAILS:
            _admin_fails[ip] = fails
            wait = int((window - (now - fails[0])) // 60) + 1
            return 429, f"Слишком много неверных попыток. Попробуйте через {wait} мин."
        ok = bool(given) and bool(ADMIN_PASSWORD) and hmac.compare_digest(
            given.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
        if ok:
            _admin_fails.pop(ip, None)
            return None
        fails.append(now)
        _admin_fails[ip] = fails
        return 401, "Неверный пароль"


# ===================== РЕЗЕРВНАЯ КОПИЯ РАЗ В СУТКИ =====================

_backup_lock = threading.Lock()
_backup_done_for = None   # дата (ГГГГ-ММ-ДД по Москве), за которую копия уже есть


def maybe_daily_backup():
    """При первом запросе за день сохраняет копию базы в backups/.
    На бесплатном PythonAnywhere нет задач по расписанию, поэтому копию
    делает сам сайт. Любая ошибка здесь не мешает работе сайта."""
    global _backup_done_for
    day = _now().strftime("%Y-%m-%d")
    if _backup_done_for == day:
        return
    if not _backup_lock.acquire(blocking=False):
        return  # копию уже делает другой запрос
    try:
        import backup
        if not backup.has_backup_for(day):
            backup.make_backup(_now())
        _backup_done_for = day
    except Exception as e:  # noqa: BLE001 — копия не должна ронять сайт
        print(f"Не удалось сделать резервную копию: {e}", file=sys.stderr)
        _backup_done_for = day  # не пытаемся на каждом запросе; повторим завтра
    finally:
        _backup_lock.release()


def setup():
    """Готовит базу, ключ и пароль админки. Вызывается и из main(), и из wsgi.py."""
    global ADMIN_PASSWORD, SECRET
    SECRET = _load_secret()
    init_db()
    ADMIN_PASSWORD = get_admin_password()
    return ADMIN_PASSWORD


# ===================== HTTP =====================

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

# Наружу отдаём только эти файлы и картинки из этих папок — всё остальное
# (база, ключи, код, резервные копии) недоступно из интернета.
PUBLIC_FILES = {"index.html", "admin.html", "rules.html", "404.html", "favicon.svg"}
PUBLIC_DIRS = {"картинки корпусов"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type, X-Device, X-Admin-Password"),
]


def _json_response(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
    return status, headers + CORS_HEADERS, body


def _not_found():
    page = os.path.join(BASE_DIR, "404.html")
    if os.path.isfile(page):
        with open(page, "rb") as f:
            return 404, [("Content-Type", MIME_TYPES[".html"])], f.read()
    return 404, [("Content-Type", MIME_TYPES[".txt"])], "Страница не найдена".encode("utf-8")


def _is_public(rel):
    parts = rel.replace("\\", "/").split("/")
    if len(parts) == 1:
        return parts[0] in PUBLIC_FILES
    if len(parts) == 2 and parts[0] in PUBLIC_DIRS:
        return os.path.splitext(parts[1])[1].lower() in IMAGE_EXTS
    return False


def _static_response(path):
    if path == "/":
        path = "/index.html"
    file_path = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
    if not file_path.startswith(BASE_DIR + os.sep):
        return _not_found()
    rel = os.path.relpath(file_path, BASE_DIR)
    if not _is_public(rel) or not os.path.isfile(file_path):
        return _not_found()
    ext = os.path.splitext(file_path)[1].lower()
    with open(file_path, "rb") as f:
        body = f.read()
    return 200, [("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))] + CORS_HEADERS, body


def _parse_body(raw):
    if not raw or len(raw) > MAX_BODY:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def handle_request(method, raw_path, headers, body, client_ip):
    """Общая логика сайта для обоих способов запуска.

    method    — "GET" / "POST" / "OPTIONS"
    raw_path  — путь запроса (может содержать ?query)
    headers   — словарь заголовков с именами в нижнем регистре
    body      — тело запроса (bytes)
    client_ip — настоящий IP посетителя (его определяет вызывающий код)

    Возвращает (код, список заголовков, тело в bytes).
    """
    path = urllib.parse.unquote(urllib.parse.urlparse(raw_path).path)
    maybe_daily_backup()

    ip = _hash(client_ip or "unknown")
    device = re.sub(r"[^\w-]", "", headers.get("x-device") or "")[:64]
    device = _hash("device:" + device) if device else ""

    if method == "OPTIONS":
        # Нужно, чтобы браузер разрешил запросы, когда страница открыта
        # не с нашего порта (Live Server, file:// и т.п.).
        return 204, list(CORS_HEADERS), b""

    if path.startswith("/api/admin/"):
        denied = check_admin(headers.get("x-admin-password") or "", ip)
        if denied:
            return _json_response({"error": denied[1]}, denied[0])

    if method in ("GET", "HEAD"):
        if path == "/api/reviews":
            return _json_response(fetch_public())
        if path == "/api/admin/reviews":
            return _json_response(fetch_admin())
        if path.startswith("/api/"):
            return _json_response({"error": "Не найдено"}, 404)
        return _static_response(path)

    if method != "POST":
        return _json_response({"error": "Метод не поддерживается"}, 405)

    if path == "/api/reviews":
        review, error = add_review(_parse_body(body), device, ip)
        if error:
            return _json_response({"error": error}, 400)
        return _json_response({"review": review}, 201)

    m = re.fullmatch(r"/api/reviews/(\d+)/(helpful|report)", path)
    if m:
        row, new = _vote(int(m.group(1)), device, ip, m.group(2))
        if row is None:
            return _json_response({"error": "Отзыв не найден"}, 404)
        return _json_response({"helpful": row["helpful"], "hidden": bool(row["hidden"]), "new": new})

    m = re.fullmatch(r"/api/reviews/(\d+)/delete", path)
    if m:
        error = delete_own_review(int(m.group(1)), device)
        if error:
            return _json_response({"error": error}, 403)
        return _json_response({"ok": True})

    m = re.fullmatch(r"/api/reviews/(\d+)/replies", path)
    if m:
        reply, error = add_reply(int(m.group(1)), _parse_body(body), device, ip)
        if error:
            return _json_response({"error": error}, 400)
        return _json_response({"reply": reply}, 201)

    m = re.fullmatch(r"/api/replies/(\d+)/report", path)
    if m:
        found, hidden, new = report_reply(int(m.group(1)), device, ip)
        if not found:
            return _json_response({"error": "Ответ не найден"}, 404)
        return _json_response({"hidden": hidden, "new": new})

    m = re.fullmatch(r"/api/admin/(reviews|replies)/(\d+)/(hide|show|delete)", path)
    if m:
        ok = admin_action(m.group(1), int(m.group(2)), m.group(3))
        return _json_response({"ok": ok}, 200 if ok else 404)

    return _json_response({"error": "Не найдено"}, 404)


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
        # Локально прокси нет, поэтому X-Forwarded-For не читаем:
        # его может подставить кто угодно и обойти лимиты по IP
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
