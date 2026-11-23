"""HTTP-сервер с JSON API и веб-интерфейсом. Только стандартная библиотека."""

import csv
import io
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import CLIENT_FIELDS, ValidationError

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 5 * 1024 * 1024

CSV_HEADERS = {
    "id": "ID",
    "name": "Имя",
    "phone": "Телефон",
    "email": "Email",
    "company": "Компания",
    "address": "Адрес",
    "status": "Статус",
    "tags": "Теги",
    "notes": "Заметки",
    "total_orders": "Сумма заказов",
    "total_paid": "Оплачено",
    "balance": "Баланс",
    "last_contact": "Последний контакт",
    "created_at": "Создан",
}
HEADER_TO_FIELD = {v.lower(): k for k, v in CSV_HEADERS.items()} | {k: k for k in CSV_HEADERS}


def clients_to_csv(clients):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(CSV_HEADERS.values())
    for c in clients:
        writer.writerow([c.get(k, "") if c.get(k) is not None else "" for k in CSV_HEADERS])
    # BOM, чтобы Excel корректно открыл кириллицу
    return "﻿" + buf.getvalue()


def csv_to_clients(text):
    text = text.lstrip("﻿")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return []
    fields = [HEADER_TO_FIELD.get(h.strip().lower()) for h in rows[0]]
    result = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        record = {f: v for f, v in zip(fields, row) if f in CLIENT_FIELDS}
        result.append(record)
    return result


def make_handler(db):
    routes = []
    lock = threading.Lock()  # одно соединение SQLite на все потоки

    def route(method, pattern):
        def deco(fn):
            routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return deco

    class Handler(BaseHTTPRequestHandler):
        server_version = "ClientBook/1.0"

        def log_message(self, fmt, *args):
            pass  # тихий режим

        # ---------- вспомогательные ----------

        def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status=HTTPStatus.OK):
            self._send(status, json.dumps(data, ensure_ascii=False))

        def _error(self, message, status=HTTPStatus.BAD_REQUEST):
            self._json({"error": message}, status)

        def _body_text(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("Слишком большой запрос")
            return self.rfile.read(length).decode("utf-8") if length else ""

        def _body_json(self):
            text = self._body_text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                raise ValidationError("Некорректный JSON")
            if not isinstance(data, dict):
                raise ValidationError("Ожидается JSON-объект")
            return data

        def _dispatch(self, method):
            url = urlparse(self.path)
            self.query = {k: v[0] for k, v in parse_qs(url.query).items()}
            for m, pattern, fn in routes:
                match = pattern.match(url.path)
                if m == method and match:
                    try:
                        with lock:
                            fn(self, *match.groups())
                    except ValidationError as e:
                        self._error(str(e))
                    return
            if method == "GET":
                self._static(url.path)
            else:
                self._error("Не найдено", HTTPStatus.NOT_FOUND)

        def _static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            file = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in file.parents or not file.is_file():
                self._error("Не найдено", HTTPStatus.NOT_FOUND)
                return
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8"}
            self._send(HTTPStatus.OK, file.read_bytes(), types.get(file.suffix, "application/octet-stream"))

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # ---------- API ----------

        @route("GET", r"/api/clients")
        def list_clients(self):
            q = self.query
            self._json(db.list_clients(q.get("q", ""), q.get("status", ""), q.get("tag", ""), q.get("sort", "name")))

        @route("POST", r"/api/clients")
        def create_client(self):
            self._json(db.create_client(self._body_json()), HTTPStatus.CREATED)

        @route("GET", r"/api/clients/(\d+)")
        def get_client(self, cid):
            client = db.get_client(int(cid))
            if not client:
                return self._error("Клиент не найден", HTTPStatus.NOT_FOUND)
            client["interactions"] = db.list_interactions(int(cid))
            self._json(client)

        @route("PUT", r"/api/clients/(\d+)")
        def update_client(self, cid):
            client = db.update_client(int(cid), self._body_json())
            if not client:
                return self._error("Клиент не найден", HTTPStatus.NOT_FOUND)
            self._json(client)

        @route("DELETE", r"/api/clients/(\d+)")
        def delete_client(self, cid):
            if not db.delete_client(int(cid)):
                return self._error("Клиент не найден", HTTPStatus.NOT_FOUND)
            self._json({"ok": True})

        @route("POST", r"/api/clients/(\d+)/interactions")
        def add_interaction(self, cid):
            item = db.add_interaction(int(cid), self._body_json())
            if not item:
                return self._error("Клиент не найден", HTTPStatus.NOT_FOUND)
            self._json(item, HTTPStatus.CREATED)

        @route("DELETE", r"/api/interactions/(\d+)")
        def delete_interaction(self, iid):
            if not db.delete_interaction(int(iid)):
                return self._error("Запись не найдена", HTTPStatus.NOT_FOUND)
            self._json({"ok": True})

        @route("GET", r"/api/stats")
        def stats(self):
            data = db.stats()
            data["tags"] = db.all_tags()
            self._json(data)

        @route("GET", r"/api/export\.csv")
        def export_csv(self):
            q = self.query
            clients = db.list_clients(q.get("q", ""), q.get("status", ""), q.get("tag", ""), q.get("sort", "name"))
            self._send(HTTPStatus.OK, clients_to_csv(clients), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="clients.csv"'})

        @route("POST", r"/api/import")
        def import_csv(self):
            records = csv_to_clients(self._body_text())
            created, errors = 0, []
            for n, record in enumerate(records, start=2):
                try:
                    db.create_client(record)
                    created += 1
                except ValidationError as e:
                    errors.append(f"Строка {n}: {e}")
            self._json({"created": created, "errors": errors})

    return Handler


def serve(db, host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), make_handler(db))
    return httpd
