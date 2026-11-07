# -*- coding: utf-8 -*-
"""
WSGI-вход для хостинга (PythonAnywhere и т.п.).

Вся логика — в server.py (handle_request), этот файл только переводит
запрос WSGI в её формат. Локально по-прежнему: python server.py

На PythonAnywhere в файле /var/www/<логин>_pythonanywhere_com_wsgi.py
оставьте только:

    import os, sys
    path = "/home/<логин>/files"
    if path not in sys.path:
        sys.path.insert(0, path)
    os.environ["ADMIN_PASSWORD"] = "придумайте-надёжный-пароль"
    from wsgi import application
"""

from http import HTTPStatus

import server

server.setup()


def _status_line(code):
    try:
        return f"{code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"{code} Unknown"


def _client_ip(environ):
    """Настоящий IP посетителя за прокси хостинга.

    PythonAnywhere кладёт его в X-Real-IP. Первое значение X-Forwarded-For
    брать нельзя: его присылает сам браузер, и подделав его, можно обойти
    лимиты по IP. Последнее значение дописывает прокси — ему можно верить.
    """
    real = environ.get("HTTP_X_REAL_IP", "").strip()
    if real:
        return real
    fwd = environ.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return environ.get("REMOTE_ADDR", "")


def application(environ, start_response):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    body = environ["wsgi.input"].read(length) if 0 < length <= server.MAX_BODY else b""

    headers = {
        key[5:].replace("_", "-").lower(): value
        for key, value in environ.items()
        if key.startswith("HTTP_")
    }

    # PATH_INFO в WSGI — это байты, прочитанные как latin-1; возвращаем UTF-8
    raw_path = environ.get("PATH_INFO", "/").encode("latin-1").decode("utf-8", "replace")
    raw_path = server.urllib.parse.quote(raw_path)

    status, resp_headers, resp_body = server.handle_request(
        environ.get("REQUEST_METHOD", "GET"),
        raw_path,
        headers,
        body,
        _client_ip(environ),
    )
    resp_headers = list(resp_headers) + [("Content-Length", str(len(resp_body)))]
    start_response(_status_line(status), resp_headers)
    if environ.get("REQUEST_METHOD") == "HEAD":
        return [b""]
    return [resp_body]
