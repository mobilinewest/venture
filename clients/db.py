"""Слой хранения данных: SQLite, только стандартная библиотека."""

import sqlite3
from datetime import datetime

STATUSES = ("lead", "active", "inactive")
INTERACTION_TYPES = ("note", "call", "meeting", "email", "order", "payment")

CLIENT_FIELDS = ("name", "phone", "email", "company", "address", "status", "tags", "notes")
INTERACTION_FIELDS = ("type", "description", "amount", "date")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'lead',
    tags        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    type        TEXT NOT NULL DEFAULT 'note',
    description TEXT NOT NULL DEFAULT '',
    amount      REAL NOT NULL DEFAULT 0,
    date        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interactions_client ON interactions(client_id);
"""


class ValidationError(ValueError):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _row(row):
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # SQLite lower() понимает только ASCII — регистрируем Unicode-версию для поиска по кириллице.
        self.conn.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- клиенты ----------

    @staticmethod
    def _clean_client(data, partial=False):
        clean = {}
        for field in CLIENT_FIELDS:
            if field in data:
                value = data[field]
                clean[field] = "" if value is None else str(value).strip()
        if not partial or "name" in clean:
            if not clean.get("name"):
                raise ValidationError("Имя клиента обязательно")
        if "status" in clean:
            if not clean["status"]:
                clean["status"] = "lead"
            if clean["status"] not in STATUSES:
                raise ValidationError(f"Недопустимый статус: {clean['status']}")
        if clean.get("email") and "@" not in clean["email"]:
            raise ValidationError("Некорректный email")
        if "tags" in clean:
            tags = [t.strip() for t in clean["tags"].split(",") if t.strip()]
            clean["tags"] = ", ".join(dict.fromkeys(tags))
        return clean

    def create_client(self, data):
        clean = self._clean_client(data)
        now = _now()
        clean.setdefault("status", "lead")
        clean["created_at"] = clean["updated_at"] = now
        cols = ", ".join(clean)
        marks = ", ".join("?" for _ in clean)
        cur = self.conn.execute(f"INSERT INTO clients ({cols}) VALUES ({marks})", tuple(clean.values()))
        self.conn.commit()
        return self.get_client(cur.lastrowid)

    def get_client(self, client_id):
        row = self.conn.execute(
            """
            SELECT c.*,
                   COALESCE(SUM(CASE WHEN i.type = 'order'   THEN i.amount END), 0) AS total_orders,
                   COALESCE(SUM(CASE WHEN i.type = 'payment' THEN i.amount END), 0) AS total_paid,
                   MAX(i.date) AS last_contact
            FROM clients c LEFT JOIN interactions i ON i.client_id = c.id
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (client_id,),
        ).fetchone()
        client = _row(row)
        if client:
            client["balance"] = round(client["total_paid"] - client["total_orders"], 2)
        return client

    def update_client(self, client_id, data):
        if not self.get_client(client_id):
            return None
        clean = self._clean_client(data, partial=True)
        if clean:
            clean["updated_at"] = _now()
            sets = ", ".join(f"{k} = ?" for k in clean)
            self.conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*clean.values(), client_id))
            self.conn.commit()
        return self.get_client(client_id)

    def delete_client(self, client_id):
        cur = self.conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_clients(self, query="", status="", tag="", sort="name"):
        sql = """
            SELECT c.*,
                   COALESCE(SUM(CASE WHEN i.type = 'order'   THEN i.amount END), 0) AS total_orders,
                   COALESCE(SUM(CASE WHEN i.type = 'payment' THEN i.amount END), 0) AS total_paid,
                   MAX(i.date) AS last_contact
            FROM clients c LEFT JOIN interactions i ON i.client_id = c.id
        """
        where, params = [], []
        if query:
            like = f"%{query.lower()}%"
            where.append(
                "(ulower(c.name) LIKE ? OR ulower(c.phone) LIKE ? OR ulower(c.email) LIKE ?"
                " OR ulower(c.company) LIKE ? OR ulower(c.tags) LIKE ? OR ulower(c.notes) LIKE ?)"
            )
            params += [like] * 6
        if status:
            where.append("c.status = ?")
            params.append(status)
        if tag:
            where.append("(',' || REPLACE(ulower(c.tags), ', ', ',') || ',') LIKE ?")
            params.append(f"%,{tag.lower().strip()},%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY c.id"
        order = {
            "name": "ulower(c.name) ASC",
            "created": "c.created_at DESC",
            "updated": "c.updated_at DESC",
            "revenue": "total_orders DESC",
            "last_contact": "last_contact IS NULL, last_contact DESC",
        }.get(sort, "ulower(c.name) ASC")
        sql += f" ORDER BY {order}, c.id"
        clients = [dict(r) for r in self.conn.execute(sql, params)]
        for c in clients:
            c["balance"] = round(c["total_paid"] - c["total_orders"], 2)
        return clients

    # ---------- взаимодействия ----------

    @staticmethod
    def _clean_interaction(data):
        clean = {}
        for field in INTERACTION_FIELDS:
            if field in data and data[field] is not None:
                clean[field] = data[field]
        itype = str(clean.get("type") or "note")
        if itype not in INTERACTION_TYPES:
            raise ValidationError(f"Недопустимый тип записи: {itype}")
        clean["type"] = itype
        clean["description"] = str(clean.get("description", "")).strip()
        try:
            amount = float(str(clean.get("amount") or 0).replace(",", ".").replace(" ", ""))
        except ValueError:
            raise ValidationError("Сумма должна быть числом")
        if amount < 0:
            raise ValidationError("Сумма не может быть отрицательной")
        clean["amount"] = round(amount, 2)
        date = str(clean.get("date") or "").strip()
        if date:
            try:
                datetime.fromisoformat(date)
            except ValueError:
                raise ValidationError("Дата должна быть в формате ГГГГ-ММ-ДД")
        else:
            date = datetime.now().date().isoformat()
        clean["date"] = date
        if not clean["description"] and not clean["amount"]:
            raise ValidationError("Добавьте описание или сумму")
        return clean

    def add_interaction(self, client_id, data):
        if not self.get_client(client_id):
            return None
        clean = self._clean_interaction(data)
        cur = self.conn.execute(
            "INSERT INTO interactions (client_id, type, description, amount, date, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, clean["type"], clean["description"], clean["amount"], clean["date"], _now()),
        )
        self.conn.execute("UPDATE clients SET updated_at = ? WHERE id = ?", (_now(), client_id))
        self.conn.commit()
        return _row(self.conn.execute("SELECT * FROM interactions WHERE id = ?", (cur.lastrowid,)).fetchone())

    def list_interactions(self, client_id):
        rows = self.conn.execute(
            "SELECT * FROM interactions WHERE client_id = ? ORDER BY date DESC, id DESC", (client_id,)
        )
        return [dict(r) for r in rows]

    def delete_interaction(self, interaction_id):
        cur = self.conn.execute("DELETE FROM interactions WHERE id = ?", (interaction_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # ---------- статистика ----------

    def stats(self):
        by_status = {s: 0 for s in STATUSES}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM clients GROUP BY status"):
            by_status[row["status"]] = row["n"]
        money = self.conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN type = 'order'   THEN amount END), 0) AS orders,
                   COALESCE(SUM(CASE WHEN type = 'payment' THEN amount END), 0) AS paid
            FROM interactions
            """
        ).fetchone()
        negative = [c["balance"] for c in self.list_clients() if c["balance"] < 0]
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "total_orders": money["orders"],
            "total_paid": money["paid"],
            "debt": round(-sum(negative), 2),
            "debtors": len(negative),
        }

    def all_tags(self):
        tags = set()
        for row in self.conn.execute("SELECT tags FROM clients WHERE tags != ''"):
            tags.update(t.strip() for t in row["tags"].split(",") if t.strip())
        return sorted(tags, key=str.lower)
