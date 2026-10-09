"""
Выгрузка PR из GitHub (участник 1):  GitHub API -> prs.json, all_prs.json, author_history.json

Запуск:
    python fetch_prs.py --repo owner/name --limit 5 --out prs_sample.json
    python fetch_prs.py --repo owner/name --limit 150 --min-age-days 30 \
        --out prs.json --all-prs all_prs.json --history author_history.json

Токен: переменная окружения GITHUB_TOKEN или файл token.txt рядом со скриптом.
Все сырые ответы API кэшируются в папке raw/ рядом со скриптом, поэтому
повторный запуск берёт данные с диска и в сеть не ходит. Чтобы заново получить
список PR (а сами PR оставить из кэша), добавьте --refresh-list.
Работает на Python 3.8+.

Что гарантирует выборка
    - Берутся именно последние N принятых PR (а не «N из недавно обновлённых»):
      список листается до тех пор, пока это не доказано, см. select_merged().
    - --min-age-days 30 оставляет только PR, принятые не позже чем за 30 дней до
      снимка данных. Это нужно валидации: у вчерашнего PR ещё не было времени на откат.
    - Ревью и комментарии, написанные ПОСЛЕ мержа, отбрасываются. Иначе модель
      прочитала бы «это сломало прод» и «угадала» бы риск задним числом.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("ОШИБКА: для выгрузки из GitHub нужна библиотека requests. Установите её командой: pip install requests")

HERE = Path(__file__).resolve().parent
# Для GitHub Enterprise и для тестов адрес API можно заменить переменной окружения.
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
CACHE_DIR = Path(os.environ.get("FETCH_CACHE_DIR", HERE / "raw"))
MAX_PATCH_LINES = 300
MAX_INLINE_COMMENTS = 30      # сколько комментариев к строкам кода брать на один PR
MAX_COMMENT_CHARS = 1000      # длинные комментарии обрезаются
NETWORK_RETRIES = 5

# ---------- что считать шумом ----------
# Шум = файлы, которые не пишет человек: lock-файлы, сборки, минифицированное, бинарное.
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

# Служебные PR (релизы, смена версии, обновление зависимостей) не оцениваются.
SERVICE_TITLE = re.compile(
    r"^\s*(prepare release|release v?\d|set version|bump |update dependenc|"
    r"chore\(deps\)|chore: release|\[pre-commit\.ci\])",
    re.IGNORECASE,
)


class FetchError(Exception):
    """Понятная ошибка для человека: что случилось и что с этим делать."""


def is_noise(f: dict) -> bool:
    path = f["filename"]
    lower = path.lower()
    name = path.rsplit("/", 1)[-1]
    if name in NOISE_FILES:
        return True
    if any(path.startswith(d) or f"/{d}" in path for d in NOISE_DIRS):
        return True
    if lower.endswith(NOISE_SUFFIXES) or lower.endswith(BINARY_SUFFIXES):
        return True
    return False


def is_bot(user: dict | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def login_of(user: dict | None) -> str:
    """У PR удалённого аккаунта GitHub иногда отдаёт user = null."""
    return (user or {}).get("login") or "ghost"


# ---------- токен ----------
def load_token():
    """GITHUB_TOKEN из окружения, иначе файл token.txt рядом со скриптом."""
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t, "переменная GITHUB_TOKEN"
    token_file = HERE / "token.txt"
    if token_file.exists():
        t = token_file.read_text(encoding="utf-8-sig").strip().strip('"').strip("'")
        if t:
            return t, "файл token.txt"
    return None, None


_session = None


def get_session():
    """Сессия создаётся при первом запросе, а не при импорте модуля."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        token, source = load_token()
        if token:
            _session.headers["Authorization"] = f"Bearer {token}"
            print(f"Токен найден ({source}), оканчивается на ...{token[-4:]}", file=sys.stderr)
        else:
            print("ВНИМАНИЕ: токен не найден (нет GITHUB_TOKEN и token.txt), лимит всего 60 запросов в час",
                  file=sys.stderr)
    return _session


# ---------- работа с API и кэш ----------
def cache_path(url: str, params: dict | None = None) -> Path:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def write_atomic(path: Path, text: str) -> None:
    """Сначала во временный файл, потом переименование: прерванный запуск не оставит полфайла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def get(url: str, params: dict | None = None, refresh: bool = False):
    """GET с кэшем на диске. Один и тот же запрос второй раз не уходит в сеть."""
    cache_file = cache_path(url, params)
    if cache_file.exists() and not refresh:
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except ValueError:
            pass  # файл кэша повреждён (например, оборвалась запись): запрашиваем заново

    problem = "не было ни одной попытки"
    for attempt in range(NETWORK_RETRIES):
        try:
            r = get_session().get(url, params=params, timeout=30)
        except requests.RequestException as e:  # нет сети, таймаут, обрыв
            problem = f"сетевая ошибка: {e}"
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code in (403, 429) and (
            r.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in r.headers
        ):
            if "Retry-After" in r.headers:
                wait = int(float(r.headers["Retry-After"])) + 1
            else:
                wait = int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time()) + 1
            print(f"Лимит исчерпан, жду {max(wait, 1)} с...", file=sys.stderr)
            problem = "лимит запросов"
            time.sleep(max(wait, 1))
            continue
        if r.status_code >= 500:
            problem = f"HTTP {r.status_code}"
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code == 401:
            raise FetchError("GitHub не принял токен (HTTP 401). Проверьте GITHUB_TOKEN или token.txt.")
        if r.status_code == 404:
            raise FetchError(f"GitHub ответил 404 на {url}. Проверьте имя репозитория (owner/name) "
                             "и что токен имеет к нему доступ.")
        if r.status_code >= 400:
            raise FetchError(f"GitHub ответил HTTP {r.status_code} на {url}: {r.text[:200]}")
        data = r.json()
        write_atomic(cache_file, json.dumps(data, ensure_ascii=False))
        return data
    raise FetchError(f"Не удалось получить {url} за {NETWORK_RETRIES} попыток ({problem}). "
                     "Уже скачанное лежит в кэше: запустите ещё раз.")


def get_all_pages(url: str, params: dict | None = None, max_items: int | None = None):
    """Пагинация: per_page=100 и страницы 1, 2, 3... пока не кончатся."""
    params = dict(params or {}, per_page=100)
    items, page = [], 1
    while True:
        batch = get(url, dict(params, page=page))
        items.extend(batch)
        if len(batch) < 100 or (max_items and len(items) >= max_items):
            return items
        page += 1


# ---------- какие PR брать ----------
def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_candidate(p: dict, keep_service: bool) -> bool:
    if not p.get("merged_at") or is_bot(p.get("user")):
        return False
    return keep_service or not SERVICE_TITLE.search(p.get("title") or "")


def select_merged(repo: str, limit: int, min_age_days: float = 0, max_pages: int = 30,
                  keep_service: bool = False, refresh: bool = False):
    """Последние `limit` принятых PR и весь просмотренный список закрытых PR.

    GitHub не умеет сортировать PR по дате мержа, только по дате обновления. Поэтому
    список листается от свежих обновлений к старым, пока не выполнится условие:
    самый старый из выбранных PR принят ПОЗЖЕ, чем обновлён самый старый PR в списке.
    Мерж сам обновляет PR, значит, всё, что принято после этой границы, уже в списке,
    и пропущенных среди выбранных нет. Заодно список покрывает весь период от первого
    выбранного PR до снимка, а это ровно то, что нужно для поиска откатов.

    Возвращает (выбранные PR, весь список, сведения о выборке).
    """
    url = f"{API}/repos/{repo}/pulls"
    params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100}
    listing, covered_since, snapshot, complete, page = {}, None, None, False, 1
    while True:
        batch = get(url, dict(params, page=page), refresh=refresh)
        for p in batch:
            listing.setdefault(p["number"], p)  # страницы могут сдвинуться между запросами: без дублей
        if batch:
            oldest = min(p["updated_at"] for p in batch)
            covered_since = oldest if covered_since is None else min(covered_since, oldest)
            if snapshot is None:
                snapshot = max(p["updated_at"] for p in batch)
        # Возраст PR считается от момента снимка, а не от «сейчас»: повторный запуск
        # из кэша через неделю выберет те же PR.
        cutoff = format_time(parse_time(snapshot) - timedelta(days=min_age_days)) if snapshot else ""
        candidates = sorted(
            (p for p in listing.values() if is_candidate(p, keep_service) and p["merged_at"] <= cutoff),
            key=lambda p: p["merged_at"], reverse=True,
        )
        chosen = candidates[:limit]
        if len(batch) < 100:
            complete = True  # дошли до начала истории репозитория
            break
        if len(chosen) == limit and chosen[-1]["merged_at"] > covered_since:
            complete = True
            break
        if page >= max_pages:
            break
        page += 1

    skipped_service = [f"#{p['number']} {p['title']}" for p in listing.values()
                       if p.get("merged_at") and not is_bot(p.get("user")) and not is_candidate(p, keep_service)]
    info = {
        "repo": repo,
        "snapshot": snapshot,
        "covered_since": covered_since,
        "pages": page,
        "closed_seen": len(listing),
        "complete": complete,
        "skipped_service": skipped_service,
    }
    return chosen, list(listing.values()), info


# ---------- сборка одной записи ----------
def ci_status(repo: str, sha: str) -> str:
    data = get(f"{API}/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
    runs = data.get("check_runs", [])
    if not runs:
        # Старые CI (Travis, Jenkins и т. п.) пишут не check runs, а «статусы коммита».
        combined = get(f"{API}/repos/{repo}/commits/{sha}/status")
        if not combined.get("statuses"):
            return "unknown"
        return {"success": "success", "failure": "failure", "error": "failure"}.get(combined.get("state"), "mixed")
    conclusions = {r.get("conclusion") for r in runs}
    if "failure" in conclusions or "timed_out" in conclusions:
        return "failure"
    if conclusions <= {"success", "skipped", "neutral"}:
        return "success"
    return "mixed"


def truncate_patch(patch: str):
    lines = patch.splitlines()
    if len(lines) > MAX_PATCH_LINES:
        return "\n".join(lines[:MAX_PATCH_LINES]), True
    return patch, False


def clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + " …"


def before_merge(stamp: str | None, merged_at: str) -> bool:
    """Написано до мержа? Без отметки времени считаем, что да."""
    return not stamp or stamp <= merged_at


def build_record(repo: str, pr: dict) -> dict:
    n = pr["number"]
    merged_at = pr["merged_at"]
    detail = get(f"{API}/repos/{repo}/pulls/{n}")  # тут есть additions/deletions
    files = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/files")
    reviews = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/reviews")
    inline = get_all_pages(f"{API}/repos/{repo}/pulls/{n}/comments")  # комментарии к строкам кода

    clean_files, noise = [], []
    for f in files:
        if is_noise(f):
            noise.append(f["filename"])
            continue
        if f.get("patch"):
            patch, truncated = truncate_patch(f["patch"])
        elif f.get("status") == "renamed" and not f.get("changes"):
            # Файл только переименован: показывать нечего, но и ничего не скрыто.
            patch, truncated = "", False
        else:
            # Обычный файл кода, но GitHub не прислал diff (файл слишком большой).
            # Файл оставляем, а truncated=true говорит модели, что содержимого она не видит.
            patch, truncated = "", True
        clean_files.append({"path": f["filename"], "patch": patch, "truncated": truncated})

    # Только то, что было написано до мержа: поздние комментарии выдают исход
    # («после этого PR упал прод») и обесценивают проверку оценки риска.
    review_items = [
        {"state": rv["state"], "body": clip(rv.get("body"))}
        for rv in reviews
        if not is_bot(rv.get("user"))
        and before_merge(rv.get("submitted_at"), merged_at)
        and (rv.get("body") or rv["state"] in ("APPROVED", "CHANGES_REQUESTED"))
    ]
    # Комментарии к конкретным строкам кладём в тот же список reviews (формат не меняется),
    # с пометкой файла и строки в начале текста.
    human_inline = [c for c in inline
                    if not is_bot(c.get("user")) and before_merge(c.get("created_at"), merged_at)]
    for c in human_inline[:MAX_INLINE_COMMENTS]:
        line = c.get("line") or c.get("original_line") or "?"
        review_items.append({
            "state": "COMMENTED",
            "body": f"[{c.get('path', '?')}:{line}] {clip(c.get('body'))}",
        })

    return {
        "number": n,
        "title": pr["title"],
        "body": pr.get("body") or "",
        "author": login_of(pr.get("user")),
        "merged_at": merged_at,
        "additions": detail["additions"],
        "deletions": detail["deletions"],
        "files": clean_files,
        "reviews": review_items,
        "ci": ci_status(repo, pr["head"]["sha"]),
        "noise_removed": noise,
    }


# ---------- история авторов (для уровней junior/middle/senior) ----------
def author_history(repo: str, authors: list[str], before: str) -> dict:
    """Сколько PR каждого автора было принято ДО начала периода. Один поисковый запрос на автора."""
    day = before[:10]
    result = {}
    for i, a in enumerate(sorted(authors), 1):
        params = {"q": f"repo:{repo} is:pr is:merged author:{a} merged:<{day}", "per_page": 1}
        cached = cache_path(f"{API}/search/issues", params).exists()
        try:
            data = get(f"{API}/search/issues", params)
        except FetchError as e:
            # Удалённый или скрытый аккаунт: поиск по нему GitHub не разрешает.
            print(f"  история [{i}/{len(authors)}] {a}: не получена ({e}); уровень будет считаться неизвестным",
                  file=sys.stderr)
            continue
        result[a] = data.get("total_count", 0)
        print(f"  история [{i}/{len(authors)}] {a}: {result[a]} PR до {day}", file=sys.stderr)
        if not cached:
            time.sleep(float(os.environ.get("FETCH_SEARCH_PAUSE", "2.1")))  # поиск GitHub разрешает 30 запросов в минуту
    return {"period_start": before, "merged_before": result}


def main():
    ap = argparse.ArgumentParser(description="Выгрузка принятых PR из GitHub в prs.json")
    ap.add_argument("--repo", required=True, help="owner/name, например pydantic/pydantic")
    ap.add_argument("--limit", type=int, default=5, help="сколько принятых PR взять")
    ap.add_argument("--min-age-days", type=float, default=0,
                    help="брать только PR, принятые не позже чем за столько дней до снимка "
                         "(для валидации ставьте 30: у свежих PR ещё не было времени на откат)")
    ap.add_argument("--out", default="prs.json")
    ap.add_argument("--all-prs", help="куда сохранить заголовки/описания всех закрытых PR за период")
    ap.add_argument("--all-pages", type=int, default=30,
                    help="предел: сколько страниц по 100 закрытых PR листать")
    ap.add_argument("--history", help="куда сохранить число принятых PR каждого автора до периода")
    ap.add_argument("--keep-service", action="store_true",
                    help="не отбрасывать служебные PR (релизы, bump версий)")
    ap.add_argument("--refresh-list", action="store_true",
                    help="заново запросить список PR, не брать его из кэша")
    args = ap.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        raise FetchError(f"--repo должен иметь вид owner/name, получено: {args.repo!r}")

    merged, listing, info = select_merged(
        args.repo, args.limit, args.min_age_days, args.all_pages, args.keep_service, args.refresh_list)
    if info["skipped_service"]:
        print(f"Пропущено служебных PR: {len(info['skipped_service'])} (например: {info['skipped_service'][0]})",
              file=sys.stderr)
    if not merged:
        raise FetchError(f"В {args.repo} не нашлось подходящих принятых PR "
                         f"(просмотрено закрытых: {info['closed_seen']}). Уменьшите --min-age-days.")
    if len(merged) < args.limit:
        print(f"ВНИМАНИЕ: найдено только {len(merged)} PR из {args.limit} запрошенных "
              f"(просмотрено закрытых: {info['closed_seen']}).", file=sys.stderr)
    if not info["complete"]:
        print(f"ВНИМАНИЕ: достигнут предел --all-pages {args.all_pages}, выборка может быть неполной: "
              "среди выбранных могут отсутствовать PR, принятые в тот же период. Увеличьте --all-pages.",
              file=sys.stderr)

    records, skipped = [], []
    for i, pr in enumerate(merged, 1):
        print(f"[{i}/{len(merged)}] PR #{pr['number']}: {pr['title'][:60]}", file=sys.stderr)
        try:
            records.append(build_record(args.repo, pr))
        except (FetchError, KeyError, TypeError) as e:
            # Один PR с неожиданным ответом API не должен ронять выгрузку остальных 149.
            skipped.append(pr["number"])
            print(f"  пропущен: {type(e).__name__}: {e}", file=sys.stderr)
    if not records:
        raise FetchError("Не удалось собрать ни одного PR, см. сообщения выше.")

    write_atomic(Path(args.out), json.dumps(records, ensure_ascii=False, indent=2))
    print(f"Готово: {len(records)} PR -> {args.out}"
          + (f" (пропущено из-за ошибок: {skipped})" if skipped else ""), file=sys.stderr)
    print(f"Период: {min(r['merged_at'] for r in records)[:10]} .. {max(r['merged_at'] for r in records)[:10]}, "
          f"снимок данных {info['snapshot'][:10]}", file=sys.stderr)

    if args.all_prs:
        slim = [
            {"number": p["number"], "title": p["title"], "body": p.get("body") or "",
             "author": login_of(p.get("user")), "is_bot": is_bot(p.get("user")),
             "created_at": p.get("created_at"), "updated_at": p.get("updated_at"),
             "merged_at": p.get("merged_at"), "closed_at": p.get("closed_at")}
            for p in sorted(listing, key=lambda p: p["number"], reverse=True)
        ]
        write_atomic(Path(args.all_prs), json.dumps(slim, ensure_ascii=False, indent=2))
        print(f"Готово: {len(slim)} закрытых PR -> {args.all_prs}", file=sys.stderr)

    if args.history:
        period_start = min(r["merged_at"] for r in records)
        hist = author_history(args.repo, list({r["author"] for r in records}), period_start)
        hist["repo"] = args.repo
        write_atomic(Path(args.history), json.dumps(hist, ensure_ascii=False, indent=2))
        print(f"Готово: история {len(hist['merged_before'])} авторов -> {args.history}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except FetchError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nПрервано. Скачанное сохранено в кэше, повторный запуск продолжит с того же места.",
              file=sys.stderr)
        sys.exit(130)
