"""График для жюри (участник 4):  validation.json -> risk_chart.png

Запуск:
    python chart.py                       # светлый фон, 1600x900, под слайд 16:9
    python chart.py --dark                # для тёмного слайда
    python chart.py --strict              # по строгому правилу (откат или исправление с причинной ссылкой)

Что на графике
    Три столбца: доля проблемных PR (откат или срочное исправление) в группах балла
    риска 1–2, 3 и 4–5. Над столбцом доля, под ним «сколько из скольких», тонкий
    отрезок показывает 95%-й интервал. Интервал на графике обязателен: проблемных PR
    мало, и без него три столбца выглядят убедительнее, чем есть на самом деле.

    Заголовок пишется по выводу валидации. Если связь не доказана, график так и
    говорит. Если баллы поставила заглушка, поперёк графика стоит пометка.

Нужен matplotlib:  pip install matplotlib
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import DataError, die, read_json, setup_output, use_data_dir

THEMES = {
    #        фон        основной текст  второстепенный  сетка      столбцы
    "light": ("#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1", "#2a78d6"),
    "dark":  ("#1a1a19", "#ffffff", "#c3c2b7", "#383835", "#3987e5"),
}
TITLES = {
    "supported": "PR с высоким баллом риска чаще оказывались проблемными",
    "not_shown": "Связь балла риска с откатами на этой выборке не доказана",
    "too_few_problems": "Проблемных PR слишком мало, чтобы делать вывод",
}


def _column(ax, x, height, width, radius_px, color):
    """Столбец со скруглённым верхом и прямым основанием. Радиус задан в пикселях."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    origin = ax.transData.transform((0, 0))
    unit = ax.transData.transform((1, 1)) - origin          # пикселей на единицу по x и по y
    rx, ry = min(radius_px / unit[0], width / 2), min(radius_px / unit[1], height)
    left, right = x - width / 2, x + width / 2
    vertices = [(left, 0), (left, height - ry), (left, height), (left + rx, height),
                (right - rx, height), (right, height), (right, height - ry), (right, 0), (left, 0)]
    codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3,
             MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO, MplPath.CLOSEPOLY]
    ax.add_patch(PathPatch(MplPath(vertices, codes), facecolor=color, edgecolor="none", zorder=3))


def draw(report: dict, out, dark: bool = False, strict: bool = False) -> dict:
    """Рисует график и возвращает то, что на нём написано (для проверки и для слайда)."""
    risk = report.get("risk_strict" if strict else "risk")
    if not risk:
        raise DataError("в validation.json нет раздела о риске: сначала запустите outcomes.py и validate.py")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise DataError("для графика нужен matplotlib: pip install matplotlib")

    surface, ink, quiet, grid, series = THEMES["dark" if dark else "light"]
    groups = risk["groups"]
    top = max([g["ci95"][1] for g in groups if g["ci95"]] + [0.1])
    y_max = min(1.0, (int(top * 10) + 2) / 10)               # до ближайших 10% с запасом под подписи

    plt.rcParams["font.family"] = "DejaVu Sans"              # есть в любой установке matplotlib, знает кириллицу
    fig = plt.figure(figsize=(8, 4.5), dpi=200, facecolor=surface)
    ax = fig.add_axes([0.09, 0.25, 0.86, 0.47], facecolor=surface)
    ax.set_xlim(-0.6, len(groups) - 0.4)
    ax.set_ylim(0, y_max)
    ticks = [t / 10 for t in range(0, int(round(y_max * 10)) + 1, 2 if y_max > 0.5 else 1)]
    ax.set_yticks(ticks)
    ax.set_yticklabels([f"{t * 100:.0f}%" for t in ticks], color=quiet, fontsize=8)
    ax.set_xticks([])
    ax.yaxis.grid(True, color=grid, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    fig.canvas.draw()                                         # фиксирует масштаб осей для скругления

    for i, g in enumerate(groups):
        if g["n"]:
            if g["rate"] > 0:
                _column(ax, i, g["rate"], 0.3, 6, series)
            low, high = g["ci95"]
            ax.plot([i + 0.22, i + 0.22], [low, high], color=quiet, linewidth=1, solid_capstyle="butt", zorder=4)
            for edge in (low, high):
                ax.plot([i + 0.2, i + 0.24], [edge, edge], color=quiet, linewidth=1, zorder=4)
            ax.text(i, g["rate"] + y_max * 0.025, f"{g['rate'] * 100:.0f}%", ha="center", va="bottom",
                    color=ink, fontsize=13, fontweight="bold", zorder=5)
            count = f"{g['problems']} из {g['n']} PR"
        else:
            count = "нет PR"
        ax.text(i, -y_max * 0.08, f"Риск {g['risk']}", ha="center", va="top", color=ink, fontsize=10)
        ax.text(i, -y_max * 0.19, count, ha="center", va="top", color=quiet, fontsize=8.5)

    title = TITLES[risk["verdict"]]
    what = "откатили или чинили с явной ссылкой на причину" if strict else "откатили или срочно чинили после мержа"
    subtitle = (f"Доля PR, которые {what}, по баллу риска от модели.\n"
                f"В расчёте {risk['prs_used']} PR, проблемных среди них {risk['problems']}.")
    fig.text(0.09, 0.945, title, color=ink, fontsize=13.5, fontweight="bold", ha="left", va="top")
    fig.text(0.09, 0.87, subtitle, color=quiet, fontsize=8.5, ha="left", va="top", linespacing=1.5)
    footer = "Тонкий отрезок: 95%-й интервал доли."
    if risk["auc_risk"]:
        a, s = risk["auc_risk"], risk["auc_size"]
        ru = lambda x: f"{x:.2f}".replace(".", ",")          # десятичная запятая, как в отчёте
        footer += (f" AUC балла риска {ru(a['value'])} (интервал {ru(a['ci95'][0])}–{ru(a['ci95'][1])}), "
                   f"размера PR в строках {ru(s['value'])}.")
    if report.get("model"):
        footer += f"\nМодель: {report['model']}."
    fig.text(0.09, 0.03, footer, color=quiet, fontsize=7.5, ha="left", va="bottom", linespacing=1.5)
    if report.get("synthetic"):
        fig.text(0.52, 0.48, "ЗАГЛУШКА МОДЕЛИ\nэто не результат", color=quiet, alpha=0.28, fontsize=40, fontweight="bold",
                 ha="center", va="center", rotation=18, linespacing=1.1, zorder=10)

    fig.savefig(str(out), facecolor=surface)
    plt.close(fig)
    return {"title": title, "subtitle": subtitle, "footer": footer, "synthetic": bool(report.get("synthetic")),
            "bars": [{"risk": g["risk"], "share": g["rate"], "label": f"{g['problems']} из {g['n']}"} for g in groups]}


def main():
    setup_output()
    ap = argparse.ArgumentParser(description="График «доля проблемных PR по баллу риска» для слайда")
    ap.add_argument("--in", dest="source", default="validation.json")
    ap.add_argument("--out", default="risk_chart.png")
    ap.add_argument("--dark", action="store_true", help="тёмный фон")
    ap.add_argument("--strict", action="store_true", help="считать проблемными только откаты и исправления с причинной ссылкой")
    use_data_dir(ap, 'source', 'out')
    args = ap.parse_args()
    try:
        report = read_json(args.source, "отчёт о валидации")
        if not isinstance(report, dict):
            raise DataError(f"{args.source} должен быть JSON-объектом, который пишет validate.py")
        shown = draw(report, Path(args.out), args.dark, args.strict)
    except DataError as e:
        die(str(e))
    print(f"График -> {args.out}")
    print(f"  заголовок: {shown['title']}")
    print("  столбцы: " + "; ".join(f"риск {b['risk']}: {b['label']}" for b in shown["bars"]))
    if shown["synthetic"]:
        print("  ВНИМАНИЕ: баллы поставлены заглушкой, на графике стоит пометка. На слайд он не идёт.")


if __name__ == "__main__":
    main()
