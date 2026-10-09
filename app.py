"""Карта вклада: одно приложение. Оценка PR, множители, проверка метода и дашборд за одну команду.

    python app.py                                   # дашборд на данных проекта (папка data/)
    python app.py run                               # досчитать недостающее, затем дашборд
    python app.py run --repo owner/name             # выгрузить PR из GitHub и пройти весь путь
    python app.py demo                              # репетиция на выдуманном репозитории, без сети и ключей
    python app.py serve --dir demo_out              # дашборд на данных из другой папки
    python app.py check                             # проверить файлы данных
    python app.py test                              # прогнать тесты проекта

Пока PR не оценены моделью, дашборд показывает, что уже известно (PR, авторы, откаты и
исправления), и подсказывает, как получить оценки.

Дашборд открывается в браузере по адресу вида http://127.0.0.1:8765 и на каждое обновление
страницы перечитывает файлы с диска: пересчитали множители, нажали F5, увидели новое.
Поле «Контекст от автора» сохраняется в notes.json рядом с данными.

REST-отчёты (часть 3 схемы POC) отдаёт тот же сервер:
    GET /api/reports/employees/{employeeId}?since=ГГГГ-ММ-ДД&until=ГГГГ-ММ-ДД
    GET /api/reports/team
Логика отчётов — в reports.py.

Сервер слушает только этот компьютер (127.0.0.1) и ничего не отправляет в сеть. В сеть ходят
только шаги выгрузки и оценки в команде run: GitHub и выбранная модель.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
DASH = HERE / "dashboard"
sys.path.insert(0, str(DASH))
import make_data  # noqa: E402
import reports  # noqa: E402  REST-отчёты по сотруднику и команде

STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"), "/styles.css": ("styles.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/i18n.js": ("i18n.js", "text/javascript")}
MODEL_LANGUAGE = {"ru": "Russian", "az": "Azerbaijani", "en": "English"}
MAX_NOTE = 4000


def say(text: str = "") -> None:
    print(text, flush=True)


def has_prs(folder: Path) -> bool:
    return (folder / "prs.json").is_file()


def has_data(folder: Path) -> bool:
    return has_prs(folder) and (folder / "scores.json").is_file()


def data_folder() -> Path | None:
    """Где лежат данные, когда папка не названа: в текущей папке, в её data/ или в demo_out."""
    here = Path.cwd()
    for folder in (here, here / "data", here / "demo_out"):
        if has_prs(folder):
            return folder
    return None


# ---------- заметки ----------
def read_notes(folder: Path) -> dict:
    """notes.json -> {номер PR: текст}. Отсутствующий или испорченный файл значит «заметок нет»."""
    try:
        stored = json.loads((folder / "notes.json").read_text(encoding="utf-8"))
        return {str(k): v["text"] for k, v in stored.items() if isinstance(v, dict) and isinstance(v.get("text"), str)}
    except (OSError, ValueError, AttributeError):
        return {}


def write_note(folder: Path, number: int, text: str) -> None:
    path = folder / "notes.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    if text.strip():
        stored[str(number)] = {"text": text, "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    else:
        stored.pop(str(number), None)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- сервер ----------
def make_handler(folder: Path, token: str, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ContributionMap"

        def log_message(self, *args):
            pass

        def reply(self, status: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def fail(self, status: int, message: str) -> None:
            self.reply(status, json.dumps({"ok": False, "error": message}, ensure_ascii=False).encode("utf-8"))

        def local_request(self) -> bool:
            """Запрос адресован именно этому серверу, а не подменённому имени: защита от чужих сайтов."""
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.local_request():
                return self.fail(403, "this server answers only on 127.0.0.1")
            path = urlparse(self.path).path
            if path in STATIC:
                name, kind = STATIC[path]
                return self.reply(200, (DASH / name).read_bytes(), kind)
            if path == "/data.js":
                try:
                    data = make_data.collect(folder, None)
                except make_data.DataProblem:
                    data = {}        # данных пока нет: дашборд покажет приглашение их загрузить
                data["notes"] = read_notes(folder)
                payload = make_data.as_script(data) + "window.DASHBOARD_API = " + json.dumps({"token": token}) + ";\n"
                return self.reply(200, payload.encode("utf-8"), "text/javascript")
            if path == "/favicon.ico":
                return self.reply(204, b"", "image/x-icon")
            if path.startswith("/api/reports/"):
                return self.report(path)
            return self.fail(404, "not found")

        def report(self, path: str):
            """GET /api/reports/employees/{employeeId} и GET /api/reports/team, ?since=&until= по дате мержа."""
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            since, until = query.get("since", ""), query.get("until", "")
            problem = reports.period_problem(since, until)
            if problem:
                return self.fail(400, problem)
            try:
                data = make_data.collect(folder, None)
            except make_data.DataProblem as e:
                return self.fail(404, str(e))
            if not data.get("scores"):
                return self.fail(404, "PR ещё не оценены моделью: запустите python run_all.py")
            if path == "/api/reports/team":
                body = reports.team_report(data, since, until)
            elif path.startswith("/api/reports/employees/") and path.count("/") == 4:
                employee = unquote(path.rsplit("/", 1)[1])
                body = reports.employee_report(data, employee, since, until)
                if body is None:
                    return self.fail(404, f"у {employee} нет оценённых PR за этот период")
            else:
                return self.fail(404, "not found")
            return self.reply(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))

        def do_POST(self):
            if not self.local_request():
                return self.fail(403, "this server answers only on 127.0.0.1")
            if urlparse(self.path).path != "/api/note":
                return self.fail(404, "not found")
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), token):
                return self.fail(403, "the page is out of date: reload it")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= 6 * MAX_NOTE + 200:
                return self.fail(413, "the note is too long")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                number, text = body["number"], body["text"]
            except (ValueError, KeyError, TypeError):
                return self.fail(400, "expected JSON with number and text")
            if not isinstance(number, int) or isinstance(number, bool) or number < 0 or not isinstance(text, str):
                return self.fail(400, "number must be an integer and text a string")
            if len(text) > MAX_NOTE:
                return self.fail(413, "the note is too long")
            try:
                with lock:
                    write_note(folder, number, text)
            except OSError as e:
                return self.fail(500, f"cannot write notes.json: {e}")
            return self.reply(200, b'{"ok": true}')

    return Handler


def start_server(folder: Path, port: int):
    """Поднимает сервер на 127.0.0.1. Если порт занят, пробует следующие двадцать."""
    handler = make_handler(folder, secrets.token_urlsafe(24), threading.Lock())
    candidates = [0] if port == 0 else range(port, port + 21)
    for candidate in candidates:
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError:
            continue
    sys.exit(f"ОШИБКА: порты {port}–{port + 20} заняты. Укажите другой: python app.py serve --port 9000")


def serve(folder: Path, port: int = 8765, browser: bool = True) -> int:
    folder = folder.resolve()
    if not folder.is_dir():
        sys.exit(f"ОШИБКА: папки {folder} нет.")
    server = start_server(folder, port)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}"
    say(f"Дашборд: {url}")
    state = ("" if has_data(folder) else
             "  (PR ещё не оценены моделью: настройте ключ командой python score.py и запустите python run_all.py)" if has_prs(folder) else
             "  (пока пусто: загрузите файлы кнопкой «Данные» или посчитайте их командой demo или run)")
    say(f"Данные: {folder}" + state)
    say("Пересчитали файлы — обновите страницу в браузере. Остановить: Ctrl+C")
    if browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\nОстановлено.")
    finally:
        server.server_close()
    return 0


# ---------- команды ----------
def script(name: str, args: list, cwd: Path | None = None, env: dict | None = None) -> int:
    return subprocess.call([sys.executable, str(HERE / name)] + [str(a) for a in args], cwd=str(cwd) if cwd else None, env=env)


def command_demo(args) -> int:
    sys.path.insert(0, str(HERE))
    import demo
    code = demo.main(limit=args.limit, workdir=args.workdir, runs=args.runs)
    folder = Path.cwd() / args.workdir
    if not has_data(folder):
        say("\nРепетиция не дошла до оценок, дашборд показывать не на чем. Причина в выводе выше.")
        return code or 1
    if args.no_serve:
        return code
    say()
    return serve(folder, args.port, not args.no_browser)


def command_run(args) -> int:
    work = Path(args.workdir) if args.workdir else HERE / "data"
    command = ["--no-app", "--workdir", work, "--limit", args.limit, "--min-age-days", args.min_age_days, "--runs", args.runs]
    if args.repo:
        command += ["--repo", args.repo]
    if args.no_style:
        command.append("--no-style")
    env = dict(os.environ)
    if args.lang:
        env["SCORER_LANG"] = MODEL_LANGUAGE[args.lang]      # на этом языке модель напишет суть PR и причины баллов
    if args.rescore:
        command.append("--rescore")
    code = script("run_all.py", command, env=env)
    if not has_prs(work):
        say("\nКонвейер не дошёл до данных, дашборд показывать не на чем. Причина в выводе выше.")
        return code or 1
    if args.no_serve:
        return code
    say()
    return serve(work, args.port, not args.no_browser)


def command_default(args) -> int:
    folder = data_folder()
    if folder:
        return serve(folder, args.port, not args.no_browser)
    say("Ни здесь, ни в папке data/ нет prs.json, показывать пока нечего. С чего начать:\n")
    say("  python app.py demo                    репетиция на выдуманном репозитории, без сети и ключей")
    say("  python app.py run --repo owner/name   настоящий прогон: нужен токен GitHub и ключ модели")
    say("  python app.py serve --dir ПАПКА       показать уже посчитанные данные из другой папки")
    say("\nПодробности: python app.py --help и файл README.md")
    return 2


def main() -> int:
    for stream in (sys.stdout, sys.stderr):          # консоль Windows не всегда умеет кириллицу
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Карта вклада: оценка PR, множители, проверка метода и дашборд",
                                 epilog="Без команды: открыть дашборд на данных из текущей папки.")
    ap.add_argument("--port", type=int, default=8765, help="порт дашборда (0 = любой свободный)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер, только напечатать адрес")
    sub = ap.add_subparsers(dest="command")

    def common(p, serve_flags=True):
        p.add_argument("--port", type=int, default=argparse.SUPPRESS, help="порт дашборда (0 = любой свободный)")
        p.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS, help="не открывать браузер")
        if serve_flags:
            p.add_argument("--no-serve", action="store_true", help="только посчитать, дашборд не запускать")

    p = sub.add_parser("demo", help="репетиция на заглушках без сети и ключей, затем дашборд")
    p.add_argument("--limit", type=int, default=70, help="сколько PR выдуманного репозитория взять")
    p.add_argument("--runs", type=int, default=3, help="прогонов заглушки на один PR")
    p.add_argument("--workdir", default="demo_out", help="куда положить результаты")
    common(p)

    p = sub.add_parser("run", help="досчитать недостающее (с --repo: выгрузить PR из GitHub), затем дашборд")
    p.add_argument("--repo", help="owner/name; без него берётся готовый prs.json из папки данных")
    p.add_argument("--limit", type=int, default=150, help="сколько принятых PR выгружать")
    p.add_argument("--min-age-days", type=float, default=30, help="брать PR не моложе стольких дней")
    p.add_argument("--runs", type=int, default=3, help="прогонов модели на один PR")
    p.add_argument("--no-style", action="store_true", help="пропустить тест стиля (он стоит ещё один прогон модели)")
    p.add_argument("--lang", choices=sorted(MODEL_LANGUAGE), help="язык, на котором модель пишет причины баллов: ru, az или en. "
                   "Смена языка меняет промпт, и все PR оцениваются заново")
    p.add_argument("--rescore", action="store_true", help="оценить заново, даже если оценки есть: нужно после правки рубрики")
    p.add_argument("--workdir", help="папка для данных и результатов (по умолчанию data/ проекта)")
    common(p)

    p = sub.add_parser("serve", help="дашборд на уже посчитанных данных")
    p.add_argument("--dir", help="папка с prs.json, scores.json и остальными файлами (по умолчанию data/)")
    common(p, serve_flags=False)

    p = sub.add_parser("check", help="проверить формат и согласованность файлов данных")
    p.add_argument("--dir", help="папка с данными (по умолчанию data/)")

    sub.add_parser("test", help="прогнать тесты проекта")
    args = ap.parse_args()

    if args.command == "demo":
        return command_demo(args)
    if args.command == "run":
        return command_run(args)
    if args.command == "serve":
        return serve(Path(args.dir) if args.dir else data_folder() or Path.cwd(), args.port, not args.no_browser)
    if args.command == "check":
        return script("check_data.py", ["--dir", args.dir] if args.dir else [])
    if args.command == "test":
        return subprocess.call([sys.executable, "-m", "unittest"], cwd=str(HERE))
    return command_default(args)


if __name__ == "__main__":
    # Запуск двойным кликом в Windows: окно не закрывается сразу, чтобы подсказку можно было прочитать.
    double_clicked = os.name == "nt" and len(sys.argv) == 1 and "PROMPT" not in os.environ
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    if double_clicked and code and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nНажмите Enter, чтобы закрыть окно.")
        except EOFError:
            pass
    sys.exit(code)
