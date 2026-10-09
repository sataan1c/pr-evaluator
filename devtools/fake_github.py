"""Локальная заглушка GitHub API поверх синтетического репозитория (devtools/demo_repo.py).

Нужна для репетиции и тестов без сети и без токена:
    python devtools/fake_github.py --port 8901
    GITHUB_API_URL=http://127.0.0.1:8901 python fetch_prs.py --repo demo/shop --limit 40

Отвечает только на те запросы, которые делает fetch_prs.py. С --flaky первый запрос
к списку файлов каждого девятого PR получает 502, а первый поисковый запрос — отказ по лимиту:
так проверяется, что выгрузка переживает сбои.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_repo  # noqa: E402


def make_handler(data: dict, flaky: bool = False, log: list | None = None):
    failed_once, lock = set(), threading.Lock()
    by_number = {p["number"]: p for p in data["pulls"]}
    by_sha = {p["head"]["sha"]: p["number"] for p in data["pulls"]}
    repo = data["repo"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, headers=None):
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def fail_once(self, key):
            with lock:
                if not flaky or key in failed_once:
                    return False
                failed_once.add(key)
                return True

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            if log is not None:
                log.append(self.path)
            page, per_page = int(query.get("page", 1)), int(query.get("per_page", 30))

            def paged(items):
                return items[(page - 1) * per_page: page * per_page]

            if path == "/search/issues":
                if self.fail_once("search"):
                    return self.send(403, {"message": "API rate limit exceeded"}, {"Retry-After": "0", "X-RateLimit-Remaining": "0"})
                m = re.search(r"author:(\S+)", query.get("q", ""))
                login = m.group(1) if m else ""
                if login not in data["merged_before"]:
                    return self.send(422, {"message": "Validation Failed: the listed users cannot be searched"})
                return self.send(200, {"total_count": data["merged_before"][login], "incomplete_results": False, "items": []})

            prefix = f"/repos/{repo}"
            if not path.startswith(prefix):
                return self.send(404, {"message": "Not Found"})
            rest = path[len(prefix):]
            if rest == "/pulls":
                ordered = sorted(data["pulls"], key=lambda p: p["updated_at"], reverse=True)
                return self.send(200, paged(ordered))
            m = re.fullmatch(r"/pulls/(\d+)(/files|/reviews|/comments)?", rest)
            if m and int(m.group(1)) in by_number:
                n, tail = int(m.group(1)), m.group(2)
                if tail == "/files":
                    if n % 9 == 0 and self.fail_once(("files", n)):
                        return self.send(502, {"message": "Bad Gateway"})
                    return self.send(200, paged(data["files"][n]))
                if tail == "/reviews":
                    return self.send(200, paged(data["reviews"][n]))
                if tail == "/comments":
                    return self.send(200, paged(data["comments"][n]))
                return self.send(200, dict(by_number[n], **data["details"][n]))
            m = re.fullmatch(r"/commits/([0-9a-f]+)/(check-runs|status)", rest)
            if m and m.group(1) in by_sha:
                ci = data["checks"][by_sha[m.group(1)]]
                if m.group(2) == "status":   # у «unknown» нет ни check runs, ни статусов
                    return self.send(200, {"state": "pending", "statuses": []})
                runs = {"success": ["success", "skipped"], "failure": ["success", "failure"],
                        "mixed": ["success", "cancelled"], "unknown": []}[ci]
                return self.send(200, {"total_count": len(runs), "check_runs": [{"conclusion": c} for c in runs]})
            return self.send(404, {"message": "Not Found"})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def stop(self):
        """Остановить и освободить порт."""
        self.shutdown()
        self.server_close()


def start(port: int = 0, flaky: bool = False, seed: int = 7, log: list | None = None, data: dict | None = None):
    """Запускает сервер в фоновом потоке. Возвращает (сервер, адрес)."""
    data = data or demo_repo.generate(seed)
    server = Server(("127.0.0.1", port), make_handler(data, flaky, log))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Заглушка GitHub API с синтетическим репозиторием demo/shop")
    ap.add_argument("--port", type=int, default=8901)
    ap.add_argument("--flaky", action="store_true", help="имитировать сбои: 502 и отказ по лимиту")
    args = ap.parse_args()
    server, address = start(args.port, args.flaky)
    print(f"Заглушка GitHub слушает {address}, репозиторий {demo_repo.REPO}. Остановить: Ctrl+C")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
