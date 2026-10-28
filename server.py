#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ГУАП • Отзывы — сервер с базой данных SQLite.

Запуск:   python server.py
Сайт:     http://localhost:8000

API:
  GET  /api/reviews               — список всех отзывов (JSON)
  POST /api/reviews               — добавить новый отзыв
  POST /api/reviews/<id>/helpful  — увеличить счётчик «Полезно»

Отзывы хранятся в файле reviews.db и видны всем пользователям сайта.
"""

import json
import os
import re
import socket
import sqlite3
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
PORT = 8000

CAMPUSES = [
    "Главный корпус на Большой Морской",
    "Корпус на Гастелло",
    "Корпус на Ленсовета",
]

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Начальные отзывы (заполняются при первом запуске)
SEED_REVIEWS = [
    {
        "name": "Алексей Ковалёв",
        "faculty": "ФПТИ • 3 курс",
        "campus": "Главный корпус на Большой Морской",
        "rating": 5,
        "text": "Отличный корпус! Большие светлые аудитории, современное оборудование в лабораториях. Столовая радует ценами, а библиотека — тишиной.",
        "date": "12 марта 2024",
        "helpful": 0,
    },
    {
        "name": "Мария Соколова",
        "faculty": "ФКП • 2 курс",
        "campus": "Корпус на Гастелло",
        "rating": 4,
        "text": "Уютный корпус, но иногда не хватает мест в компьютерных классах. Преподаватели отзывчивые, всегда помогут разобраться в материале.",
        "date": "28 февраля 2024",
        "helpful": 0,
    },
    {
        "name": "Дмитрий Волков",
        "faculty": "ФРТ • 4 курс",
        "campus": "Корпус на Ленсовета",
        "rating": 4,
        "text": "Спокойный корпус с историческими интерьерами. Аудитории небольшие, но уютные. Рядом метро «Петроградская» — очень удобно добираться.",
        "date": "15 января 2024",
        "helpful": 0,
    },
]


# ===================== БАЗА ДАННЫХ =====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL,
            faculty TEXT NOT NULL,
            campus  TEXT NOT NULL,
            rating  INTEGER NOT NULL,
            text    TEXT NOT NULL,
            date    TEXT NOT NULL,
            helpful INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO reviews (name, faculty, campus, rating, text, date, helpful) "
            "VALUES (:name, :faculty, :campus, :rating, :text, :date, :helpful)",
            SEED_REVIEWS,
        )
    conn.commit()
    conn.close()


def fetch_reviews():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reviews ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_review(data):
    name = (data.get("name") or "").strip()
    faculty = (data.get("faculty") or "").strip()
    campus = (data.get("campus") or "").strip()
    text = (data.get("text") or "").strip()
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        rating = 0

    if not name or not campus or not text:
        return None, "Заполните все поля."
    if rating < 1 or rating > 5:
        return None, "Поставьте оценку от 1 до 5."
    if campus not in CAMPUSES:
        return None, "Выберите корпус из списка."

    today = date.today()
    date_str = f"{today.day} {MONTHS[today.month - 1]} {today.year}"

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO reviews (name, faculty, campus, rating, text, date, helpful) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (name, faculty, campus, rating, text, date_str),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row), None


def increment_helpful(review_id):
    conn = get_db()
    cur = conn.execute(
        "UPDATE reviews SET helpful = helpful + 1 WHERE id = ?", (review_id,)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    conn.close()
    if cur.rowcount == 0:
        return None
    return dict(row)


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


class Handler(BaseHTTPRequestHandler):
    server_version = "GUAPReviews/1.0"

    def log_message(self, fmt, *args):
        try:
            with open(os.path.join(BASE_DIR, "server.log"), "a", encoding="utf-8") as f:
                f.write(f"{self.command} {self.path} len={self.headers.get('Content-Length', 0)}\n")
        except OSError:
            pass

    # ---------- утилиты ----------

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
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- GET ----------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/api/reviews":
            self._send_json({"reviews": fetch_reviews()})
            return

        # Статические файлы
        if path == "/":
            path = "/index.html"
        file_path = os.path.normpath(os.path.join(BASE_DIR, path.lstrip("/")))
        if not file_path.startswith(BASE_DIR):
            self.send_error(403)
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
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/api/reviews":
            data = self._read_body()
            review, error = add_review(data)
            if error:
                self._send_json({"error": error}, 400)
            else:
                self._send_json({"review": review}, 201)
            return

        m = re.fullmatch(r"/api/reviews/(\d+)/helpful", path)
        if m:
            review = increment_helpful(int(m.group(1)))
            if review is None:
                self._send_json({"error": "Отзыв не найден"}, 404)
            else:
                self._send_json({"review": review})
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
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 52)
    print("  ГУАП • Отзывы — сервер запущен")
    print(f"  Локально:   http://localhost:{PORT}")
    print(f"  В сети:     http://{get_lan_ip()}:{PORT}")
    print("  База данных: reviews.db (SQLite)")
    print("  Остановка:  Ctrl+C")
    print("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        server.server_close()


if __name__ == "__main__":
    main()