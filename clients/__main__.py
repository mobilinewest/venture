"""Запуск: python -m clients [--port 8000] [--db clients.db]"""

import argparse
import webbrowser

from .db import Database
from .server import serve


def main():
    parser = argparse.ArgumentParser(description="Учёт клиентов — веб-приложение")
    parser.add_argument("--host", default="127.0.0.1", help="адрес (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument("--db", default="clients.db", help="файл базы данных (по умолчанию clients.db)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()

    db = Database(args.db)
    httpd = serve(db, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"Учёт клиентов запущен: {url}  (база: {args.db})")
    print("Остановить: Ctrl+C")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
        db.close()


if __name__ == "__main__":
    main()
