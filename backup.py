#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Резервная копия базы отзывов.

    python backup.py

Кладёт копию reviews.db в папку backups/ (имя с датой) и хранит
последние KEEP копий. Копия делается через SQLite backup API, поэтому
её можно снимать прямо во время работы сайта.

На PythonAnywhere: вкладка Tasks → ежедневная задача с командой
    python3 /home/<логин>/files/backup.py
Папка backups/ не попадает в git и не открывается из интернета.
"""

import glob
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "reviews.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP = 30


def main():
    if not os.path.exists(DB_PATH):
        print("reviews.db не найдена — копировать нечего.")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    target = os.path.join(BACKUP_DIR, f"reviews-{datetime.now():%Y-%m-%d_%H-%M}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    count = dst.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    src.close()
    dst.close()
    print(f"Копия сохранена: {target} (отзывов: {count})")

    old = sorted(glob.glob(os.path.join(BACKUP_DIR, "reviews-*.db")))[:-KEEP]
    for path in old:
        os.remove(path)
    if old:
        print(f"Удалено старых копий: {len(old)}")


if __name__ == "__main__":
    main()
