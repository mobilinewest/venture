import json
import threading
import unittest
import urllib.request
from urllib.error import HTTPError

from clients.db import Database, ValidationError
from clients.server import csv_to_clients, clients_to_csv, serve


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_create_and_get(self):
        c = self.db.create_client({"name": " Иван Петров ", "phone": "+7 900", "tags": "VIP, опт, VIP"})
        self.assertEqual(c["name"], "Иван Петров")
        self.assertEqual(c["status"], "lead")
        self.assertEqual(c["tags"], "VIP, опт")
        self.assertEqual(c["balance"], 0)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.db.create_client({"name": ""})
        with self.assertRaises(ValidationError):
            self.db.create_client({"name": "A", "email": "bad"})
        with self.assertRaises(ValidationError):
            self.db.create_client({"name": "A", "status": "zzz"})

    def test_update_partial(self):
        c = self.db.create_client({"name": "Анна", "phone": "1"})
        u = self.db.update_client(c["id"], {"status": "active"})
        self.assertEqual(u["status"], "active")
        self.assertEqual(u["phone"], "1")
        self.assertIsNone(self.db.update_client(999, {"name": "x"}))

    def test_search_cyrillic_case_insensitive(self):
        self.db.create_client({"name": "Мария Сидорова", "company": "ООО Ромашка"})
        self.db.create_client({"name": "John Smith"})
        self.assertEqual(len(self.db.list_clients("сидорова")), 1)
        self.assertEqual(len(self.db.list_clients("ромашка")), 1)
        self.assertEqual(len(self.db.list_clients("JOHN")), 1)

    def test_tag_filter_exact(self):
        self.db.create_client({"name": "A", "tags": "опт, Москва"})
        self.db.create_client({"name": "B", "tags": "оптовик"})
        self.assertEqual([c["name"] for c in self.db.list_clients(tag="опт")], ["A"])
        self.assertEqual(self.db.all_tags(), ["Москва", "опт", "оптовик"])

    def test_balance_and_stats(self):
        a = self.db.create_client({"name": "A", "status": "active"})
        b = self.db.create_client({"name": "B"})
        self.db.add_interaction(a["id"], {"type": "order", "amount": "1 500,50", "date": "2026-01-10"})
        self.db.add_interaction(a["id"], {"type": "payment", "amount": 1000})
        self.db.add_interaction(b["id"], {"type": "order", "amount": 200})
        self.db.add_interaction(b["id"], {"type": "payment", "amount": 300})
        self.assertEqual(self.db.get_client(a["id"])["balance"], -500.5)
        s = self.db.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_status"]["active"], 1)
        self.assertEqual(s["debt"], 500.5)  # переплата B не гасит долг A
        self.assertEqual(s["debtors"], 1)

    def test_interaction_validation(self):
        c = self.db.create_client({"name": "A"})
        for bad in ({"type": "x", "description": "d"}, {"amount": "abc"}, {"amount": -1},
                    {"description": "d", "date": "31.12.2026"}, {}):
            with self.assertRaises(ValidationError):
                self.db.add_interaction(c["id"], bad)

    def test_delete_cascades(self):
        c = self.db.create_client({"name": "A"})
        self.db.add_interaction(c["id"], {"description": "hi"})
        self.assertTrue(self.db.delete_client(c["id"]))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0], 0)

    def test_csv_roundtrip(self):
        self.db.create_client({"name": "Пётр; «Кавычки»", "phone": "123", "notes": "строка1\nстрока2"})
        text = clients_to_csv(self.db.list_clients())
        records = csv_to_clients(text)
        self.assertEqual(records[0]["name"], "Пётр; «Кавычки»")
        self.assertEqual(records[0]["notes"], "строка1\nстрока2")
        self.assertNotIn("balance", records[0])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Database(":memory:")
        cls.httpd = serve(cls.db, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, raw=False):
        data = None if body is None else (body.encode() if raw else json.dumps(body).encode())
        r = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                text = resp.read().decode("utf-8")
                return resp.status, (json.loads(text) if "json" in resp.headers["Content-Type"] else text)
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        st, c = self.req("POST", "/api/clients", {"name": "Олег", "email": "o@x.ru"})
        self.assertEqual(st, 201)
        st, _ = self.req("POST", f"/api/clients/{c['id']}/interactions", {"type": "order", "amount": 100})
        self.assertEqual(st, 201)
        st, full = self.req("GET", f"/api/clients/{c['id']}")
        self.assertEqual(full["total_orders"], 100)
        self.assertEqual(len(full["interactions"]), 1)
        st, upd = self.req("PUT", f"/api/clients/{c['id']}", {"status": "active"})
        self.assertEqual(upd["status"], "active")
        st, lst = self.req("GET", "/api/clients?q=%D0%BE%D0%BB%D0%B5%D0%B3")  # "олег"
        self.assertEqual(len(lst), 1)
        st, csv_text = self.req("GET", "/api/export.csv")
        self.assertIn("Олег", csv_text)
        st, _ = self.req("DELETE", f"/api/clients/{c['id']}")
        self.assertEqual(st, 200)
        st, err = self.req("GET", f"/api/clients/{c['id']}")
        self.assertEqual(st, 404)

    def test_errors_and_import(self):
        st, err = self.req("POST", "/api/clients", {"name": ""})
        self.assertEqual(st, 400)
        self.assertIn("обязательно", err["error"])
        st, r = self.req("POST", "/api/import", "Имя;Телефон;Email\nА;1;\n;2;\nБ;3;bad\n", raw=True)
        self.assertEqual(r["created"], 1)
        self.assertEqual(len(r["errors"]), 2)

    def test_static_and_traversal(self):
        st, html = self.req("GET", "/")
        self.assertIn("Учёт клиентов", html)
        st, _ = self.req("GET", "/../db.py")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
