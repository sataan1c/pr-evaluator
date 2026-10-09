# AI-оценщик вклада по PR

Система читает принятые pull request'ы репозитория, оценивает каждый по четырём
критериям (сложность, качество, риск, ясность) с объяснением и ссылками на код,
и считает для каждого разработчика множитель относительно нормы его уровня.

Пример построен на репозитории [pydantic/pydantic](https://github.com/pydantic/pydantic),
150 последних принятых PR.

## Быстрый запуск (готовые данные уже в репозитории)

Нужен Python 3.8 или новее.

```
git clone https://github.com/<OWNER>/<REPO>.git
cd <REPO>
pip install -r requirements.txt
python run_all.py
```

Дашборд откроется в браузере. Токены, ключи API и интернет для этого не нужны:
все результаты уже лежат в папке `data/`.

## Как устроен конвейер

```
GitHub API ──fetch_prs.py──▶ data/prs.json, all_prs.json, author_history.json
                              │
             score.py ◀───────┘  (нейросеть, автор от неё скрыт)
                │
                ▼
          data/scores.json ──metrics.py, outcomes.py──▶ data/metrics.json, outcomes.json
                                                          │
                                         app.py (Streamlit) ◀┘
```

| Файл | Что делает | Автор |
|---|---|---|
| `fetch_prs.py` | Выгрузка PR из GitHub, очистка от шума | Участник 1 |
| `score.py` | Оценка каждого PR нейросетью по рубрике | Участник 2 |
| `app.py` | Дашборд: разбор PR, профиль, команда | Участник 3 |
| `metrics.py`, `outcomes.py` | Множитель, поиск откатов, валидация | Участник 4 |
| `run_all.py` | Запускает всё по порядку | Общий |

## Пересчитать данные с нуля

```
python run_all.py --force
```

Для этого нужны:
- токен GitHub: файл `token.txt` в корне проекта или переменная `GITHUB_TOKEN`
  (Settings → Developer settings → Fine-grained tokens, доступ к публичным репозиториям);
- ключ API нейросети для `score.py` (см. комментарии в начале файла).

Без токена выгрузка тоже работает, но GitHub разрешает только 60 запросов в час.
`token.txt` и ключи в репозиторий не попадают: они перечислены в `.gitignore`.
