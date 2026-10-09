"""Локальная заглушка модели с OpenAI-совместимым API. Это НЕ нейросеть.

Баллы ставятся простыми правилами по тексту PR (размер, пути файлов, CI, длина описания).
Они годятся только для одного: прогнать весь конвейер без ключа, без денег и без сети,
чтобы участники 3 и 4 получили файлы правильного формата и проверили свой код.
Показывать эти баллы как результат нельзя.

    python devtools/fake_llm.py --port 8902
    SCORER_BASE_URL=http://127.0.0.1:8902/v1 SCORER_MODEL=fake-llm SCORER_API_KEY=local python score.py

Заглушка нарочно ведёт себя как настоящая модель в плохой день: иногда отвечает не по
формату, ссылается на несуществующий файл или на строки вне diff и слегка меняет баллы
между прогонами. Так проверяется, что score.py всё это ловит.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CRITERIA = ["complexity", "quality", "risk", "clarity"]
RISKY = re.compile(r"migrations/|\.sql\b|auth|session|_lock|thread|DELETE FROM|DROP INDEX|ALTER TABLE", re.I)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _clamp(value: int) -> int:
    return max(1, min(5, value))


def _section(text: str, start: str, end: str) -> str:
    a = text.find(start)
    if a < 0:
        return ""
    b = text.find(end, a + len(start))
    return text[a + len(start): b if b >= 0 else len(text)].strip()


def score_text(text: str, call_no: int = 1) -> dict:
    """Правила вместо модели. call_no — который раз приходит этот же текст (номер прогона)."""
    title = _section(text, "Title: ", "\n")
    description = _section(text, "Description:\n", "\n\nCI result:")
    ci = _section(text, "CI result: ", "\n")
    size = re.search(r"Size reported by GitHub: \+(\d+) / -(\d+)", text)
    lines = int(size.group(1)) + int(size.group(2)) if size else 0
    diffs = text.split("\nDiffs:\n", 1)[1] if "\nDiffs:\n" in text else ""
    shown = []   # (путь, первая строка hunk, последняя строка hunk)
    for block in re.split(r"^=== ", diffs, flags=re.M)[1:]:
        path = block.split(" ===", 1)[0]
        m = HUNK.search(block)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            shown.append((path, start, start + max(count, 1) - 1))
    paths = [p for p, _, _ in shown]
    has_tests = any("test" in p for p in paths)
    risky = bool(RISKY.search(" ".join(paths) + "\n" + diffs))
    words = len(description.split()) if description != "(no description)" else 0

    complexity = 1 if lines <= 15 else 2 if lines <= 50 else 3 if lines <= 120 else 4 if lines <= 300 else 5
    quality = _clamp(3 + has_tests + (ci == "success") - (ci == "failure") - ("CHANGES_REQUESTED" in text))
    risk = _clamp(1 + 2 * risky + (lines > 80) + (lines > 30 and not has_tests))
    clarity = _clamp((1 if words <= 2 else 2 if words <= 8 else 3 if words <= 25 else 4 if words <= 60 else 5)
                     - (len(title.split()) < 3))
    scores = {"complexity": complexity, "quality": quality, "risk": risk, "clarity": clarity}

    # Небольшой разброс между прогонами, детерминированный по тексту и номеру прогона.
    digest = hashlib.sha1(f"{text}|{call_no}".encode("utf-8")).digest()
    if digest[0] < 30:
        target = CRITERIA[digest[1] % 4]
        scores[target] = _clamp(scores[target] + (2 if digest[0] < 6 else 1) * (1 if digest[2] % 2 else -1))

    stable = hashlib.sha1(text.encode("utf-8")).digest()
    reasons = {
        "complexity": f"Заглушка: изменено {lines} строк в {len(paths)} показанных файлах.",
        "quality": f"Заглушка: тесты {'есть' if has_tests else 'не найдены'}, CI: {ci or 'unknown'}.",
        "risk": f"Заглушка: {'затронуты миграции, авторизация или блокировки' if risky else 'опасных мест по путям и коду не видно'}.",
        "clarity": f"Заглушка: в описании {words} слов.",
    }
    answer = {"summary": f"Заглушка модели: «{title}».", "scores": {}}
    for i, c in enumerate(CRITERIA):
        evidence = []
        if shown and c != "clarity":
            path, start, end = shown[(stable[3] + i) % len(shown)]
            evidence.append({"path": path, "lines": f"{start}-{min(end, start + 8)}"})
        if c == "risk" and stable[4] < 40:
            evidence.append({"path": "src/does_not_exist.py", "lines": "1-5"})          # выдуманный файл
        if c == "complexity" and shown and stable[5] < 40:
            evidence.append({"path": shown[0][0], "lines": "9000-9010"})                # строки вне diff
        answer["scores"][c] = {"score": scores[c], "reason": reasons[c], "evidence": evidence}
    return answer


class FakeLLM:
    def __init__(self, reject_schema=False, limit_every=0, always_429=False, never_valid=False, misbehave=True,
                 first_answer_bad=False):
        self.reject_schema, self.limit_every, self.first_answer_bad = reject_schema, limit_every, first_answer_bad
        self.always_429, self.never_valid, self.misbehave = always_429, never_valid, misbehave
        self.lock = threading.Lock()
        self.calls = Counter()     # сколько раз приходил каждый текст PR
        self.requests = []         # все принятые тела запросов: тесты смотрят, что ушло «модели»
        self.hits = 0

    def reply(self, payload: dict):
        """(статус, тело ответа, заголовки)."""
        with self.lock:
            self.hits += 1
            hit = self.hits
        if self.always_429 or (self.limit_every and hit % self.limit_every == 0):
            return 429, {"error": {"message": "Rate limit reached, slow down"}}, {"Retry-After": "0"}
        fmt = (payload.get("response_format") or {}).get("type")
        if self.reject_schema and fmt == "json_schema":
            return 400, {"error": {"message": "response_format of type json_schema is not supported by this model"}}, {}
        messages = payload.get("messages") or []
        users = [m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
        if not users:
            return 400, {"error": {"message": "no user message"}}, {}
        with self.lock:
            self.requests.append(payload)
        retry = users[-1].startswith("That answer was rejected")
        text = next((u for u in reversed(users) if u.startswith("PULL REQUEST DATA")), None)
        if text is None:
            content = "OK"   # проверка связи при первом запуске score.py
        else:
            with self.lock:
                if not retry:
                    self.calls[text] += 1
                call_no = self.calls[text]
            answer = score_text(text, call_no)
            digest = hashlib.sha1(f"bad|{text}|{call_no}".encode("utf-8")).digest()
            if self.never_valid or (not retry and (self.first_answer_bad or (self.misbehave and digest[0] < 25))):
                answer["scores"]["risk"]["score"] = 7          # балл вне шкалы: ответ должен быть отклонён
            content = json.dumps(answer, ensure_ascii=False)
            if self.misbehave and digest[1] < 40:
                content = "Вот оценка:\n```json\n" + content + "\n```"   # обёртка, которую модели любят добавлять
        prompt_chars = sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))
        body = {
            "id": f"fake-{hit}", "object": "chat.completion", "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_chars // 4, "completion_tokens": len(content) // 4},
        }
        return 200, body, {}


def make_handler(fake: FakeLLM):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            key = (self.headers.get("Authorization") or "").replace("Bearer", "").strip()
            if not self.path.endswith("/chat/completions"):
                status, body, headers = 404, {"error": {"message": "unknown path"}}, {}
            elif not key or key == "bad-key":
                status, body, headers = 401, {"error": {"message": "invalid api key"}}, {}
            else:
                try:
                    status, body, headers = fake.reply(json.loads(raw.decode("utf-8")))
                except ValueError:
                    status, body, headers = 400, {"error": {"message": "body is not JSON"}}, {}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def stop(self):
        """Остановить и освободить порт."""
        self.shutdown()
        self.server_close()


def start(port: int = 0, **options):
    """Запускает заглушку в фоновом потоке. Возвращает (сервер, базовый адрес API, объект заглушки)."""
    fake = FakeLLM(**options)
    server = Server(("127.0.0.1", port), make_handler(fake))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1", fake


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Заглушка модели: правила вместо нейросети, только для проверки конвейера")
    ap.add_argument("--port", type=int, default=8902)
    args = ap.parse_args()
    server, address, _ = start(args.port)
    print(f"Заглушка модели слушает {address}. Это не нейросеть: баллы ставятся правилами. Остановить: Ctrl+C")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
