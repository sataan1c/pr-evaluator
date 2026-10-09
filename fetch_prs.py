from __future__ import annotations

import argparse   
import hashlib    
import json      
import os         
import re         
import sys        
import time       
from pathlib import Path  

import requests   


HERE = Path(__file__).resolve().parent

API = "https://api.github.com"     # базовый адрес GitHub REST API
CACHE_DIR = HERE / "raw"           # папка кэша
MAX_PATCH_LINES = 300              # патч длиннее обрезается (требование плана)
MAX_INLINE_COMMENTS = 30           # сколько комментариев к строкам брать на PR
MAX_COMMENT_CHARS = 1000           # слишком длинные комментарии обрезаем

NOISE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock", "Pipfile.lock",
    "uv.lock",
}

NOISE_DIRS = ("dist/", "build/", "vendor/", "node_modules/", "__snapshots__/")

NOISE_SUFFIXES = (".min.js", ".min.css", ".map", ".snap")

BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".zip", ".gz", ".tar", ".whl",
    ".so", ".dll", ".dylib", ".exe", ".bin", ".pyc", ".mp4", ".mp3", ".wav",
)

SERVICE_TITLE = re.compile(
    r"^\s*(prepare release|release v?\d|set version|bump |update dependenc|"
    r"chore\(deps\)|chore: release|\[pre-commit\.ci\])",
    re.IGNORECASE,
)


def is_noise(f: dict) -> bool:
    """Решает, мусорный ли файл. f — один файл из ответа GitHub /pulls/N/files."""
    path = f["filename"]                 # полный путь, например "src/pkg/a.py"
    lower = path.lower()                 # для сравнения расширений без учёта регистра
    name = path.rsplit("/", 1)[-1]       # только имя файла: "a.py"
    if name in NOISE_FILES:
        return True
    # Папка шума может быть в начале пути ("dist/x.js") или внутри ("web/dist/x.js").
    if any(path.startswith(d) or f"/{d}" in path for d in NOISE_DIRS):
        return True
    if lower.endswith(NOISE_SUFFIXES) or lower.endswith(BINARY_SUFFIXES):
        return True
    return False


def is_bot(user: dict) -> bool:
    """У ботов в GitHub type == "Bot", а логин оканчивается на [bot] (dependabot[bot])."""
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def load_token():
    """Ищет токен: сначала в переменной GITHUB_TOKEN, потом в файле token.txt.
    Возвращает пару (токен, откуда_взят) или (None, None)."""
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t, "переменная GITHUB_TOKEN"
    token_file = HERE / "token.txt"
    if token_file.exists():
        # utf-8-sig убирает невидимый символ BOM, который любит добавлять Блокнот.
        # strip убирает пробелы, переносы строк и случайные кавычки.
        t = token_file.read_text(encoding="utf-8-sig").strip().strip('"').strip("'")
        if t:
            return t, "файл token.txt"
    return None, None


# Session — «постоянное соединение»: заголовки задаются один раз и
# автоматически добавляются ко всем запросам.
session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",   # просим ответ в формате GitHub JSON
    "X-GitHub-Api-Version": "2022-11-28",      # фиксируем версию API
})
token, token_source = load_token()
if token:
    # Так GitHub понимает, что запрос от тебя, и даёт лимит 5000/час вместо 60.
    session.headers["Authorization"] = f"Bearer {token}"
    # Печатаем только последние 4 символа, чтобы токен не засветился на экране.
    print(f"Токен найден ({token_source}), оканчивается на ...{token[-4:]}", file=sys.stderr)
else:
    print("ВНИМАНИЕ: токен не найден (нет GITHUB_TOKEN и token.txt), лимит всего 60 запросов в час",
          file=sys.stderr)


# ============================================================================
# 3. ЗАПРОСЫ К GITHUB С КЭШЕМ
# ============================================================================

def get(url: str, params: dict | None = None):
    """Делает GET-запрос, но сначала смотрит, нет ли ответа в кэше.

    Как работает кэш:
      - адрес + параметры склеиваются в строку;
      - из строки делается хэш sha1 (40 символов) — это имя файла в raw/;
      - если такой файл есть, читаем его и в сеть не идём;
      - если нет, идём в GitHub и сохраняем ответ в этот файл.
    Поэтому имена файлов в raw/ выглядят как случайный набор букв.
    """
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    cache_file = CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    # До 5 попыток на случай лимита или сбоя GitHub.
    for attempt in range(5):
        r = session.get(url, params=params, timeout=30)

        # 403/429 + "осталось 0 запросов" = упёрлись в лимит.
        # GitHub сам сообщает, когда лимит обновится (X-RateLimit-Reset)
        # или сколько ждать (Retry-After). Спим и пробуем снова.
        if r.status_code in (403, 429) and (
            r.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in r.headers
        ):
            if "Retry-After" in r.headers:
                wait = int(r.headers["Retry-After"]) + 1
            else:
                wait = int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time()) + 1
            print(f"Лимит исчерпан, жду {wait} с...", file=sys.stderr)
            time.sleep(max(wait, 1))
            continue

        # 5xx — сбой на стороне GitHub. Ждём 1, 2, 4, 8... секунд и повторяем.
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue

        # Любая другая ошибка (401 — плохой токен, 404 — нет репозитория)
        # останавливает скрипт с понятным сообщением.
        r.raise_for_status()

        data = r.json()
        CACHE_DIR.mkdir(exist_ok=True)   # создаём raw/, если её ещё нет
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data
    raise RuntimeError(f"Не удалось получить {url}")


def get_all_pages(url: str, params: dict | None = None, max_items: int | None = None):
    """Пагинация. GitHub отдаёт максимум 100 элементов за запрос.
    Запрашиваем страницу 1, 2, 3... и склеиваем, пока страница не придёт
    неполной (меньше 100 = больше данных нет) или пока не наберём max_items."""
    params = dict(params or {}, per_page=100)
    items, page = [], 1
    while True:
        batch = get(url, dict(params, page=page))
        items.extend(batch)
        if len(batch) < 100 or (max_items and len(items) >= max_items):
            return items
        page += 1


# ============================================================================
# 4. СБОРКА ОДНОЙ ЗАПИСИ prs.json
# ============================================================================

def ci_status(repo: str, sha: str) -> str:
    """Результат CI для последнего коммита PR (sha).
    check-runs — это проверки во вкладке Checks на GitHub (тесты, линтеры).
    Сводим их к одному слову: success / failure / mixed / unknown."""
    data = get(f"{API}/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
    runs = data.get("check_runs", [])
    if not runs:
        return "unknown"                              # проверок не было
    conclusions = {r.get("conclusion") for r in runs} # множество итогов
    if "failure" in conclusions or "timed_out" in conclusions:
        return "failure"                              # хоть одна упала
    if conclusions <= {"success", "skipped", "neutral"}:
        return "success"                              # все хорошие
    return "mixed"                                    # что-то ещё (отменены и т.п.)


def truncate_patch(patch: str):
    """Обрезает патч до MAX_PATCH_LINES строк.
    Возвращает (патч, был_ли_обрезан)."""
    lines = patch.splitlines()
    if len(lines) > MAX_PATCH_LINES:
        return "\n".join(lines[:MAX_PATCH_LINES]), True
    return patch, False


def clip(text: str) -> str:
    """Обрезает слишком длинный текст комментария."""
    text = (text or "").strip()
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + " …"


def build_record(repo: str, pr: dict) -> dict:
    """Из краткой информации о PR (элемент списка /pulls) собирает
    полную запись в формате команды. Делает 5 запросов на PR."""
    n = pr["number"]

    # Запрос 1: детали PR. В списке /pulls нет additions/deletions, тут есть.
    detail = get(f"{API}/repos/{repo}/pulls/{n}")
    # Запрос 2: изменённые файлы с полем patch (diff в виде текста).
    files = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/files")
    # Запрос 3: ревью — итоговые вердикты ("Approve", "Request changes") и общий текст.
    reviews = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/reviews")
    # Запрос 4: комментарии к конкретным строкам кода. Тут обычно основная критика.
    inline = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/comments")

    # ---- файлы: делим на полезные и шум ----
    clean_files, noise = [], []
    for f in files:
        if is_noise(f):
            noise.append(f["filename"])   # запоминаем, что выкинули (поле noise_removed)
            continue
        if f.get("patch"):
            patch, truncated = truncate_patch(f["patch"])
        else:
            # Обычный файл кода, но GitHub не прислал diff (файл слишком большой).
            # Не выкидываем его: оставляем с пустым патчем и truncated=true,
            # чтобы модель знала — файл менялся, но содержимого она не видит.
            patch, truncated = "", True
        clean_files.append({"path": f["filename"], "patch": patch, "truncated": truncated})

    # ---- ревью ----
    # Берём ревью людей (не ботов), у которых есть текст ИЛИ явный вердикт.
    # Пустые "COMMENTED" без текста бесполезны.
    review_items = [
        {"state": rv["state"], "body": clip(rv.get("body"))}
        for rv in reviews
        if not is_bot(rv.get("user") or {})
        and (rv.get("body") or rv["state"] in ("APPROVED", "CHANGES_REQUESTED"))
    ]
    # Комментарии к строкам кладём в тот же список reviews, чтобы не менять
    # формат файла. В начале текста пишем файл и строку: "[src/a.py:12] ..."
    for c in [c for c in inline if not is_bot(c.get("user") or {})][:MAX_INLINE_COMMENTS]:
        line = c.get("line") or c.get("original_line") or "?"
        review_items.append({
            "state": "COMMENTED",
            "body": f"[{c.get('path', '?')}:{line}] {clip(c.get('body'))}",
        })

    # ---- итоговая запись: ровно поля из согласованного формата ----
    return {
        "number": n,
        "title": pr["title"],
        "body": pr.get("body") or "",          # описание может быть пустым (None)
        "author": pr["user"]["login"],         # модели НЕ показывается, нужен участнику 4
        "merged_at": pr["merged_at"],
        "additions": detail["additions"],
        "deletions": detail["deletions"],
        "files": clean_files,
        "reviews": review_items,
        "ci": ci_status(repo, pr["head"]["sha"]),   # запрос 5
        "noise_removed": noise,
    }


# ============================================================================
# 5. ИСТОРИЯ АВТОРОВ (для уровней junior / middle / senior)
# ============================================================================

def author_history(repo: str, authors: list[str], before: str) -> dict:
    """Для каждого автора считает, сколько его PR было принято ДО начала периода.
    Участник 4 по этому числу определяет уровень: <10 junior, 10–50 middle, >50 senior.

    Используем поиск GitHub: он сразу возвращает total_count, поэтому
    нужен один запрос на автора. У поиска свой лимит — 30 запросов в минуту,
    поэтому между запросами пауза 2.1 с (кроме тех, что взяты из кэша)."""
    day = before[:10]   # "2026-05-14T10:22:00Z" -> "2026-05-14"
    result = {}
    for i, a in enumerate(sorted(authors), 1):
        q = f"repo:{repo} is:pr is:merged author:{a} merged:<{day}"
        params = {"q": q, "per_page": 1}
        # Проверяем заранее, есть ли ответ в кэше, чтобы не ждать зря.
        cached = (CACHE_DIR / (hashlib.sha1(
            (f"{API}/search/issues?" + json.dumps(params, sort_keys=True)).encode()
        ).hexdigest() + ".json")).exists()
        data = get(f"{API}/search/issues", params)
        result[a] = data.get("total_count", 0)
        print(f"  история [{i}/{len(authors)}] {a}: {result[a]} PR до {day}", file=sys.stderr)
        if not cached:
            time.sleep(2.1)
    return {"period_start": before, "merged_before": result}


# ============================================================================
# 6. ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    # ---- параметры командной строки ----
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name, например pydantic/pydantic")
    ap.add_argument("--limit", type=int, default=5, help="сколько принятых PR взять")
    ap.add_argument("--out", default="prs.json", help="куда сохранить результат")
    ap.add_argument("--all-prs", help="куда сохранить заголовки/описания всех закрытых PR")
    ap.add_argument("--all-pages", type=int, default=10,
                    help="сколько страниц по 100 закрытых PR брать для --all-prs")
    ap.add_argument("--history", help="куда сохранить число принятых PR каждого автора до периода")
    ap.add_argument("--keep-service", action="store_true",
                    help="не отбрасывать служебные PR (релизы, bump версий)")
    args = ap.parse_args()

    # ---- шаг 1: список закрытых PR, свежие первыми ----
    # Берём с запасом (в 3 раза больше лимита): часть закрыта без слияния,
    # часть от ботов, часть служебные — их отбросим.
    closed = get_all_pages(
        f"{API}/repos/{args.repo}/pulls",
        {"state": "closed", "sort": "updated", "direction": "desc"},
        max_items=max(args.limit * 3, 100),
    )

    # ---- шаг 2: фильтрация ----
    merged, skipped_service = [], []
    for p in closed:
        if not p.get("merged_at") or is_bot(p["user"]):
            continue                      # не принят или от бота
        if not args.keep_service and SERVICE_TITLE.search(p["title"]):
            skipped_service.append(f"#{p['number']} {p['title']}")
            continue                      # служебный
        merged.append(p)

    # Сортируем по дате слияния (новые первыми) и берём нужное количество.
    merged.sort(key=lambda p: p["merged_at"], reverse=True)
    merged = merged[: args.limit]
    if skipped_service:
        print(f"Пропущено служебных PR: {len(skipped_service)} (например: {skipped_service[0]})",
              file=sys.stderr)

    # ---- шаг 3: полная запись для каждого PR ----
    records = []
    for i, pr in enumerate(merged, 1):
        print(f"[{i}/{len(merged)}] PR #{pr['number']}: {pr['title'][:60]}", file=sys.stderr)
        records.append(build_record(args.repo, pr))

    # ---- шаг 4: сохраняем prs.json ----
    # ensure_ascii=False — русский текст пишется как есть, а не ру...
    # indent=2 — красивые отступы, файл удобно читать глазами.
    Path(args.out).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(records)} PR -> {args.out}", file=sys.stderr)

    # ---- шаг 5 (по желанию): все закрытые PR для поиска откатов ----
    if args.all_prs:
        all_closed = get_all_pages(
            f"{API}/repos/{args.repo}/pulls",
            {"state": "closed", "sort": "updated", "direction": "desc"},
            max_items=args.all_pages * 100,
        )
        # Только нужные поля, без патчей: участнику 4 нужны заголовки и описания,
        # чтобы найти "Revert ..." и "fix #123".
        slim = [
            {"number": p["number"], "title": p["title"], "body": p.get("body") or "",
             "author": p["user"]["login"], "merged_at": p.get("merged_at"),
             "closed_at": p["closed_at"]}
            for p in all_closed
        ]
        Path(args.all_prs).write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Готово: {len(slim)} закрытых PR -> {args.all_prs}", file=sys.stderr)

    # ---- шаг 6 (по желанию): история авторов ----
    if args.history and records:
        period_start = min(r["merged_at"] for r in records)   # дата самого старого PR в выборке
        authors = list({r["author"] for r in records})         # уникальные авторы
        hist = author_history(args.repo, authors, period_start)
        Path(args.history).write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Готово: история {len(hist['merged_before'])} авторов -> {args.history}", file=sys.stderr)


# Этот блок срабатывает, только когда файл запускают напрямую
# (python fetch_prs.py), а не импортируют из другого скрипта.
if __name__ == "__main__":
    main()