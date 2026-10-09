/* Карта вклада: дашборд по файлам конвейера (prs, scores, metrics, outcomes, validation).
   Без библиотек и без сети. Данные приходят из data.js (его пишет make_data.py или отдаёт app.py)
   либо загружаются через кнопку «Данные». Тексты интерфейса лежат в i18n.js на трёх языках.
   Всё, что пришло из файлов (заголовки PR, логины, причины), вставляется только как текст. */
(function () {
  "use strict";

  const CRIT = ["complexity", "quality", "risk", "clarity"];
  const IN_MULTIPLIER = ["complexity", "quality", "clarity"];
  const LEVELS = ["junior", "middle", "senior"];
  const TYPES = ["feature", "bugfix", "performance", "refactor", "docs", "tests", "build"];   // change_types.json, classify.py
  const LANGS = ["az", "ru", "en"];
  const I18N = window.I18N || {};
  const API = window.DASHBOARD_API || null;      // есть, когда страницу отдаёт app.py: заметки пишутся в файл

  // ---------- язык ----------
  let L = I18N.ru || {};
  function pickLang() {
    let saved = null;
    try { saved = localStorage.getItem("pr-impact-lang"); } catch (e) { /* без хранилища язык берётся из браузера */ }
    if (LANGS.includes(saved)) return saved;
    for (const tag of navigator.languages || [navigator.language || ""]) {
      const short = String(tag).slice(0, 2).toLowerCase();
      if (LANGS.includes(short)) return short;
    }
    return "ru";
  }
  const template = (key) => (key in L ? L[key] : key in (I18N.ru || {}) ? I18N.ru[key] : key);
  /* Текст без разметки: для атрибутов, подписей графика и там, где нужна просто строка. */
  function tr(key, vars) {
    let text = template(key);
    if (Array.isArray(text)) text = text[0];
    return String(text).replace(/\{(\w+)\}/g, (m, name) => (vars && name in vars ? vars[name] : m)).replace(/\*\*|`/g, "");
  }
  /* Форма слова по числу: в русском три формы, в английском две, в азербайджанском одна. */
  function tn(key, n) {
    const forms = [].concat(template(key));
    if (forms.length === 1) return forms[0];
    if (forms.length === 2) return forms[n === 1 ? 0 : 1];
    const a = Math.abs(n) % 100, b = a % 10;
    return forms[a > 10 && a < 20 ? 2 : b === 1 ? 0 : b >= 2 && b <= 4 ? 1 : 2];
  }
  /* Текст с разметкой -> узлы. `код` и **выделение** разбираются только в самом шаблоне:
     подставленные значения (логины, заголовки, номера) всегда остаются обычным текстом или готовыми узлами. */
  function T(key, vars) {
    const parse = (text, bold) => text.split(bold ? /(\{\w+\}|`[^`]+`|\*\*[^*]+\*\*)/ : /(\{\w+\}|`[^`]+`)/).map((part) => {
      if (/^\{\w+\}$/.test(part)) {
        const name = part.slice(1, -1);
        return vars && name in vars ? vars[name] : part;
      }
      if (/^`[^`]+`$/.test(part)) return h("code", null, part.slice(1, -1));
      if (bold && /^\*\*[^*]+\*\*$/.test(part)) return h("strong", null, parse(part.slice(2, -2), false));
      return part;
    });
    let text = template(key);
    if (Array.isArray(text)) text = text[0];
    return parse(String(text), true);
  }

  // ---------- маленькие помощники ----------
  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";

  function fill(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      el.append(k.nodeType ? k : document.createTextNode(String(k)));
    }
    return el;
  }
  function make(ns, tag, attrs, kids) {
    const el = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    return fill(el, kids);
  }
  const h = (tag, attrs, ...kids) => make(null, tag, attrs, kids);
  const s = (tag, attrs, ...kids) => make(SVG_NS, tag, attrs, kids);

  const local = (text) => (L._decimal === "," ? text.replace(".", ",") : text);
  const num = (x, digits = 2) => (typeof x === "number" && isFinite(x) ? local(x.toFixed(digits)) : "—");
  const trim = (x) => (typeof x === "number" ? local(String(Math.round(x * 100) / 100)) : "—");
  /* Ровно половина округляется к чётному, как в отчёте validation.md: иначе 92,5% здесь и там разойдутся. */
  function pct(x) {
    if (typeof x !== "number") return "—";
    const v = Math.round(x * 1e6) / 1e4, half = Math.abs(v % 1) === 0.5;
    return (half ? 2 * Math.round(v / 2) : Math.round(v)) + "%";
  }
  const signed = (x, digits = 2) => (typeof x === "number" ? (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), digits) : "—");
  /* Дата собирается вручную из своих названий месяцев: не в каждом браузере есть азербайджанские,
     и тогда вместо «21 sen 2026» получается «2026 M09 21». День берётся по UTC, как в данных GitHub. */
  function date(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : `${d.getUTCDate()} ${(L._months || [])[d.getUTCMonth()] || d.getUTCMonth() + 1} ${d.getUTCFullYear()}`;
  }
  function median(values) {
    const v = values.slice().sort((a, b) => a - b);
    if (!v.length) return null;
    const m = Math.floor(v.length / 2);
    return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
  }
  const crit = (c) => tr("crit." + c);
  const link = (href, text, cls) => h("a", { href, class: cls }, text);
  const devHref = (login) => "#/dev/" + encodeURIComponent(login);
  const prHref = (n) => "#/pr/" + n;
  const joined = (items) => items.map((x, i) => [i ? ", " : "", x]);

  // ---------- подсказка при наведении и фокусе ----------
  const tipBox = $("tip");
  function tip(el, content) {
    const show = (ev) => {
      tipBox.replaceChildren();
      fill(tipBox, [content()]);
      tipBox.hidden = false;
      const r = el.getBoundingClientRect();
      const x = ev && ev.clientX ? ev.clientX : r.left + r.width / 2;
      const y = ev && ev.clientY ? ev.clientY : r.top;
      const w = tipBox.offsetWidth, hh = tipBox.offsetHeight;
      tipBox.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w / 2)) + "px";
      tipBox.style.top = (y - hh - 14 < 8 ? y + 18 : y - hh - 14) + "px";
    };
    const hide = () => { tipBox.hidden = true; };
    el.addEventListener("pointerenter", show);
    el.addEventListener("pointermove", show);
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", () => show(null));
    el.addEventListener("blur", hide);
    return el;
  }
  const tipRows = (title, rows) => h("div", null, h("div", { class: "tip-title" }, title),
    rows.map(([value, label]) => h("div", { class: "tip-row" }, h("strong", null, value), " ", label)));

  // ---------- данные ----------
  let D = null;                      // собранное состояние
  const raw = Object.assign({}, window.DASHBOARD_DATA || {});
  const notes = Object.assign({}, raw.notes || {});
  const sortState = {};
  const filters = { q: "", author: "", risk: "", type: "", problem: false, unstable: false };

  function build() {
    const prs = Array.isArray(raw.prs) ? raw.prs : [];
    const scores = Array.isArray(raw.scores) ? raw.scores : [];
    const prBy = new Map(prs.map((p) => [p.number, p]));
    const outBy = new Map((Array.isArray(raw.outcomes) ? raw.outcomes : []).map((o) => [o.number, o]));
    const perPr = (raw.run_stats && raw.run_stats.per_pr) || {};
    const typeBy = new Map(((raw.types && Array.isArray(raw.types.prs)) ? raw.types.prs : [])
      .filter((t) => t && TYPES.includes(t.type)).map((t) => [t.number, t]));
    const rows = [];
    for (const sc of scores) {
      const pr = prBy.get(sc.number);
      if (!pr || !sc.scores) continue;
      const values = {};
      for (const c of CRIT) values[c] = sc.scores[c] ? sc.scores[c].score : null;
      rows.push({
        n: sc.number, title: pr.title || "", author: pr.author || "ghost", merged: pr.merged_at || "",
        add: pr.additions || 0, del: pr.deletions || 0, lines: (pr.additions || 0) + (pr.deletions || 0), ci: pr.ci || "unknown",
        sc: values, unstable: !!sc.unstable, runs: sc.runs || 1, summary: sc.summary || "", detail: sc.scores,
        runScores: (perPr[String(sc.number)] || {}).run_scores || null, out: outBy.get(sc.number) || null, pr,
        type: (typeBy.get(sc.number) || {}).type || null, typeReason: (typeBy.get(sc.number) || {}).reason || "",
      });
    }
    rows.sort((a, b) => (a.merged < b.merged ? 1 : -1));
    // PR без оценок: экран «оценок пока нет» показывает, что известно и без модели.
    const pending = scores.length ? [] : prs.filter((p) => p && typeof p.number === "number").map((p) => ({
      n: p.number, title: p.title || "", author: p.author || "ghost", merged: p.merged_at || "",
      add: p.additions || 0, del: p.deletions || 0, lines: (p.additions || 0) + (p.deletions || 0), out: outBy.get(p.number) || null }));
    const rowBy = new Map(rows.map((r) => [r.n, r]));
    const byAuthor = new Map();
    for (const r of rows) (byAuthor.get(r.author) || byAuthor.set(r.author, []).get(r.author)).push(r);

    let devs, derived = false;
    if (Array.isArray(raw.metrics) && raw.metrics.length) {
      devs = raw.metrics.map((m) => Object.assign({}, m));
    } else {                           // metrics.json ещё нет: показываем медианы, без множителя
      derived = true;
      devs = [...byAuthor.keys()].sort((a, b) => a.localeCompare(b)).map((author) => {
        const items = byAuthor.get(author), medians = {};
        for (const c of CRIT) medians[c] = median(items.map((r) => r.sc[c]).filter((x) => x !== null));
        return { author, level: null, pr_count: items.length, medians, composite: null, norm: null, multiplier: null, flags: [] };
      });
    }
    const cfg = raw.config && raw.config.multiplier ? raw.config : null;
    const stats = raw.run_stats || null, val = raw.validation || null;
    const repo = raw.repo || (raw.history && raw.history.repo) || "";
    const model = (val && val.model) || (stats && stats.model) || "";
    D = {
      rows, rowBy, byAuthor, devs, devBy: new Map(devs.map((d) => [d.author, d])), derived, cfg, val, stats, model,
      repo: /^[\w.-]+\/[\w.-]+$/.test(repo) ? repo : "",
      hasOutcomes: outBy.size > 0, hasTypes: typeBy.size > 0, history: (raw.history && raw.history.merged_before) || {},
      synthetic: !!(val && val.synthetic) || String(model).startsWith("fake"),
      mult: cfg ? cfg.multiplier : { min: 0.9, max: 1.15, slope: 0.5 },
      ready: rows.length > 0, havePrs: prs.length > 0, haveScores: scores.length > 0,
      pending, waiting: rows.length === 0 && pending.length > 0,
    };
    $("repo-name").textContent = D.repo;
    $("stub-banner").hidden = !(D.ready && D.synthetic);
  }

  // ---------- общие элементы ----------
  const title = (r) => r.title || tr("untitled");
  function scoreCell(value) {
    if (value === null || value === undefined) return h("span", { class: "sc sc-0" }, "—");
    const step = Math.max(1, Math.min(5, Math.round(value)));
    return h("span", { class: "sc sc-" + step }, trim(value));
  }
  function meter(value, label) {
    const box = h("span", { class: "meter", role: "img", "aria-label": tr("meter.aria", { label, value: trim(value) }) });
    for (let i = 1; i <= 5; i++) {
      const part = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value - (i - 1)));
      box.append(h("i", { style: `--part:${part}` }));
    }
    return box;
  }
  /* Линейка множителя: шкала от минимума до максимума, отметка «норма уровня» на 1,00 и точка человека. */
  function ruler(value, big) {
    const { min, max } = D.mult, pos = (x) => ((Math.max(min, Math.min(max, x)) - min) / (max - min)) * 100;
    const none = value === null || value === undefined;
    const box = h("div", { class: "ruler" + (big ? " big" : ""), role: "img",
      "aria-label": none ? tr("ruler.none") : tr("ruler.aria", { value: num(value), one: num(1) }) });
    box.append(h("span", { class: "ruler-track" }), h("span", { class: "ruler-norm", style: `left:${pos(1)}%` }));
    if (!none) box.append(h("span", { class: "ruler-dot", style: `left:${pos(value)}%` }));
    if (big) {
      box.append(h("span", { class: "ruler-label", style: "left:0" }, num(min)),
        h("span", { class: "ruler-label mid", style: `left:${pos(1)}%` }, tr("ruler.norm", { one: num(1) })),
        h("span", { class: "ruler-label end", style: "left:100%" }, num(max)));
    }
    return box;
  }
  function multWords(m) {
    if (m === null || m === undefined) return tr("mult.none");
    return tr(m >= 1.03 ? "mult.above" : m <= 0.97 ? "mult.below" : "mult.meets");
  }
  const badge = (text, kind, hint) => h("span", { class: "badge" + (kind ? " " + kind : ""), title: hint }, text);
  const typeName = (t) => tr("type." + t);
  const typeBadge = (r) => (r.type ? badge(typeName(r.type), "type", r.typeReason || tr("type." + r.type + ".long")) : null);
  /* Разбивка PR по типу изменения: сколько и какая доля. Самый частый тип первым. */
  function typeCounts(items) {
    const counts = new Map();
    for (const r of items) if (r.type) counts.set(r.type, (counts.get(r.type) || 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || TYPES.indexOf(a[0]) - TYPES.indexOf(b[0]));
  }
  function typeMix(items) {
    const counts = typeCounts(items), total = counts.reduce((sum, [, n]) => sum + n, 0);
    if (!total) return hint(tr("dev.types.none"));
    return h("div", { class: "mix", role: "list" }, counts.map(([t, n]) => h("div", { class: "mix-row", role: "listitem" },
      h("span", { class: "mix-name", title: tr("type." + t + ".long") }, typeName(t)),
      h("span", { class: "mix-bar", "aria-hidden": "true" }, h("i", { style: `width:${Math.max(2, (100 * n) / total)}%` })),
      h("span", { class: "mix-num" }, tr("dev.types.count", { n, pct: pct(n / total) })))));
  }
  function outcomeBadge(r) {
    const o = r.out;
    if (!o) return null;
    if (o.reverted) return badge(tr("out.reverted"), "bad");
    if (o.fixed_by && o.fixed_by.length) return badge(tr("out.fixed"), "warn");
    if (!o.observable) return badge(tr("out.early"), "", tr("out.early.title"));
    return null;
  }
  const flagName = (f) => (("flag." + f) in L ? tr("flag." + f) : f);
  const flagBadges = (flags) => (flags || []).map((f) => badge(flagName(f), "", ("flag." + f + ".long") in L ? tr("flag." + f + ".long") : null));
  const githubPr = (n) => `https://github.com/${D.repo}/pull/${n}`;
  function prRef(n) {
    if (D.rowBy.has(n)) return link(prHref(n), "#" + n);
    if (D.repo) return h("a", { href: githubPr(n), target: "_blank", rel: "noopener" }, "#" + n);
    return "#" + n;
  }

  /* Таблица с сортировкой по клику на заголовок. cols: {key, label, value(row), cell(row), num, title} */
  function table(id, cols, rows, opts) {
    const state = sortState[id] || (sortState[id] = { key: opts.sort, dir: opts.dir || 1 });
    const col = cols.find((c) => c.key === state.key) || cols[0];
    const sorted = rows.slice().sort((a, b) => {
      const x = col.value(a), y = col.value(b);
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === "string" ? x.localeCompare(y, L._locale) : x - y) * state.dir;
    });
    const head = h("tr", null, cols.map((c) => {
      const active = c.key === state.key;
      const btn = h("button", { type: "button", class: "th-btn", title: c.title, onclick: () => {
        sortState[id] = { key: c.key, dir: active ? -state.dir : (c.num ? -1 : 1) };
        render(true);
      } }, c.label, h("span", { class: "sort", "aria-hidden": "true" }, active ? (state.dir > 0 ? "↑" : "↓") : ""));
      return h("th", { scope: "col", class: c.num ? "r" : null, "aria-sort": active ? (state.dir > 0 ? "ascending" : "descending") : null }, btn);
    }));
    const body = sorted.map((row) => {
      const tr_ = h("tr", { class: opts.href ? "go" : null }, cols.map((c) => h("td", { class: c.num ? "r" : null }, c.cell(row))));
      if (opts.href) tr_.addEventListener("click", (ev) => { if (!ev.target.closest("a,button")) location.hash = opts.href(row); });
      return tr_;
    });
    return h("div", { class: "table-wrap" }, h("table", { class: "grid" }, h("caption", { class: "vh" }, opts.caption), h("thead", null, head), h("tbody", null, body)));
  }
  const crumbs = (...items) => h("nav", { class: "crumbs", "aria-label": tr("crumbs.aria") }, items.map((it, i) => [i ? h("span", { "aria-hidden": "true" }, "/") : null, it]));
  const section = (heading, ...kids) => h("section", { class: "block" }, heading ? h("h2", null, heading) : null, kids);
  const hint = (...kids) => h("p", { class: "hint" }, kids);
  const verdict = (risk) => h("p", { class: "verdict" }, h("strong", null, tr("verdict." + risk.verdict) + ". "), tr("verdict." + risk.verdict + ".text"));
  const plainHead = (keys) => h("thead", null, h("tr", null, keys.map((k, i) => h("th", { scope: "col", class: i ? "r" : null }, tr(k)))));

  // ---------- графики ----------
  /* Доля проблемных PR по группам балла риска, с 95%-м интервалом. Один ряд, один цвет. */
  function riskChart(risk, compact) {
    const W = 520, H = compact ? 230 : 280, Lm = 44, R = 16, Tm = 26, B = 58;
    const groups = risk.groups, top = Math.max(0.1, ...groups.map((g) => (g.ci95 ? g.ci95[1] : 0)));
    const yMax = Math.min(1, (Math.floor(top * 10) + 2) / 10), step = yMax > 0.5 ? 0.2 : 0.1;
    const y = (v) => Tm + (H - Tm - B) * (1 - v / yMax), band = (W - Lm - R) / groups.length;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
      "aria-label": tr("chart.aria", { list: groups.map((g) => tr("chart.aria.item", { risk: g.risk, k: g.problems, n: g.n })).join("; ") }) });
    for (let v = 0; v <= yMax + 1e-9; v += step) {
      svg.append(s("line", { x1: Lm, x2: W - R, y1: y(v), y2: y(v), class: v === 0 ? "axis" : "gridline" }),
        s("text", { x: Lm - 8, y: y(v) + 4, class: "tick", "text-anchor": "end" }, Math.round(v * 100) + "%"));
    }
    groups.forEach((g, i) => {
      const cx = Lm + band * (i + 0.5), w = 24, grp = s("g", { class: "col", tabindex: "0" });
      if (g.n && g.rate > 0) {
        const y0 = y(0), y1 = y(g.rate), r = Math.min(4, (y0 - y1) / 2);
        grp.append(s("path", { class: "bar", d: `M${cx - w / 2} ${y0}V${y1 + r}Q${cx - w / 2} ${y1} ${cx - w / 2 + r} ${y1}H${cx + w / 2 - r}Q${cx + w / 2} ${y1} ${cx + w / 2} ${y1 + r}V${y0}Z` }));
      }
      if (g.n && g.ci95) {
        const x = cx + 26;
        grp.append(s("path", { class: "whisker", d: `M${x} ${y(g.ci95[0])}V${y(g.ci95[1])}M${x - 4} ${y(g.ci95[0])}h8M${x - 4} ${y(g.ci95[1])}h8` }));
      }
      grp.append(s("text", { x: cx, y: (g.n ? y(g.rate) : y(0)) - 8, class: "value", "text-anchor": "middle" }, g.n ? pct(g.rate) : ""),
        s("text", { x: cx, y: H - B + 22, class: "cat", "text-anchor": "middle" }, tr("chart.cat", { risk: g.risk })),
        s("text", { x: cx, y: H - B + 40, class: "tick", "text-anchor": "middle" }, g.n ? tr("chart.count", { k: g.problems, n: g.n }) : tr("chart.none")),
        s("rect", { x: cx - band / 2, y: Tm - 10, width: band, height: H - Tm - B + 10, class: "hit" }));
      tip(grp, () => tipRows(tr("tip.risk", { risk: g.risk }), g.n ? [[pct(g.rate), tr("tip.problem")], [tr("chart.count", { k: g.problems, n: g.n }), tr("tip.reverted")],
        [`${pct(g.ci95[0])}–${pct(g.ci95[1])}`, tr("tip.ci")]] : [["0", tr("tip.nogroup")]]));
      svg.append(grp);
    });
    return svg;
  }
  function riskTable(risk) {
    return h("div", { class: "table-wrap" }, h("table", { class: "grid plain" }, plainHead(["rt.risk", "rt.prs", "rt.problems", "rt.share", "rt.ci"]),
      h("tbody", null, risk.groups.map((g) => h("tr", null, h("td", null, g.risk), h("td", { class: "r" }, g.n), h("td", { class: "r" }, g.problems),
        h("td", { class: "r" }, pct(g.rate)), h("td", { class: "r" }, g.ci95 ? `${pct(g.ci95[0])}–${pct(g.ci95[1])}` : "—"))))));
  }
  /* Мини-гистограмма баллов 1–5 для одного критерия. */
  function histogram(values, label) {
    const counts = [1, 2, 3, 4, 5].map((v) => values.filter((x) => x === v).length), top = Math.max(1, ...counts);
    const box = h("span", { class: "histo", role: "img", "aria-label": `${label}: ` + counts.map((c, i) => tr("histo.item", { v: i + 1, c })).join(", ") });
    counts.forEach((c, i) => {
      const bar = h("i", { style: `--h:${c / top}`, tabindex: "0", class: c ? null : "zero" });
      tip(bar, () => tipRows(label, [[c, tr("histo.tip", { v: i + 1 })]]));
      box.append(bar);
    });
    return h("span", { class: "histo-wrap" }, box, h("span", { class: "histo-axis", "aria-hidden": "true" }, h("b", null, "1"), h("b", null, "5")));
  }
  /* Как модель распределяет баллы: критерии по строкам, баллы по столбцам, насыщенность = доля строки. */
  function heatmap(rows) {
    const box = h("div", { class: "heat", role: "table", "aria-label": tr("heat.aria") });
    box.append(h("div", { class: "heat-row head", role: "row" }, h("span", { role: "columnheader" }, ""),
      [1, 2, 3, 4, 5].map((v) => h("span", { role: "columnheader" }, tr("heat.col", { v })))));
    for (const c of CRIT) {
      const values = rows.map((r) => r.sc[c]).filter((x) => x !== null);
      const row = h("div", { class: "heat-row", role: "row" }, h("span", { role: "rowheader" }, crit(c)));
      for (let v = 1; v <= 5; v++) {
        const count = values.filter((x) => x === v).length, share = values.length ? count / values.length : 0;
        const level = count === 0 ? 0 : Math.min(5, 1 + Math.floor(share * 8));
        const cell = h("span", { class: "heat-cell h" + level, role: "cell", tabindex: "0" }, count || "");
        tip(cell, () => tipRows(tr("heat.tip", { crit: crit(c), v }), [[count, "PR"], [pct(share), tr("heat.share")]]));
        row.append(cell);
      }
      box.append(row);
    }
    return box;
  }

  // ---------- экран: команда ----------
  function viewTeam() {
    const rows = D.rows, seen = rows.filter((r) => r.out && r.out.observable), problems = seen.filter((r) => r.out.problem);
    const dates = rows.map((r) => r.merged).filter(Boolean).sort(), unstable = rows.filter((r) => r.unstable).length;
    const facts = [
      [rows.length, tr("fact.scored"), dates.length ? `${date(dates[0])} — ${date(dates[dates.length - 1])}` : ""],
      [D.devs.length, tn("fact.devs", D.devs.length), D.derived ? tr("fact.devs.none") : tr("fact.devs.with", { n: D.devs.filter((d) => d.multiplier !== null).length })],
      D.hasOutcomes ? [problems.length, tn("fact.problem", problems.length), tr("fact.problem.note", { n: seen.length })] : ["—", tn("fact.problem", 5), tr("fact.problem.nofile")],
      [unstable, tn("fact.unstable", unstable), tr("fact.unstable.note")],
    ];
    const strip = h("div", { class: "facts" }, facts.map(([value, label, note]) =>
      h("div", { class: "fact" }, h("div", { class: "fact-value" }, value), h("div", { class: "fact-label" }, label), h("div", { class: "fact-note" }, note))));

    const cols = [
      { key: "author", label: tr("col.dev"), value: (d) => d.author.toLowerCase(), cell: (d) => link(devHref(d.author), d.author, "strong") },
      { key: "level", label: tr("col.level"), value: (d) => LEVELS.indexOf(d.level), cell: (d) => (d.level ? tr("level." + d.level) : "—") },
      { key: "prs", label: tr("col.prs"), num: true, value: (d) => d.pr_count, cell: (d) => d.pr_count },
      ...IN_MULTIPLIER.map((c) => ({ key: c, label: crit(c), num: true, title: tr("col.median", { hint: tr("hint." + c) }),
        value: (d) => d.medians[c], cell: (d) => scoreCell(d.medians[c]) })),
      { key: "composite", label: tr("col.composite"), num: true, title: tr("col.composite.title"), value: (d) => d.composite,
        cell: (d) => (d.composite === null ? "—" : h("span", null, h("strong", null, num(d.composite)), h("span", { class: "dim" }, " " + tr("composite.at", { norm: num(d.norm) })))) },
      { key: "mult", label: tr("col.mult"), num: true, value: (d) => d.multiplier,
        cell: (d) => h("span", { class: "mult-cell" }, ruler(d.multiplier), h("strong", { class: "mult-num" }, d.multiplier === null ? "—" : num(d.multiplier))) },
      D.hasTypes ? { key: "type", label: tr("col.type"), title: tr("col.type.title"),
        value: (d) => { const top = typeCounts(D.byAuthor.get(d.author) || [])[0]; return top ? TYPES.indexOf(top[0]) : null; },
        cell: (d) => { const items = D.byAuthor.get(d.author) || [], top = typeCounts(items)[0];
          return top ? h("span", null, badge(typeName(top[0]), "type"), h("span", { class: "dim" }, pct(top[1] / items.filter((r) => r.type).length))) : "—"; } } : null,
      { key: "flags", label: tr("col.flags"), value: (d) => (d.flags || []).length, cell: (d) => flagBadges(d.flags) },
    ].filter(Boolean);
    const attention = rows.filter((r) => (r.out && r.out.problem) || r.sc.risk >= 4).slice(0, 7);
    const risk = D.val && D.val.risk;
    return [
      h("div", { class: "page-head" }, h("h1", null, tr("team.title")),
        h("p", { class: "lead" }, D.repo ? tr("team.repo", { repo: D.repo }) : "", D.model ? tr("team.model", { model: D.model }) : "", tr("team.lead"))),
      strip,
      section(tr("team.devs"),
        D.derived ? hint(T("team.nometrics")) : null,
        table("team", cols, D.devs, { sort: "author", href: (d) => devHref(d.author), caption: tr("team.caption") }),
        h("p", { class: "legend" }, h("span", { class: "legend-ruler" }, ruler(1.06)), tr("team.legend", { min: num(D.mult.min), max: num(D.mult.max), one: num(1) }))),
      h("div", { class: "split" },
        section(tr("team.trust"), risk ? [verdict(risk), riskChart(risk, true), h("p", null, link("#/check", tr("team.trust.link")))] : hint(T("team.trust.none"))),
        section(tr("team.attention"),
          attention.length ? h("ul", { class: "pr-list" }, attention.map((r) => h("li", null,
            h("a", { href: prHref(r.n), class: "pr-line" }, h("span", { class: "pr-n" }, "#" + r.n), h("span", { class: "pr-t" }, title(r))),
            h("span", { class: "pr-meta" }, link(devHref(r.author), r.author), h("span", null, tr("risk.inline") + " ", scoreCell(r.sc.risk)), outcomeBadge(r)))))
            : hint(tr("team.attention.none")))),
    ];
  }

  // ---------- экран: разработчик ----------
  function viewDev(login) {
    const d = D.devBy.get(login), items = D.byAuthor.get(login) || [];
    if (!d) return notFound(tr("dev.notfound", { login }), "#/team", tr("dev.back"));
    const count = D.history[login];
    const levelNote = d.level_source === "override" ? tr("dev.level.override")
      : d.level_source === "history" && typeof count === "number" ? tr("dev.level.history", { n: count })
        : d.level_source === "unknown" ? tr("dev.level.unknown") : "";
    const head = h("div", { class: "page-head" }, crumbs(link("#/team", tr("team.title")), login), h("h1", null, login),
      h("p", { class: "lead" }, [d.level ? tr("level." + d.level) : "", tr("dev.prs", { n: d.pr_count }), levelNote].filter(Boolean).join(", "), "."));

    const hero = h("div", { class: "hero" },
      h("div", { class: "hero-figure" }, h("div", { class: "hero-num" }, d.multiplier === null ? "—" : num(d.multiplier)), h("div", { class: "hero-word" }, multWords(d.multiplier))),
      h("div", { class: "hero-scale" }, ruler(d.multiplier, true)));

    let how = null;
    if (d.composite !== null && d.composite !== undefined) {
      const weights = D.cfg && D.cfg.weights && D.cfg.weights[d.level];
      const terms = weights ? IN_MULTIPLIER.map((c, i) => [i ? h("span", { class: "op" }, "+") : null,
        h("span", { class: "term" }, h("b", null, `${trim(d.medians[c])} × ${trim(weights[c])}`), h("small", null, crit(c).toLowerCase()))]) : null;
      how = section(tr("dev.how"),
        h("div", { class: "formula" }, terms, terms ? h("span", { class: "op" }, "=") : null,
          h("span", { class: "term total" }, h("b", null, num(d.composite)), h("small", null, tr("dev.composite"))),
          h("span", { class: "op" }, tr("dev.at")),
          h("span", { class: "term" }, h("b", null, num(d.norm)), h("small", null, tr(d.norm_source === "calibrated" ? "dev.norm.cal" : "dev.norm.cfg")))),
        h("p", { class: "note" }, d.multiplier === null ? tr("dev.how.none")
          : tr(d.composite >= d.norm ? "dev.how.above" : "dev.how.below", { pct: pct(Math.abs(d.composite - d.norm) / d.norm) })
            + tr("dev.how.rule", { one: num(1), slope: trim(D.mult.slope), min: num(D.mult.min), max: num(D.mult.max) }), tr("dev.how.risk")));
    }
    const flags = (d.flags || []).length ? section(tr("dev.flags"), h("ul", { class: "flag-list" },
      d.flags.map((f) => h("li", null, h("strong", null, flagName(f) + ". "), ("flag." + f + ".long") in L ? T("flag." + f + ".long") : "")))) : null;

    const profile = section(tr("dev.profile"), h("div", { class: "profile" }, CRIT.map((c) => h("div", { class: "profile-row" },
      h("div", { class: "profile-name" }, h("b", null, crit(c)), h("small", null, tr(c === "risk" ? "dev.norisk" : "dev.median"))),
      h("div", { class: "profile-score" }, h("span", { class: "big-score" }, trim(d.medians[c])), meter(d.medians[c], crit(c))),
      histogram(items.map((r) => r.sc[c]), crit(c))))),
      h("p", { class: "note" }, tr("dev.profile.note")));

    const types = D.hasTypes ? section(tr("dev.types"), typeMix(items), h("p", { class: "note" }, tr("dev.types.note"))) : null;
    return [head, hero, how, flags, types, profile, section(tr("prs.title"), prTable("dev-prs", items, false))];
  }

  // ---------- таблица PR и её фильтры ----------
  function prTable(id, rows, withAuthor) {
    const cols = [
      { key: "n", label: tr("prt.pr"), value: (r) => r.n, cell: (r) => h("span", { class: "dim" }, "#" + r.n) },
      { key: "title", label: tr("prt.title"), value: (r) => r.title.toLowerCase(),
        cell: (r) => h("span", { class: "title-cell" }, link(prHref(r.n), title(r), "strong"),
          r.type || r.unstable ? h("span", { class: "title-tags" }, typeBadge(r), r.unstable ? badge(tr("prt.unstable"), "", tr("prt.unstable.title")) : null) : null) },
      withAuthor ? { key: "author", label: tr("prt.author"), value: (r) => r.author.toLowerCase(), cell: (r) => link(devHref(r.author), r.author) } : null,
      { key: "merged", label: tr("prt.merged"), value: (r) => r.merged, cell: (r) => h("span", { class: "nowrap" }, date(r.merged)) },
      { key: "lines", label: tr("prt.lines"), num: true, value: (r) => r.lines, cell: (r) => h("span", { class: "nowrap" }, `+${r.add} −${r.del}`) },
      ...CRIT.map((c) => ({ key: c, label: crit(c), num: true, title: tr("hint." + c), value: (r) => r.sc[c], cell: (r) => scoreCell(r.sc[c]) })),
      D.hasOutcomes ? { key: "out", label: tr("prt.out"), value: (r) => (r.out ? (r.out.reverted ? 3 : r.out.problem ? 2 : r.out.observable ? 0 : 1) : 0), cell: outcomeBadge } : null,
    ].filter(Boolean);
    if (!rows.length) return hint(tr("prt.none"));
    return table(id, cols, rows, { sort: "merged", dir: -1, href: (r) => prHref(r.n), caption: tr("prt.caption") });
  }
  function viewPrs() {
    const authors = [...D.byAuthor.keys()].sort((a, b) => a.localeCompare(b));
    const apply = () => D.rows.filter((r) => (!filters.q || (r.title + " #" + r.n).toLowerCase().includes(filters.q.toLowerCase()))
      && (!filters.author || r.author === filters.author) && (!filters.risk || r.sc.risk >= +filters.risk)
      && (!filters.type || r.type === filters.type)
      && (!filters.problem || (r.out && r.out.problem)) && (!filters.unstable || r.unstable));
    const out = h("div", null), count = h("p", { class: "count", "aria-live": "polite" });
    const refresh = () => {
      const rows = apply();
      count.textContent = tr("prs.count", { n: rows.length, total: D.rows.length });
      out.replaceChildren(prTable("prs", rows, true));
    };
    const check = (key, label) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: filters[key], onchange: (e) => { filters[key] = e.target.checked; refresh(); } }), label);
    const author = h("select", { "aria-label": tr("f.author.aria"), onchange: (e) => { filters.author = e.target.value; refresh(); } },
      h("option", { value: "" }, tr("f.authors")), authors.map((a) => h("option", { value: a, selected: a === filters.author }, a)));
    const risk = h("select", { "aria-label": tr("f.risk.aria"), onchange: (e) => { filters.risk = e.target.value; refresh(); } },
      [["", tr("f.risk.any")], ["3", tr("f.risk.from", { v: 3 })], ["4", tr("f.risk.from", { v: 4 })], ["5", tr("f.risk.5")]].map(([v, text]) => h("option", { value: v, selected: v === filters.risk }, text)));
    const typeSel = D.hasTypes ? h("select", { "aria-label": tr("f.type.aria"), onchange: (e) => { filters.type = e.target.value; refresh(); } },
      h("option", { value: "" }, tr("f.types")), TYPES.map((t) => h("option", { value: t, selected: t === filters.type }, typeName(t)))) : null;
    const bar = h("div", { class: "filters" },
      h("input", { type: "search", placeholder: tr("f.search"), "aria-label": tr("f.search.aria"), value: filters.q, oninput: (e) => { filters.q = e.target.value; refresh(); } }),
      author, typeSel, risk, D.hasOutcomes ? check("problem", tr("f.problem")) : null, check("unstable", tr("f.unstable")), count);
    refresh();
    return [h("div", { class: "page-head" }, h("h1", null, tr("prs.title")), h("p", { class: "lead" }, tr("prs.lead"))), bar, out];
  }

  // ---------- экран: разбор PR ----------
  function snippet(patch, lines) {
    if (!patch) return null;
    const all = [];
    let n = 0;
    for (const line of patch.split("\n")) {
      const head = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (head) { n = +head[1]; continue; }
      if (line[0] === "\\") continue;
      if (line[0] === "-") all.push({ t: "-", num: "", at: n, text: line.slice(1) });
      else { all.push({ t: line[0] === "+" ? "+" : " ", num: n, at: n, text: line.slice(1) }); n++; }
    }
    const m = /^(\d+)(?:-(\d+))?$/.exec(lines || "");
    if (!m) return null;
    const a = +m[1], b = +(m[2] || m[1]);
    // Номера в ссылке относятся к новой версии файла. Удалённые строки показываются рядом для
    // понимания, но не подсвечиваются, а длинная серия удалений сворачивается.
    const near = all.filter((l) => l.at >= a - 2 && l.at <= b + 2), picked = [];
    for (let i = 0; i < near.length; i++) {
      let end = i;
      while (end < near.length && near[end].t === "-") end++;
      if (end - i > 4) {
        picked.push(near[i], near[i + 1], near[i + 2], { t: "…", num: "", text: tr("snip.more", { n: end - i - 3 }) });
        i = end - 1;
      } else picked.push(near[i]);
    }
    if (!picked.some((l) => l.num !== "")) return null;
    return h("pre", { class: "code" }, picked.slice(0, 40).map((l) => h("span", {
      class: "ln" + (l.num !== "" && l.num >= a && l.num <= b ? " hit" : "") + (l.t === "+" ? " add" : l.t === "-" ? " del" : l.t === "…" ? " gap" : "") },
      h("i", null, l.num), h("b", null, l.t === "-" ? "−" : l.t === "…" ? "" : l.t), h("span", null, l.text || " "))));
  }
  /* Поле «Контекст от автора». С app.py заметка пишется в notes.json рядом с данными, без него остаётся в браузере. */
  function noteField(r) {
    const key = `pr-impact-note:${D.repo}:${r.n}`, status = h("span", { class: "note", "aria-live": "polite" });
    let saved = "", timer = null;
    if (API) saved = notes[String(r.n)] || "";
    else try { saved = localStorage.getItem(key) || ""; } catch (e) { /* хранилище недоступно: поле просто не запоминает */ }
    const send = (text) => fetch("/api/note", { method: "POST", headers: { "Content-Type": "application/json", "X-Dashboard-Token": API.token },
      body: JSON.stringify({ number: r.n, text }) })
      // 403: сервер перезапускали, у страницы старый ключ. Обычное «не удалось» тут не подсказывает, что делать.
      .then((res) => { status.textContent = tr(res.ok ? "note.saved.file" : res.status === 403 ? "note.stale" : "note.fail"); if (res.ok) notes[String(r.n)] = text; })
      .catch(() => { status.textContent = tr("note.fail"); });
    const box = h("textarea", { rows: "4", maxlength: "4000", "aria-label": tr("note.aria"), placeholder: tr("note.placeholder"), oninput: (e) => {
      const text = e.target.value;
      if (API) {
        status.textContent = tr("note.saving");
        clearTimeout(timer);
        timer = setTimeout(() => send(text), 500);
      } else {
        try { localStorage.setItem(key, text); status.textContent = tr("note.saved.browser"); } catch (err) { status.textContent = tr("note.fail"); }
      }
    } });
    box.value = saved;
    return [box, status];
  }
  function viewPr(n) {
    const r = D.rowBy.get(n);
    if (!r) return notFound(tr("pr.notfound", { n }), "#/prs", tr("pr.back"));
    const pr = r.pr, o = r.out, files = pr.files || [], fileBy = new Map(files.map((f) => [f.path, f]));
    const gh = D.repo ? h("a", { href: githubPr(r.n), target: "_blank", rel: "noopener" }, tr("pr.github")) : null;
    const head = h("div", { class: "page-head" }, crumbs(link("#/prs", tr("prs.title")), "#" + r.n), h("h1", null, title(r)),
      h("p", { class: "meta" }, h("span", null, T("pr.author", { author: link(devHref(r.author), r.author) })), h("span", null, tr("pr.merged", { date: date(r.merged) })),
        h("span", { class: "nowrap" }, tr("pr.lines", { a: r.add, d: r.del })), typeBadge(r), badge(("ci." + r.ci) in L ? tr("ci." + r.ci) : r.ci, r.ci === "failure" ? "bad" : ""), gh),
      r.summary ? h("p", { class: "lead" }, r.summary) : null,
      r.type ? h("p", { class: "type-note" }, h("strong", null, tr("pr.type", { type: typeName(r.type) })), r.typeReason ? " " + r.typeReason : "") : null);

    const notices = [];
    if (o && o.reverted) notices.push(h("div", { class: "notice bad" }, T("pr.reverted", { list: joined(o.reverted_by.map(prRef)) })));
    else if (o && o.fixed_by && o.fixed_by.length) notices.push(h("div", { class: "notice warn" },
      T(o.fixed_by_explicit && o.fixed_by_explicit.length ? "pr.fixed.explicit" : "pr.fixed", { list: joined(o.fixed_by.map(prRef)) })));
    else if (o && !o.observable) notices.push(h("div", { class: "notice" }, tr("pr.early", { days: trim(o.observed_days) })));
    else if (o) notices.push(h("div", { class: "notice ok" }, tr("pr.clean")));
    if (r.unstable) notices.push(h("div", { class: "notice" }, T("pr.unstable", { runs: r.runs })));

    const criteria = CRIT.map((c) => {
      const item = r.detail[c] || {}, runs = r.runScores && r.runScores[c];
      const evidence = (item.evidence || []).map((ev) => {
        const f = fileBy.get(ev.path), code = f ? snippet(f.patch, ev.lines) : null;
        return h("div", { class: "evidence" }, h("div", { class: "evidence-path" }, h("code", null, ev.path),
          ev.lines ? h("span", null, tr("pr.lines.label", { lines: ev.lines.replace("-", "–") })) : null), code);
      });
      return h("article", { class: "crit" },
        h("div", { class: "crit-side" }, h("h3", null, crit(c)), h("div", { class: "crit-score" }, h("span", { class: "big-score" }, trim(item.score)), h("span", { class: "of" }, tr("pr.of"))),
          meter(item.score, crit(c)), runs && runs.length > 1 ? h("div", { class: "runs", title: tr("pr.runs.title") }, tr("pr.runs", { list: runs.join(", ") })) : null,
          c === "risk" ? h("div", { class: "runs" }, tr("dev.norisk")) : null),
        h("div", { class: "crit-main" }, h("p", { class: "reason" }, item.reason || tr("pr.reason.none")),
          evidence.length ? evidence : h("p", { class: "note" }, tr(c === "clarity" ? "pr.evidence.clarity" : "pr.evidence.none"))));
    });

    const reviews = (pr.reviews || []).filter((rv) => rv.body || rv.state !== "COMMENTED");
    // HTML-комментарии шаблона PR (<!-- Thank you for your contribution -->) GitHub не показывает: здесь тоже.
    const body = String(pr.body || "").replace(/<!--[\s\S]*?(?:-->|$)/g, "").replace(/\n{3,}/g, "\n\n").trim();
    const aside = h("aside", { class: "pr-aside" },
      section(tr("note.title"), h("p", { class: "note" }, tr("note.lead")), noteField(r)),
      section(tr("pr.body"), body ? h("div", { class: "body-text" }, body) : h("p", { class: "note" }, tr("pr.body.none"))),
      reviews.length ? section(tr("pr.reviews", { n: reviews.length }), h("ul", { class: "reviews" }, reviews.slice(0, 8).map((rv) => h("li", null,
        badge(("rv." + rv.state) in L ? tr("rv." + rv.state) : rv.state, rv.state === "CHANGES_REQUESTED" ? "warn" : ""), rv.body ? h("span", null, rv.body) : null)))) : null,
      section(tr("pr.files", { n: files.length }), h("ul", { class: "files" }, files.slice(0, 30).map((f) => h("li", null, h("code", null, f.path),
        f.truncated ? h("span", { class: "dim" }, tr(f.patch ? "pr.file.partial" : "pr.file.hidden")) : null))),
        files.length > 30 ? h("p", { class: "note" }, tr("pr.files.more", { n: files.length - 30 })) : null,
        (pr.noise_removed || []).length ? h("p", { class: "note" }, tr("pr.noise", { n: pr.noise_removed.length })) : null));

    return [head, notices, h("div", { class: "pr-layout" }, h("div", { class: "pr-main" }, h("h2", { class: "vh" }, tr("pr.criteria")), criteria), aside)];
  }

  // ---------- экран: проверка метода ----------
  function viewCheck() {
    const v = D.val, head = h("div", { class: "page-head" }, h("h1", null, tr("check.title")), h("p", { class: "lead" }, tr("check.lead")));
    if (!v) return [head, hint(T("check.none")), section(tr("check.dist"), heatmap(D.rows))];
    const out = [head], risk = v.risk;

    if (risk) {
      const view = h("div", null, riskChart(risk)), toggle = h("button", { type: "button", class: "btn quiet", "aria-pressed": "false", onclick: () => {
        const asTable = toggle.getAttribute("aria-pressed") !== "true";
        toggle.setAttribute("aria-pressed", String(asTable));
        toggle.textContent = tr(asTable ? "check.as.chart" : "check.as.table");
        view.replaceChildren(asTable ? riskTable(risk) : riskChart(risk));
      } }, tr("check.as.table"));
      const a = risk.auc_risk, sz = risk.auc_size, strict = v.risk_strict;
      /* «Модель различает лучше размера» говорим, только когда сама связь риска с исходами подтверждена:
         при двух проблемных PR интервалы широкие и пересекаются, и такое сравнение ничего не значит. */
      const sizeKey = !a || !sz ? null : a.value <= sz.value ? "check.size.worse"
        : risk.verdict === "supported" ? "check.size.better" : "check.size.unclear";
      out.push(section(tr("check.risk"), verdict(risk),
        h("div", { class: "chart-card" }, h("div", { class: "chart-head" }, h("div", null, h("h3", null, tr("check.chart")),
          h("p", { class: "note" }, tr("check.chart.note", { n: risk.prs_used, k: risk.problems }))), toggle), view),
        h("ul", { class: "plain-list" },
          a ? h("li", null, T("check.auc", { v: num(a.value), lo: num(a.ci95[0]), hi: num(a.ci95[1]), half: num(0.5, 1) })) : null,
          sizeKey ? h("li", null, T(sizeKey, { v: num(sz.value), lo: num(sz.ci95[0]), hi: num(sz.ci95[1]) })) : null,
          risk.excluded_window_not_passed ? h("li", null, tr("check.excluded", { n: risk.excluded_window_not_passed })) : null,
          strict && strict.problems !== risk.problems && strict.auc_risk ? h("li", null, tr("check.strict", { k: strict.problems, v: num(strict.auc_risk.value) })) : null)));
    } else out.push(section(tr("check.risk"), hint(T("check.risk.none"))));

    const hu = v.human;
    out.push(section(tr("check.human"), hu ? [
      h("p", { class: "verdict" }, T("check.human.verdict", { pct: pct(hu.within_1), prs: hu.prs, n: hu.ratings, exact: pct(hu.exact) })),
      h("div", { class: "table-wrap narrow" }, h("table", { class: "grid plain" }, plainHead(["hu.crit", "hu.n", "hu.within", "hu.exact", "hu.diff"]),
        h("tbody", null, Object.entries(hu.criteria).map(([c, it]) => h("tr", null, h("td", null, CRIT.includes(c) ? crit(c) : c), h("td", { class: "r" }, it.n), h("td", { class: "r" }, pct(it.within_1)),
          h("td", { class: "r" }, pct(it.exact)), h("td", { class: "r" }, signed(it.mean_model_minus_human)))))))]
      : hint(T("check.human.none"))));

    const st = v.style;
    out.push(section(tr("check.style"), st ? [
      h("p", { class: "verdict" }, T(st.others_stable ? "check.style.no" : "check.style.yes"), tr("check.style.what", { n: st.n }),
        tr(st.clarity_dropped ? "check.style.dropped" : "check.style.notdropped"),
        st.others_stable ? tr("check.style.stable") : tr("check.style.moved", { crit: CRIT.includes(st.worst_other) ? crit(st.worst_other).toLowerCase() : "" })),
      h("div", { class: "shifts" }, CRIT.map((c) => h("div", { class: "shift" }, h("div", { class: "shift-num" }, signed(st.criteria[c].mean_shift)),
        h("div", { class: "fact-label" }, crit(c)), h("div", { class: "fact-note" }, tr("check.style.unchanged", { pct: pct(st.criteria[c].unchanged_share) })))))]
      : hint(T("check.style.none"))));

    const sb = v.stability || {};
    out.push(section(tr("check.stab"), h("ul", { class: "plain-list" },
      sb.prs_with_several_runs ? h("li", null, T("check.stab.runs", { k: sb.unstable, n: sb.prs_with_several_runs }),
        typeof sb.all_runs_identical === "number" ? tr("check.stab.same", { n: sb.all_runs_identical }) : "") : h("li", null, tr("check.stab.single")),
      typeof sb.evidence_removed === "number" ? h("li", null, T("check.stab.evidence", { n: sb.evidence_removed, m: sb.evidence_lines_cleared || 0 })) : null,
      typeof sb.answers_rejected_and_retried === "number" ? h("li", null, tr("check.stab.rejected", { n: sb.answers_rejected_and_retried })) : null,
      (sb.author_login_still_visible || []).length ? h("li", null, T("check.stab.login", { list: joined(sb.author_login_still_visible.map(prRef)) })) : h("li", null, tr("check.stab.nologin")))));

    out.push(section(tr("check.dist"), heatmap(D.rows), h("p", { class: "note" }, tr("check.dist.note"))));
    out.push(section(tr("check.limits"), h("ul", { class: "plain-list" }, ["limit.1", "limit.2", "limit.3", "limit.4"].map((k) => h("li", null, tr(k))))));
    return out;
  }

  // ---------- пустые состояния и маршруты ----------
  const notFound = (text, href, label) => [h("div", { class: "page-head" }, h("h1", null, tr("nf.title")), h("p", { class: "lead" }, text), h("p", null, link(href, label)))];
  function viewEmpty() {
    const missing = !D.havePrs && !D.haveScores ? "empty.none" : !D.havePrs ? "empty.noprs" : !D.haveScores ? "empty.noscores" : "empty.nocommon";
    return [h("div", { class: "empty" }, h("h1", null, tr("brand")), h("p", { class: "lead" }, T(missing)), h("p", null, T("empty.how")),
      h("button", { type: "button", class: "btn primary", onclick: () => $("data-dialog").showModal() }, tr("empty.load")))];
  }
  /* PR выгружены, а модель их ещё не оценивала: показываем, что известно, и как получить оценки. */
  function viewWaiting() {
    const rows = D.pending, perAuthor = new Map();
    for (const r of rows) perAuthor.set(r.author, (perAuthor.get(r.author) || 0) + 1);
    const days = rows.map((r) => r.merged).filter(Boolean).sort();
    const seen = rows.filter((r) => r.out && r.out.observable), problems = seen.filter((r) => r.out.problem);
    const facts = [
      [rows.length, tn("wait.fact.prs", rows.length), days.length ? `${date(days[0])} — ${date(days[days.length - 1])}` : ""],
      [perAuthor.size, tn("wait.fact.authors", perAuthor.size), tr("wait.fact.authors.note", { n: [...perAuthor.values()].filter((k) => k >= 3).length })],
      D.hasOutcomes ? [seen.length, tr("wait.fact.seen"), tr("wait.fact.seen.note", { n: rows.length - seen.length })] : ["—", tr("wait.fact.seen"), tr("fact.problem.nofile")],
      D.hasOutcomes ? [problems.length, tn("fact.problem", problems.length), tr("fact.problem.note", { n: seen.length })] : ["—", tn("fact.problem", 5), tr("fact.problem.nofile")],
    ];
    const github = (r) => (D.repo ? h("a", { href: githubPr(r.n), target: "_blank", rel: "noopener", class: "strong" }, title(r)) : h("strong", null, title(r)));
    const cols = [
      { key: "n", label: tr("prt.pr"), value: (r) => r.n, cell: (r) => h("span", { class: "dim" }, "#" + r.n) },
      { key: "title", label: tr("prt.title"), value: (r) => r.title.toLowerCase(), cell: (r) => h("span", { class: "title-cell" }, github(r)) },
      { key: "author", label: tr("prt.author"), value: (r) => r.author.toLowerCase(), cell: (r) => r.author },
      { key: "merged", label: tr("prt.merged"), value: (r) => r.merged, cell: (r) => h("span", { class: "nowrap" }, date(r.merged)) },
      { key: "lines", label: tr("prt.lines"), num: true, value: (r) => r.lines, cell: (r) => h("span", { class: "nowrap" }, `+${r.add} −${r.del}`) },
      D.hasOutcomes ? { key: "out", label: tr("prt.out"), value: (r) => (r.out ? (r.out.reverted ? 3 : r.out.problem ? 2 : r.out.observable ? 0 : 1) : 0), cell: outcomeBadge } : null,
    ].filter(Boolean);
    return [
      h("div", { class: "page-head" }, h("h1", null, tr("wait.title")), h("p", { class: "lead" }, T("wait.lead"))),
      h("div", { class: "facts" }, facts.map(([value, label, note]) =>
        h("div", { class: "fact" }, h("div", { class: "fact-value" }, value), h("div", { class: "fact-label" }, label), h("div", { class: "fact-note" }, note)))),
      section(tr("wait.how.title"), h("ol", { class: "steps" }, ["wait.how.1", "wait.how.2", "wait.how.3"].map((k) => h("li", null, T(k)))),
        h("p", { class: "note" }, T("wait.how.demo"))),
      section(tr("wait.list.title"), table("pending", cols, rows, { sort: "merged", dir: -1, caption: tr("wait.list.caption") })),
    ];
  }
  function render(keepScroll) {
    let address = location.hash.replace(/^#\/?/, "");
    try { address = decodeURIComponent(address); } catch (e) { /* битая ссылка вида #/dev/%E0%A4: не падаем, а покажем «не найдено» */ }
    const main = $("main"), parts = address.split("/");
    const route = parts[0] || "team";
    let view, tab = route;
    if (D.waiting) { view = viewWaiting(); tab = ""; }
    else if (!D.ready) { view = viewEmpty(); tab = ""; }
    else if (route === "dev") { view = viewDev(parts.slice(1).join("/")); tab = "team"; }
    else if (route === "pr") { view = viewPr(+parts[1]); tab = "prs"; }
    else if (route === "prs") view = viewPrs();
    else if (route === "check") view = viewCheck();
    else { view = viewTeam(); tab = "team"; }
    main.replaceChildren();
    fill(main, [view]);
    for (const a of document.querySelectorAll(".tabs a")) {
      if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    const heading = main.querySelector("h1");
    document.title = (heading && heading.textContent !== tr("brand") ? heading.textContent + " — " : "") + tr("brand");
    tipBox.hidden = true;
    if (!keepScroll) window.scrollTo(0, 0);
  }

  // ---------- загрузка файлов ----------
  function classify(name, data) {
    const first = Array.isArray(data) ? data.find((x) => x && typeof x === "object") || {} : null;
    if (first) {
      if ("multiplier" in first && "author" in first) return "metrics";
      if ("reverted_by" in first) return "outcomes";
      if ("scores" in first && "summary" in first) return /style/i.test(name) ? null : "scores";
      if ("files" in first && "merged_at" in first) return /style/i.test(name) ? null : "prs";
      return null;
    }
    if (data && typeof data === "object") {
      if ("stability" in data && "prs_scored" in data) return "validation";
      if ("merged_before" in data) return "history";
      if (Array.isArray(data.prs) && Array.isArray(data.types)) return "types";
      if ("weights" in data && "norms" in data) return "config";
      if ("prompt_version" in data || "per_pr" in data) return /style/i.test(name) ? null : "run_stats";
    }
    return null;
  }
  async function ingest(files) {
    const list = $("file-list");
    for (const file of files) {
      const li = h("li", null, h("code", null, file.name), " ");
      try {
        const data = JSON.parse(await file.text());
        const kind = classify(file.name, data);
        if (!kind) li.append(h("span", { class: "dim" }, tr("file.unused")));
        else { raw[kind] = data; li.append(h("span", { class: "ok-text" }, "✓ " + tr("kind." + kind))); }
      } catch (e) {
        li.append(h("span", { class: "bad-text" }, tr("file.bad")));
      }
      list.append(li);
    }
    build();
    render();
  }

  // ---------- язык, тема, запуск ----------
  function isDark() {
    return document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }
  /* Подписи, которые стоят прямо в index.html: data-i18n="ключ" для текста, data-i18n-attr="атрибут:ключ" для атрибута. */
  function applyStatic() {
    document.documentElement.lang = (L._locale || "ru").slice(0, 2);
    for (const el of document.querySelectorAll("[data-i18n]")) { el.replaceChildren(); fill(el, [T(el.dataset.i18n)]); }
    for (const el of document.querySelectorAll("[data-i18n-attr]")) {
      for (const pair of el.dataset.i18nAttr.split(";")) { const [attr, key] = pair.split(":"); el.setAttribute(attr, tr(key)); }
    }
    for (const b of document.querySelectorAll("[data-lang]")) b.setAttribute("aria-pressed", String(I18N[b.dataset.lang] === L));
    $("theme-toggle").textContent = tr(isDark() ? "theme.light" : "theme.dark");
  }
  function setLang(code, remember) {
    L = I18N[code] || I18N.ru || {};
    if (remember) try { localStorage.setItem("pr-impact-lang", code); } catch (e) { /* выбор просто не запомнится */ }
    applyStatic();
    if (D) render(true);
  }
  for (const b of document.querySelectorAll("[data-lang]")) b.addEventListener("click", () => setLang(b.dataset.lang, true));
  $("theme-toggle").addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("pr-impact-theme", next); } catch (e) { /* тема просто не запомнится */ }
    applyStatic();
  });
  $("data-open").addEventListener("click", () => $("data-dialog").showModal());
  $("file-input").addEventListener("change", (e) => { ingest([...e.target.files]); e.target.value = ""; });
  const drop = $("drop-zone");
  for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
  for (const type of ["dragleave", "drop"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  drop.addEventListener("drop", (e) => ingest([...e.dataTransfer.files]));
  window.addEventListener("hashchange", () => render());
  window.addEventListener("scroll", () => { tipBox.hidden = true; }, { passive: true });

  setLang(pickLang(), false);
  build();
  render();
})();
