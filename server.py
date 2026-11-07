#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ГУАП • Отзывы — сервер с базой данных SQLite.

Запуск:            python server.py
Сайт:              http://localhost:8000
Очистить базу:     python server.py --reset   (удалит все отзывы и добавит примеры)

API:
  GET  /api/reviews                 — все отзывы с ответами + статистика по корпусам
  POST /api/reviews                 — добавить отзыв
  POST /api/reviews/<id>/helpful    — «Полезно» (+1)
  POST /api/reviews/<id>/replies    — ответить на отзыв

Отзывы анонимные: вместо имени — роль (студент / абитуриент / выпускник),
курс, институт и направление. Корпус оценивается по 5 критериям,
общая оценка — среднее по критериям.
"""

import json
import os
import re
import socket
import sqlite3
import sys
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
PORT = 8000

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

# Ограничения длины полей
MAX_TEXT = 2000
MAX_SHORT = 120
MAX_REPLY = 600

# Примеры отзывов для пустой базы. Помечены is_demo=1 — на сайте у них
# значок «Пример», а команда --reset удаляет их вместе с остальными.
SEED_REVIEWS = [
    {
        "campus": "Главный корпус на Большой Морской",
        "role": "Студент", "course": "3 курс", "institute": "Институт информационных технологий и программирования",
        "direction": "Программная инженерия",
        "r_rooms": 5, "r_food": 4, "r_tech": 4, "r_clean": 5, "r_transport": 5,
        "pros": "Красивое историческое здание, центр города, большая библиотека",
        "cons": "Легко заблудиться в коридорах на первом курсе",
        "text": "Учиться в самом центре Петербурга — отдельное удовольствие. Аудитории просторные, в лабораториях нормальное оборудование. До метро минут десять пешком, станций рядом несколько.",
        "date": "14 октября 2026", "created": "2026-10-14T12:00:00", "helpful": 12,
    },
    {
        "campus": "Главный корпус на Большой Морской",
        "role": "Абитуриент", "course": "", "institute": "",
        "direction": "Прикладной искусственный интеллект",
        "r_rooms": 4, "r_food": 4, "r_tech": 3, "r_clean": 4, "r_transport": 5,
        "pros": "Удобно добираться, приёмная комиссия работает быстро",
        "cons": "Длинные очереди в приёмной комиссии летом",
        "text": "Приходил на день открытых дверей. Здание впечатляет, студенты-волонтёры всё показали и ответили на вопросы. Wi-Fi в холле работал не везде.",
        "date": "2 октября 2026", "created": "2026-10-02T15:30:00", "helpful": 5,
    },
    {
        "campus": "Главный корпус на Большой Морской",
        "role": "Выпускник", "course": "", "institute": "Институт аэрокосмических приборов и систем",
        "direction": "Приборостроение",
        "r_rooms": 4, "r_food": 3, "r_tech": 4, "r_clean": 4, "r_transport": 5,
        "pros": "Сильные преподаватели, хорошие лаборатории",
        "cons": "В столовой в большой перерыв не протолкнуться",
        "text": "Отучился четыре года и ни разу не пожалел. Главный минус — очереди в столовой, лучше брать еду с собой или ходить в кафе рядом.",
        "date": "20 сентября 2026", "created": "2026-09-20T10:15:00", "helpful": 9,
    },
    {
        "campus": "Корпус на Гастелло",
        "role": "Студент", "course": "1 курс", "institute": "Институт фундаментальной подготовки и технологических инноваций",
        "direction": "Информатика и вычислительная техника",
        "r_rooms": 4, "r_food": 5, "r_tech": 4, "r_clean": 4, "r_transport": 4,
        "pros": "Вкусная и недорогая столовая, компактный корпус",
        "cons": "На переменах тесно в коридорах",
        "text": "Здесь учатся младшие курсы технических направлений, так что почти все свои. Корпус небольшой, между парами успеваешь всё. От метро «Московская» идти около 15 минут.",
        "date": "25 октября 2026", "created": "2026-10-25T09:40:00", "helpful": 14,
    },
    {
        "campus": "Корпус на Гастелло",
        "role": "Студент", "course": "2 курс", "institute": "Гуманитарный факультет",
        "direction": "Реклама и связи с общественностью",
        "r_rooms": 3, "r_food": 4, "r_tech": 3, "r_clean": 4, "r_transport": 4,
        "pros": "Уютная атмосфера, отзывчивые преподаватели",
        "cons": "Не во всех аудиториях работают проекторы",
        "text": "В целом корпус приятный. Хотелось бы больше розеток в аудиториях и стабильный Wi-Fi — на ноутбуке работать не всегда удобно.",
        "date": "8 октября 2026", "created": "2026-10-08T13:05:00", "helpful": 6,
    },
    {
        "campus": "Корпус на Гастелло",
        "role": "Абитуриент", "course": "", "institute": "",
        "direction": "Информационная безопасность",
        "r_rooms": 4, "r_food": 4, "r_tech": 4, "r_clean": 5, "r_transport": 4,
        "pros": "Чисто и светло",
        "cons": "Немного далеко от метро",
        "text": "Заходил посмотреть корпус перед поступлением. Чисто, светло, охрана вежливая. Рядом парк — приятно прогуляться между парами.",
        "date": "29 сентября 2026", "created": "2026-09-29T17:20:00", "helpful": 3,
    },
    {
        "campus": "Корпус на Ленсовета",
        "role": "Студент", "course": "2 курс", "institute": "Институт технологий предпринимательства и права",
        "direction": "Юриспруденция",
        "r_rooms": 4, "r_food": 3, "r_tech": 4, "r_clean": 4, "r_transport": 4,
        "pros": "Спокойно и тихо, мало людей",
        "cons": "Маленькая столовая",
        "text": "Тихий корпус, в котором удобно готовиться к семинарам. Аудитории небольшие, но для наших групп хватает. Добираюсь от метро «Московская».",
        "date": "18 октября 2026", "created": "2026-10-18T11:00:00", "helpful": 7,
    },
    {
        "campus": "Корпус на Ленсовета",
        "role": "Студент", "course": "4 курс", "institute": "Институт технологий предпринимательства и права",
        "direction": "Экономика",
        "r_rooms": 5, "r_food": 3, "r_tech": 5, "r_clean": 5, "r_transport": 3,
        "pros": "Современные компьютерные классы",
        "cons": "Утром переполненные автобусы",
        "text": "За годы учёбы корпус заметно обновили: новые компьютеры, отремонтированные аудитории. Единственное — дорога утром в час пик.",
        "date": "11 сентября 2026", "created": "2026-09-11T08:50:00", "helpful": 10,
    },
]

SEED_REPLIES = [
    # (номер отзыва в SEED_REVIEWS, роль, текст)
    (3, "Студент", "Подтверждаю про столовую, особенно сырники по утрам!"),
    (1, "Студент", "На дне открытых дверей лучше сразу идти в библиотеку — там тише и есть розетки."),
]


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
    # Новые столбцы (если база старая — добавляем их)
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
    }
    for key in CRITERIA:
        new_columns[key] = "INTEGER"
    existing = _columns(conn, "reviews")
    for col, decl in new_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {decl}")
    # У старых отзывов была одна оценка — считаем её общей
    conn.execute("UPDATE reviews SET rating_avg = rating WHERE rating_avg IS NULL")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replies (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            role      TEXT NOT NULL DEFAULT '',
            text      TEXT NOT NULL,
            date      TEXT NOT NULL
        )
        """
    )

    count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    if count == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    ids = []
    for r in SEED_REVIEWS:
        scores = [r[k] for k in CRITERIA]
        avg = round(sum(scores) / len(scores), 1)
        cur = conn.execute(
            "INSERT INTO reviews (name, faculty, campus, rating, rating_avg, text, date, helpful, "
            "role, course, institute, direction, pros, cons, created, is_demo, "
            + ", ".join(CRITERIA) + ") VALUES (" + ", ".join(["?"] * (16 + len(CRITERIA))) + ")",
            ("", "", r["campus"], round(avg), avg, r["text"], r["date"], r["helpful"],
             r["role"], r["course"], r["institute"], r["direction"], r["pros"], r["cons"],
             r["created"], 1, *scores),
        )
        ids.append(cur.lastrowid)
    for idx, role, text in SEED_REPLIES:
        conn.execute(
            "INSERT INTO replies (review_id, role, text, date) VALUES (?, ?, ?, ?)",
            (ids[idx], role, text, SEED_REVIEWS[idx]["date"]),
        )


def reset_db():
    conn = get_db()
    conn.execute("DROP TABLE IF EXISTS replies")
    conn.execute("DROP TABLE IF EXISTS reviews")
    conn.commit()
    conn.close()
    init_db()


def _review_to_dict(row, replies):
    d = dict(row)
    d["criteria"] = {k: d.pop(k) for k in CRITERIA}
    d["rating"] = d.pop("rating_avg") or d["rating"]
    d["is_demo"] = bool(d["is_demo"])
    d["replies"] = replies.get(d["id"], [])
    # Имя на сайте не показываем — отзывы анонимные
    d.pop("name", None)
    return d


def fetch_all():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reviews ORDER BY created DESC, id DESC").fetchall()
    replies = {}
    for rep in conn.execute("SELECT * FROM replies ORDER BY id"):
        replies.setdefault(rep["review_id"], []).append(dict(rep))
    conn.close()
    reviews = [_review_to_dict(r, replies) for r in rows]
    return {"reviews": reviews, "stats": build_stats(reviews), "criteria": CRITERIA}


def build_stats(reviews):
    stats = {}
    for campus in CAMPUSES:
        items = [r for r in reviews if r["campus"] == campus]
        entry = {"count": len(items), "rating": None, "criteria": {}}
        if items:
            entry["rating"] = round(sum(r["rating"] for r in items) / len(items), 1)
        for key in CRITERIA:
            vals = [r["criteria"][key] for r in items if r["criteria"][key]]
            entry["criteria"][key] = round(sum(vals) / len(vals), 1) if vals else None
        stats[campus] = entry
    return stats


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _today():
    t = date.today()
    return f"{t.day} {MONTHS[t.month - 1]} {t.year}"


def add_review(data):
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
    avg = round(sum(scores.values()) / len(scores), 1)

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reviews (name, faculty, campus, rating, rating_avg, text, date, helpful, "
        "role, course, institute, direction, pros, cons, created, is_demo, "
        + ", ".join(CRITERIA) + ") VALUES (" + ", ".join(["?"] * (16 + len(CRITERIA))) + ")",
        ("", "", campus, round(avg), avg, text, _today(), 0,
         role, course if role == "Студент" else "", institute, direction, pros, cons,
         datetime.now().isoformat(timespec="seconds"), 0, *scores.values()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return _review_to_dict(row, {}), None


def increment_helpful(review_id):
    conn = get_db()
    cur = conn.execute("UPDATE reviews SET helpful = helpful + 1 WHERE id = ?", (review_id,))
    conn.commit()
    row = conn.execute("SELECT helpful FROM reviews WHERE id = ?", (review_id,)).fetchone()
    conn.close()
    return None if cur.rowcount == 0 else row["helpful"]


def add_reply(review_id, data):
    text = str(data.get("text") or "").strip()[:MAX_REPLY]
    role = _clean(data.get("role"), MAX_SHORT)
    if role not in ROLES:
        role = ""
    if len(text) < 2:
        return None, "Напишите ответ."
    conn = get_db()
    if not conn.execute("SELECT 1 FROM reviews WHERE id = ?", (review_id,)).fetchone():
        conn.close()
        return None, "Отзыв не найден."
    cur = conn.execute(
        "INSERT INTO replies (review_id, role, text, date) VALUES (?, ?, ?, ?)",
        (review_id, role, text, _today()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM replies WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row), None


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
PRIVATE_FILES = {"reviews.db", "server.log", "server.py"}


class Handler(BaseHTTPRequestHandler):
    server_version = "GUAPReviews/2.0"

    def log_message(self, fmt, *args):
        try:
            with open(os.path.join(BASE_DIR, "server.log"), "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path} len={self.headers.get('Content-Length', 0)}\n")
        except OSError:
            pass

    CORS_HEADERS = [
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
    ]

    def _send_cors_headers(self):
        for name, value in self.CORS_HEADERS:
            self.send_header(name, value)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 50_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- GET ----------

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

        if path == "/api/reviews":
            self._send_json(fetch_all())
            return

        if path == "/":
            path = "/index.html"
        file_path = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if not file_path.startswith(BASE_DIR + os.sep):
            self.send_error(403)
            return
        rel = os.path.relpath(file_path, BASE_DIR)
        if rel in PRIVATE_FILES or rel.startswith((".git", "__pycache__")):
            self.send_error(404)
            return
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            ctype = MIME_TYPES.get(ext, "application/octet-stream")
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    # ---------- CORS / OPTIONS ----------

    def do_OPTIONS(self):
        # Нужно, чтобы браузер разрешил сохранение отзыва,
        # когда страница открыта не с нашего порта (Live Server, file:// и т.п.).
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- POST ----------

    def do_POST(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

        if path == "/api/reviews":
            review, error = add_review(self._read_body())
            if error:
                self._send_json({"error": error}, 400)
            else:
                self._send_json({"review": review}, 201)
            return

        m = re.fullmatch(r"/api/reviews/(\d+)/helpful", path)
        if m:
            helpful = increment_helpful(int(m.group(1)))
            if helpful is None:
                self._send_json({"error": "Отзыв не найден"}, 404)
            else:
                self._send_json({"helpful": helpful})
            return

        m = re.fullmatch(r"/api/reviews/(\d+)/replies", path)
        if m:
            reply, error = add_reply(int(m.group(1)), self._read_body())
            if error:
                self._send_json({"error": error}, 400)
            else:
                self._send_json({"reply": reply}, 201)
            return

        self.send_error(404)


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
        print("База очищена: добавлены примеры отзывов.")
        return
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 52)
    print("  ГУАП • Отзывы — сервер запущен")
    print(f"  Локально:   http://localhost:{PORT}")
    print(f"  В сети:     http://{get_lan_ip()}:{PORT}")
    print("  База данных: reviews.db (SQLite)")
    print("  Очистить базу: python server.py --reset")
    print("  Остановка:  Ctrl+C")
    print("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        server.server_close()


if __name__ == "__main__":
    main()
