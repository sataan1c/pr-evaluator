"""Статистика для валидации без внешних библиотек.

Всё рассчитано на маленькие выборки: откатов в репозитории мало, поэтому
везде, где можно, вместо одной цифры отдаётся интервал.
"""
from __future__ import annotations

import math
import random


def wilson(successes: int, total: int, z: float = 1.96):
    """95%-й интервал Уилсона для доли. Честнее «плюс-минус» на малых выборках."""
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """Односторонний точный тест Фишера для таблицы 2x2:

                    проблемный   чистый
        группа 1        a           b
        группа 2        c           d

    Возвращает вероятность увидеть в группе 1 столько же проблемных PR или больше,
    если на самом деле группы не различаются.
    """
    row1, col1, total = a + b, a + c, a + b + c + d
    if total == 0 or row1 == 0 or col1 == 0:
        return 1.0
    denom = math.comb(total, row1)
    upper = min(row1, col1)
    return min(1.0, sum(math.comb(col1, k) * math.comb(total - col1, row1 - k) for k in range(a, upper + 1)) / denom)


def auc(positives, negatives):
    """Вероятность того, что у случайного проблемного PR значение выше, чем у случайного чистого.

    0.5 = значение ничего не различает, 1.0 = различает идеально. Равные значения считаются за половину.
    """
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def auc_interval(positives, negatives, rounds: int = 2000, seed: int = 0):
    """95%-й интервал для AUC бутстрепом. Зерно фиксировано: повторный запуск даёт те же числа."""
    if not positives or not negatives:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        pos = [rng.choice(positives) for _ in positives]
        neg = [rng.choice(negatives) for _ in negatives]
        values.append(auc(pos, neg))
    values.sort()
    return values[int(0.025 * rounds)], values[min(rounds - 1, int(0.975 * rounds))]


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None
