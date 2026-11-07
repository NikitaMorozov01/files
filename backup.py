#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Резервная копия базы отзывов.

Автоматически: сервер сам делает копию раз в сутки — при первом запросе
за день (см. maybe_daily_backup в server.py). Ничего настраивать не нужно.

Вручную:
    python backup.py

Копии лежат в папке backups/ (имя с датой), хранятся последние KEEP штук.
Копия делается через SQLite backup API, поэтому её можно снимать прямо
во время работы сайта. Папка backups/ не попадает в git и не открывается
из интернета.
"""

import glob
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP = 30


def has_backup_for(day):
    """Есть ли уже копия за этот день (day — строка ГГГГ-ММ-ДД)."""
    return bool(glob.glob(os.path.join(BACKUP_DIR, f"reviews-{day}_*.db")))


def make_backup(now=None):
    """Сохраняет копию и удаляет лишние старые. Возвращает (путь, число отзывов, удалено)."""
    if not os.path.exists(DB_PATH):
        return None, 0, 0
    now = now or datetime.now()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"reviews-{now:%Y-%m-%d_%H-%M}.db")
    tmp = target + ".tmp"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)
        count = dst.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    finally:
        src.close()
        dst.close()
    # Сначала пишем во временный файл: недописанная копия не будет считаться готовой
    os.replace(tmp, target)

    old = sorted(glob.glob(os.path.join(BACKUP_DIR, "reviews-*.db")))[:-KEEP]
    for path in old:
        os.remove(path)
    return target, count, len(old)


def main():
    target, count, removed = make_backup()
    if not target:
        print("reviews.db не найдена — копировать нечего.")
        return
    print(f"Копия сохранена: {target} (отзывов: {count})")
    if removed:
        print(f"Удалено старых копий: {removed}")


if __name__ == "__main__":
    main()
