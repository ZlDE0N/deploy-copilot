"use strict";
/* Deploy Copilot UI — no deps, no innerHTML with external data (everything via DOM nodes). */
const $ = (s, r = document) => r.querySelector(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const TYPE_LABEL = {"function-dotnet": "Function .NET", "function-node": "Function Node", "appservices-dotnet": "App Service .NET",
  "frontend-or-node": "Frontend / Node", "frontend-angular": "Angular", "container": "Container", "terraform": "Terraform",
  "mobile-react-native": "React Native", "mobile-native": "Mobile nativo", "unknown": "Sin clasificar", "go": "Go", "python": "Python"};
const tl = t => TYPE_LABEL[t] || t;
const ICON = {
  home: "M3 11l9-8 9 8M5 10v10h14V10", grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  chat: "M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z", cpu: "M9 9h6v6H9zM4 9h2M4 15h2M18 9h2M18 15h2M9 4v2M15 4v2M9 18v2M15 18v2",
  wave: "M3 17c3-6 5-6 8 0s5 6 10-4", search: "M11 4a7 7 0 100 14 7 7 0 000-14zM21 21l-4.3-4.3", play: "M6 4l14 8-14 8z"};

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style") e.style.cssText = v;
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat(2)) { if (c == null || c === false) continue; e.append(c.nodeType ? c : document.createTextNode(String(c))); }
  return e;
}
function svgIcon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICON[name]); s.append(p); return s;
}
async function api(path) {
  const r = await fetch(path, {headers: {Accept: "application/json", "X-Copilot": "1"}});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Copilot": "1"}, body: JSON.stringify(body)});
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
}
function toast(msg) { const t = h("div", {class: "toast", role: "status"}, msg); document.body.append(t); setTimeout(() => t.remove(), 2600); }
const lvlClass = s => s >= 70 ? "ok" : s >= 45 ? "warn" : "bad";
const lvlColor = s => s >= 70 ? "var(--ok)" : s >= 45 ? "var(--warn)" : "var(--bad)";
function ring(score, big) {
  const r = h("div", {class: "ring" + (big ? " big" : ""), style: `--p:${score};--c:${lvlColor(score)}`, title: `Readiness ${score}/100`}, h("span", {}, score));
  return r;
}
function highlight(text, toks) {
  const parts = (toks || []).filter(Boolean).map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!parts.length) return [text];
  const rx = new RegExp("(" + parts.join("|") + ")", "ig");
  return text.split(rx).map((seg, i) => i % 2 ? h("mark", {}, seg) : seg);
}
function bars(obj, max) {
  const m = max || Math.max(...Object.values(obj), 1);
  return Object.entries(obj).map(([k, v]) => h("div", {class: "bar"}, h("span", {class: "nm", title: k}, tl(k)),
    h("div", {class: "tr"}, h("div", {class: "fl", style: `width:${(v / m) * 100}%`})), h("span", {class: "ct"}, v)));
}
function clear(el) { while (el.firstChild) el.firstChild.remove(); }

/* ---------- live (SSE) ---------- */
const LIVE = {feed: [], handlers: new Set(), es: null, ok: false};
const EV_LABEL = {"job:start": "Trabajo iniciado", "job:done": "Trabajo terminado", "job:log": "log", "job:progress": "progreso", "reindex:start": "Reindexando fuentes…", "reindex:done": "Índice actualizado", "source:changed": "Cambio detectado en las fuentes",
  "deploy:recorded": "Despliegue registrado", "server:ready": "Servidor listo", error: "Error"};
function evText(ev) {
  const d = ev.data || {};
  if (ev.type === "reindex:done") return `${EV_LABEL[ev.type]}: ${d.chunks} fragmentos (${d.delta >= 0 ? "+" : ""}${d.delta}), ${d.embedded || 0} embebidos, en ${d.ms} ms`;
  if (ev.type === "deploy:recorded") return `${d.repo} → ${d.env}: ${d.result}`;
  if (ev.type === "error") return `${d.where}: ${d.msg}`;
  if (ev.type === "job:start") return `Trabajo iniciado: ${d.job}`;
  if (ev.type === "job:done") return `Trabajo ${d.job}: ${d.status} (${fmtDur(d.ms)})`;
  return EV_LABEL[ev.type] || ev.type;
}
function setLive(ok) {
  LIVE.ok = ok;
  const el = $("#live-state"); if (!el) return;
  el.textContent = ok ? "en vivo" : "reconectando…";
  el.previousElementSibling.className = "dot" + (ok ? "" : " off");
}
function connectLive() {
  const es = new EventSource("/api/events"); LIVE.es = es;
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);            // EventSource reconnects by itself and replays via Last-Event-ID
  Object.keys(EV_LABEL).forEach(t => es.addEventListener(t, m => {
    const ev = JSON.parse(m.data); if (ev.type !== "job:log" && ev.type !== "job:progress") { LIVE.feed.unshift(ev); LIVE.feed.length = Math.min(LIVE.feed.length, 40); }
    LIVE.handlers.forEach(fn => { try { fn(ev); } catch (_) {} });
  }));
}
function onLive(fn) { LIVE.handlers.add(fn); }
function feedCard() {
  const list = h("div", {class: "feed"});
  const draw = () => { clear(list);
    if (!LIVE.feed.length) list.append(h("div", {class: "muted small"}, "Esperando eventos…"));
    LIVE.feed.slice(0, 8).forEach(ev => list.append(h("div", {class: "ev " + (ev.type === "error" ? "bad" : ev.type === "reindex:done" ? "ok" : "")},
      h("span", {class: "t"}, new Date(ev.ts * 1000).toLocaleTimeString()), h("span", {}, evText(ev))))); };
  draw(); onLive(draw);
  return h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, "Actividad en vivo"),
    h("button", {class: "btn", onclick: async () => { try { await post("/api/reindex", {}); toast("Reindexado solicitado"); } catch (e) { toast("Error: " + e.message); } }}, "↻ Reindexar ahora")), list);
}

/* ---------- views ---------- */
async function viewHome(root) {
  const again = ev => { if (["reindex:done", "deploy:recorded", "job:done"].includes(ev.type)) route(); };
  root.append(h("h1", {}, "Panel de despliegues"), h("p", {class: "sub"}, "Estado de todo el portafolio, calculado de tus repos reales. Nada se ejecuta: el copiloto lee, planifica y genera borradores para revisión humana."));
  const sk = h("div", {class: "grid g4"}, h("div", {class: "skel"}), h("div", {class: "skel"}), h("div", {class: "skel"}), h("div", {class: "skel"}));
  root.append(sk);
  const [o, w] = await Promise.all([api("/api/overview"), api("/api/waves")]);
  sk.replaceWith(h("div", {class: "grid g4"},
    kpi("Proyectos", o.repos, "repos indexados"), kpi("Readiness medio", o.avg_readiness + "/100", `${o.readiness.listo} listos · ${o.readiness.casi} casi · ${o.readiness.trabajo} con trabajo`),
    kpi("Pipelines por convertir", o.pipelines_pending_actions, "Azure Pipelines → GitHub Actions"), kpi("Rama ≠ copia Azure", o.with_az_branches, `main/master difiere de su _az · ${o.with_lfs} usan LFS`)));
  root.append(h("div", {class: "grid g2", style: "margin-top:16px"},
    h("div", {class: "card"}, h("h2", {}, "Portafolio por tipo"), bars(o.by_type)),
    h("div", {class: "card"}, h("h2", {}, "Olas sugeridas de despliegue"),
      h("p", {class: "muted small"}, "Ordenadas por readiness: empieza por lo que ya está listo."),
      Object.entries(w).map(([name, list]) => h("div", {style: "margin:12px 0"},
        h("div", {class: "row spread"}, h("b", {}, name), h("span", {class: "pill acc"}, list.length)),
        h("div", {class: "muted small"}, list.slice(0, 4).map(x => x.name).join(" · ") + (list.length > 4 ? " …" : "")))))));
  root.append(realCards(o));
  root.append(h("div", {style: "margin-top:16px"}, feedCard()));
  onLive(again);
  root.append(h("div", {class: "grid g2", style: "margin-top:16px"},
    h("div", {class: "card"}, h("h2", {}, "Más listos"), o.top_ready.map(projLine)),
    h("div", {class: "card"}, h("h2", {}, "Requieren más trabajo"), o.least_ready.map(projLine))));
}
const kpi = (l, v, n) => h("div", {class: "card kpi"}, h("div", {class: "l"}, l), h("div", {class: "v"}, v), h("div", {class: "n"}, n));
const projLine = p => h("div", {class: "row spread", style: "padding:6px 0;cursor:pointer", onclick: () => location.hash = "#/project/" + encodeURIComponent(p.name)},
  h("span", {}, p.name, " ", h("span", {class: "muted small"}, tl(p.type))), h("span", {class: "pill " + lvlClass(p.score)}, p.score));

async function viewProjects(root) {
  root.append(h("h1", {}, "Proyectos"), h("p", {class: "sub"}, "Cada proyecto tiene su plan, workflow, runbook y métricas generados automáticamente."));
  const repos = await api("/api/repos");
  let q = "", type = "todos", az = false;
  const list = h("div", {class: "grid g4", style: "grid-template-columns:repeat(auto-fill,minmax(270px,1fr))"});
  const chips = h("div", {class: "chips"});
  const input = h("input", {class: "in", placeholder: "Filtrar por nombre…", "aria-label": "Filtrar proyectos", style: "width:min(340px,100%)", oninput: e => { q = e.target.value.toLowerCase(); draw(); }});
  const types = ["todos", ...[...new Set(repos.map(r => r.type))].sort()];
  function drawChips() {
    clear(chips);
    types.forEach(t => chips.append(h("span", {class: "chip" + (type === t ? " on" : ""), role: "button", tabindex: "0",
      onclick: () => { type = t; drawChips(); draw(); }}, t === "todos" ? `todos (${repos.length})` : `${tl(t)} (${repos.filter(r => r.type === t).length})`)));
    chips.append(h("span", {class: "chip" + (az ? " on" : ""), role: "button", tabindex: "0", onclick: () => { az = !az; drawChips(); draw(); }}, "rama ≠ _az"));
  }
  function draw() {
    clear(list);
    const f = repos.filter(r => (type === "todos" || r.type === type) && (!az || r.az) && r.name.toLowerCase().includes(q));
    if (!f.length) list.append(h("div", {class: "empty"}, "Sin resultados"));
    f.forEach(r => list.append(h("div", {class: "card hover proj", tabindex: "0", role: "link", onclick: () => location.hash = "#/project/" + encodeURIComponent(r.name),
      onkeydown: e => { if (e.key === "Enter") location.hash = "#/project/" + encodeURIComponent(r.name); }},
      h("div", {class: "row spread", style: "flex-wrap:nowrap"}, h("h3", {}, r.name), ring(r.readiness)),
      h("div", {class: "row"}, h("span", {class: "pill acc"}, tl(r.type)), h("span", {class: "pill"}, r.strategy), r.az && h("span", {class: "pill warn"}, "≠ _az"), r.lfs && h("span", {class: "pill"}, "LFS")),
      h("div", {class: "meta"}, `${r.workflow || "sin pipeline"} · ${r.last_commit || "—"}`))));
  }
  root.append(input, chips, list); drawChips(); draw();
}

async function viewProject(root, name) {
  let env = "prod", tab = "Resumen", d;
  const body = h("div"), head = h("div");
  root.append(h("span", {class: "back", onclick: () => history.length > 1 ? history.back() : location.hash = "#/projects"}, "← Proyectos"), head, body);
  async function load() { d = await api(`/api/repo?name=${encodeURIComponent(name)}&env=${env}`); drawHead(); drawTab(); }
  function drawHead() {
    clear(head);
    head.append(h("div", {class: "row spread"},
      h("div", {}, h("h1", {style: "word-break:break-all"}, d.repo.name),
        h("div", {class: "row"}, h("span", {class: "pill acc"}, tl(d.repo.type)), h("span", {class: "pill"}, "estrategia: " + d.strategy),
          h("span", {class: "pill " + (d.risk.level === "ALTO" ? "bad" : d.risk.level === "MEDIO" ? "warn" : "ok")}, "riesgo " + d.risk.level))),
      h("div", {class: "row"}, ring(d.readiness.score, true),
        h("select", {class: "in", "aria-label": "Entorno", onchange: async e => { env = e.target.value; await load(); }},
          ["qa", "prod"].map(x => h("option", {value: x, selected: x === env}, "Entorno: " + x))))));
    const tabs = h("div", {class: "tabs", role: "tablist"});
    ["Resumen", "Datos reales", "Plan y ensayo", "Workflow", "Runbook", "Métricas", "Evidencia"].forEach(t => tabs.append(h("div", {class: "tab" + (tab === t ? " on" : ""), role: "tab", tabindex: "0",
      onclick: () => { tab = t; drawHead(); drawTab(); }}, t)));
    head.append(tabs);
  }
  function drawTab() {
    clear(body);
    ({Resumen: tabSummary, "Datos reales": tabReal, "Plan y ensayo": tabPlan, Workflow: tabWorkflow, Runbook: tabRunbook, "Métricas": tabMetrics, Evidencia: tabEvidence})[tab](body, d, env, load);
  }
  onLive(ev => { if (ev.type === "deploy:recorded" && ev.data.repo === name || ev.type === "reindex:done") load().catch(() => {}); });
  await load();
}
function tabSummary(b, d) {
  const r = d.repo;
  b.append(h("div", {class: "grid g2"},
    h("div", {class: "card"}, h("h2", {}, "Manifiesto detectado"),
      [["Rama base", r.branch], ["Pipeline", r.workflow || "ninguno"], ["Proyecto Azure DevOps", r.org_project || "n/d"], ["Ramas heredadas", r.az_branches || "ninguna"],
       ["Archivos", r.n_files], ["Último commit", r.last_commit || "—"], ["Git LFS", r.has_lfs ? "sí" : "no"], ["Terraform", r.has_terraform ? "sí" : "no"], ["Dockerfile", r.has_dockerfile ? "sí" : "no"]]
        .map(([k, v]) => h("div", {class: "row spread", style: "padding:5px 0;border-bottom:1px dashed var(--line)"}, h("span", {class: "muted"}, k), h("span", {style: "word-break:break-all;text-align:right"}, v)))),
    h("div", {class: "card"}, h("h2", {}, "Readiness " + d.readiness.score + "/100"),
      d.readiness.items.map(i => h("div", {class: "row spread", style: "padding:5px 0"}, h("span", {}, (i.ok ? "✓ " : "✗ ") + i.name), h("span", {class: "pill " + (i.ok ? "ok" : "bad")}, (i.ok ? "+" : "0") + "/" + i.points)))),
    h("div", {class: "card"}, h("h2", {}, "Políticas (código, no el modelo)"),
      d.risk.checks.map(c => h("div", {class: "pol"}, h("span", {class: "pill " + ({PASS: "ok", FAIL: "bad", WARN: "warn", REQ: "acc"})[c.level]}, c.level), h("span", {}, c.msg)))),
    h("div", {class: "card"}, h("h2", {}, "Migración del pipeline"),
      h("p", {class: "muted"}, (d.repo.workflow || "").includes("azure-pipelines") ? "Este proyecto aún usa Azure Pipelines. Para convertirlo a GitHub Actions usa el importador oficial (requiere Docker y tokens por variable de entorno):" : "El pipeline ya no es de Azure Pipelines."),
      h("pre", {class: "code"}, d.importer))));
}
function tabPlan(b, d, env) {
  const steps = h("div"), msg = h("p", {class: "muted small"}, "Ensayo = simulación visual del plan y las políticas. No ejecuta nada.");
  const draw = (state = {}) => { clear(steps); d.steps.forEach((s, i) => steps.append(h("div", {class: "step " + (state[i] || "")}, h("div", {class: "ix"}, state[i] === "done" ? "✓" : state[i] === "fail" ? "✕" : i + 1), h("div", {}, s)))); };
  draw();
  const btn = h("button", {class: "btn pri", onclick: async () => {
    btn.disabled = true; const st = {};
    if (d.risk.blocked) { st[0] = "fail"; draw(st); msg.textContent = "Ensayo detenido: una política FAIL bloquea el despliegue."; btn.disabled = false; return; }
    for (let i = 0; i < d.steps.length; i++) { st[i] = "run"; draw(st); await sleep(550); st[i] = "done"; draw(st); }
    msg.textContent = "Ensayo completo (simulación). Para producción se requiere aprobación humana en el environment de GitHub."; btn.disabled = false;
  }}, "▶ Ensayar plan");
  b.append(h("div", {class: "grid g2"},
    h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, `Plan ${d.strategy} → ${env}`), btn), steps, msg,
      h("p", {}, h("b", {}, "Rollback: "), d.rollback)),
    h("div", {class: "card"}, h("h2", {}, "Checklist previa"), d.checklist.map((c, i) => {
      const key = `chk:${d.repo.name}:${env}:${i}`; let on = false;
      try { on = localStorage.getItem(key) === "1"; } catch (_) {}
      return h("label", {class: "check"}, h("input", {type: "checkbox", checked: on, onchange: e => { try { localStorage.setItem(key, e.target.checked ? "1" : "0"); } catch (_) {} }}), h("span", {}, c));
    }))));
}
function copyBtn(text, label = "Copiar") {
  return h("button", {class: "btn", onclick: async () => { try { await navigator.clipboard.writeText(text); toast("Copiado"); } catch (_) { toast("No se pudo copiar"); } }}, label);
}
function tabWorkflow(b, d) {
  const w = d.workflow;
  if (!w.supported) { b.append(h("div", {class: "card empty"}, w.reason)); return; }
  b.append(h("div", {class: "card"}, h("div", {class: "row spread"},
    h("div", {}, h("h2", {}, "GitHub Actions · " + w.kind), h("div", {class: "muted small"}, "Guardar como " + w.path + " · borrador para revisión humana")), copyBtn(w.yaml)),
    h("pre", {class: "code"}, w.yaml)));
}
function tabRunbook(b, d) {
  b.append(h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, "Runbook generado"), copyBtn(d.runbook, "Copiar Markdown")), h("pre", {class: "code", style: "white-space:pre-wrap"}, d.runbook)));
}
function tabMetrics(b, d, env, reload) {
  const m = d.dora;
  const res = h("select", {class: "in", "aria-label": "Resultado"}, ["ok", "rollback", "failed"].map(x => h("option", {value: x}, x)));
  const notes = h("input", {class: "in", placeholder: "Notas (opcional)", maxlength: "200", style: "flex:1"});
  b.append(h("div", {class: "grid g2"},
    h("div", {class: "card"}, h("h2", {}, "Métricas tipo DORA (prod)"),
      m.deploys ? h("div", {}, [["Despliegues registrados", m.deploys], ["Frecuencia", m.per_week + " / semana"], ["Tasa de fallo", (m.change_failure_rate * 100).toFixed(0) + "%"], ["MTTR", m.mttr_hours == null ? "n/d" : m.mttr_hours + " h"], ["Banda", m.band]]
        .map(([k, v]) => h("div", {class: "row spread", style: "padding:6px 0"}, h("span", {class: "muted"}, k), h("b", {}, v))), h("p", {class: "muted small"}, m.note))
        : h("p", {class: "muted"}, "Sin despliegues en prod registrados. Registra cada resultado para que el riesgo y las métricas mejoren.")),
    h("div", {class: "card"}, h("h2", {}, "Registrar despliegue (" + env + ")"),
      h("div", {class: "row"}, res, notes, h("button", {class: "btn pri", onclick: async () => {
        try { await post("/api/record", {repo: d.repo.name, env, strategy: d.strategy, result: res.value, notes: notes.value}); toast("Registrado"); await reload(); } catch (e) { toast("Error: " + e.message); } }}, "Guardar")),
      h("h2", {style: "margin-top:18px"}, "Historial"),
      d.history.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Fecha", "Env", "Resultado", "Notas"].map(x => h("th", {}, x)))),
        h("tbody", {}, d.history.map(x => h("tr", {}, h("td", {class: "num"}, x.ts), h("td", {}, x.env), h("td", {}, h("span", {class: "pill " + (x.result === "ok" ? "ok" : "bad")}, x.result)), h("td", {}, x.notes || "")))))
        : h("div", {class: "muted"}, "Aún vacío."))));
}
function tabEvidence(b, d) {
  b.append(h("div", {class: "card"}, h("h2", {}, "Evidencia que respalda el plan"),
    d.evidence.length ? d.evidence.map(e => h("div", {class: "src"}, h("div", {class: "t"}, e.title), h("div", {class: "s"}, `(${e.kind}) ${e.source}`), h("p", {}, e.snippet)))
      : h("div", {class: "muted"}, "No hay incidentes o documentos relacionados con este proyecto en el índice.")));
}

/* ---------- RAG ---------- */
async function viewAsk(root) {
  root.append(h("h1", {}, "Pregúntale a tu infraestructura"), h("p", {class: "sub"}, "Búsqueda híbrida sobre tus repos, pipelines, incidentes y documentos. Cada respuesta cita su fuente y muestra cómo se obtuvo."));
  const input = h("input", {class: "in", placeholder: "Ej: ¿cuál fue la causa raíz de los 502 en el servicio de pagos?", style: "flex:1;min-width:240px", "aria-label": "Pregunta", maxlength: "300"});
  const kind = h("select", {class: "in", "aria-label": "Tipo de fuente"}, [["", "Todas las fuentes"], ["incident", "Incidentes"], ["script", "Scripts de migración"], ["workflow", "Pipelines"], ["doc", "Documentos"], ["readme", "READMEs"], ["manifest", "Fichas de repo"]].map(([v, t]) => h("option", {value: v}, t)));
  const llm = h("input", {type: "checkbox"});
  const flowNodes = [["Consulta", "tokens"], ["BM25", "candidatos"], ["N-gramas", "candidatos"], ["Semántico", "bge-m3"], ["Fusión RRF", "únicos"], ["Rerank", "resultados"], ["Respuesta", "citas"]].map(([t, s]) => h("div", {class: "node"}, h("b", {}, t), h("small", {}, s), h("div", {class: "num"}, "–")));
  const out = h("div");
  let seq = 0;
  async function run() {
    const text = input.value.trim(); if (!text) return;
    const my = ++seq;
    flowNodes.forEach(n => { n.classList.remove("lit"); n.lastChild.textContent = "–"; });
    clear(out); out.append(h("div", {class: "skel"}));
    let res;
    try { res = await api(`/api/search?q=${encodeURIComponent(text)}&kind=${kind.value}`); }
    catch (e) { clear(out); out.append(h("div", {class: "card empty"}, "Error: " + e.message)); return; }
    if (my !== seq) return;                      // a newer keystroke superseded this response
    const t = res.trace;
    const vals = [t.tokens.length, t.sparse_candidates, t.dense_candidates, t.semantic_candidates,  t.fused_candidates, t.returned, res.answer.sentences ? res.answer.sentences.length : 1];
    for (let i = 0; i < flowNodes.length; i++) { flowNodes[i].classList.add("lit"); flowNodes[i].lastChild.textContent = vals[i]; await sleep(60); if (my !== seq) return; }
    clear(out);
    const ans = h("div", {class: "card answer"}, h("div", {class: "row spread"}, h("h2", {}, "Respuesta"), h("span", {class: "pill acc"}, res.answer.mode)));
    if (res.answer.mode === "extractive" && res.answer.sentences.length) res.answer.sentences.forEach(s => ans.append(h("p", {style: "margin:8px 0"}, ...highlight(s.text, t.tokens), h("sup", {}, `[${s.cite}]`))));
    else if (res.answer.mode === "llm") ans.append(h("p", {style: "white-space:pre-wrap"}, res.answer.text));
    else ans.append(h("p", {class: "muted"}, res.answer.error || "Sin fragmentos suficientemente relevantes."));
    if (res.structured) out.append(exactCard(res.structured));
    if (!(res.structured && !(res.answer.sentences || []).length)) out.append(ans);
    if (llm.checked) out.append(aiCard(text, kind.value, my, () => my === seq));
    if (t.semantic_error) out.append(h("div", {class: "pill warn", style: "margin:10px 0"}, "Búsqueda semántica no disponible (" + t.semantic_error + "): resultados solo con BM25 y n-gramas"));
    out.append(h("div", {class: "muted small", style: "margin:12px 2px"}, `${t.ms} ms · intención detectada: ${t.intent_kinds.join(", ") || "general"} · tokens: ${t.tokens.join(", ")}`));
    res.hits.forEach((x, i) => out.append(h("div", {class: "src"}, h("div", {class: "row spread"}, h("div", {class: "t"}, `[${i + 1}] ${x.title}`), h("span", {class: "pill"}, x.kind + " · " + x.score)),
      h("div", {class: "s"}, x.source), h("p", {}, ...highlight(x.snippet, t.tokens)),
      h("div", {class: "why"}, h("span", {}, "BM25 #" + (x.why.in_sparse ?? "–")), h("span", {}, "n-gramas #" + (x.why.in_dense ?? "–")), h("span", {}, "semántico #" + (x.why.in_semantic ?? "–")), h("span", {}, "RRF " + x.why.rrf), h("span", {}, "cobertura " + x.why.coverage), x.why.intent > 0 && h("span", {}, "intención +" + x.why.intent), x.why.repo > 0 && h("span", {}, "repo +" + x.why.repo)))));
    if (!res.hits.length) out.append(h("div", {class: "card empty"}, "Sin evidencia para esta consulta."));
  }
  input.addEventListener("keydown", e => { if (e.key === "Enter") { clearTimeout(timer); run(); } });
  let timer; input.addEventListener("input", () => { clearTimeout(timer); if (input.value.trim().length >= 3) timer = setTimeout(run, 350); });
  function exactCard(s) {
    return h("div", {class: "card", style: "margin-bottom:12px;border-color:rgba(52,211,153,.4)"},
      h("div", {class: "row spread"}, h("h2", {}, "Respuesta exacta"), h("span", {class: "pill ok"}, "SQL · sin IA")),
      h("div", {class: "row", style: "align-items:baseline;gap:14px"}, h("span", {class: "kpi"}, h("span", {class: "v"}, s.count)),
        h("span", {class: "muted"}, `${s.label} · de ${s.total} repos indexados`)),
      s.repos.length ? h("div", {class: "chips", style: "margin:10px 0 6px"}, s.repos.map(n => h("span", {class: "chip", role: "link", tabindex: "0",
        onclick: () => { location.hash = "#/project/" + encodeURIComponent(n); }}, n)), s.truncated && h("span", {class: "muted small"}, "…")) : null,
      h("pre", {class: "code", style: "max-height:none;padding:10px;margin-top:8px"}, s.sql));
  }
  async function streamAI(text, kindv, body, meta, alive, pill) {
    try {
      const r = await fetch(`/api/answer?q=${encodeURIComponent(text)}&kind=${kindv}`, {headers: {"X-Copilot": "1"}});
      if (!r.ok || !r.body) throw new Error("HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", ev = "", got = false;
      for (;;) {
        const {done, value} = await rd.read(); if (done || !alive()) break;
        buf += dec.decode(value, {stream: true});
        let i; while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          const m = block.match(/^event: (\w+)\ndata: (.*)$/s); if (!m) continue; ev = m[1]; const d = JSON.parse(m[2]);
          if (ev === "token") { if (!got) { got = true; body.textContent = ""; } body.append(document.createTextNode(d.t)); }
          else if (ev === "done") {
            const v = d.verify || {grounded: 0, total: 0};
            meta.textContent = `${d.model} · ${d.gpu ? "GPU" : "CPU"} · 1er token ${(d.first_token_ms / 1000).toFixed(1)} s · ${d.tokens} tokens · ${(d.ms / 1000).toFixed(1)} s · citas ancladas ${v.grounded}/${v.total}`;
            const all = v.total > 0 && v.grounded === v.total;
            pill.textContent = all ? "citas verificadas" : v.grounded ? "citas parciales" : "sin citas verificables";
            pill.className = "pill " + (all ? "ok" : "warn");
          }
          else if (ev === "fail") { body.textContent = "No se pudo generar: " + d.error; }
        }
      }
    } catch (e) { body.textContent = "No se pudo generar: " + e.message; }
  }
  function aiCard(text, kindv, my, alive) {
    const body = h("p", {class: "muted", style: "white-space:pre-wrap;margin:8px 0"}, "Generando… (unos segundos)");
    const meta = h("div", {class: "muted small"}, ""), pill = h("span", {class: "pill warn"}, "generando");
    streamAI(text, kindv, body, meta, alive, pill);
    return h("div", {class: "card answer", style: "margin-top:12px;border-color:rgba(34,211,238,.35)"}, h("div", {class: "row spread"}, h("h2", {}, "Borrador IA local"), pill), body, meta);
  }
  const examples = ["causa raíz de los errores 502 en el servicio de pagos", "cómo migramos repos con archivos pesados con git lfs", "hallazgos de la auditoría de seguridad", "qué repos usan terraform", "rollback swap warm-up timeout"];
  root.append(h("div", {class: "row"}, input, kind, h("button", {class: "btn pri", onclick: run}, "Buscar")),
    h("label", {class: "row small muted", style: "margin:8px 0"}, llm, "Borrador con IA local (GPU vía Docker Model Runner, streaming). Verifica con las fuentes; nada sale de tu máquina."),
    h("div", {class: "chips"}, examples.map(x => h("span", {class: "chip", role: "button", tabindex: "0", onclick: () => { input.value = x; run(); }}, x))),
    h("div", {class: "card", style: "margin:6px 0 18px"}, h("div", {class: "flow"}, flowNodes)), out);
  input.focus();
}

async function viewHow(root) {
  root.append(h("h1", {}, "Cómo funciona el RAG"), h("p", {class: "sub"}, "Todo corre en tu máquina: SQLite y Python estándar, más Ollama en Docker para embeddings y generación. Ningún dato sale de localhost."));
  const [o, ev] = await Promise.all([api("/api/overview"), api("/api/eval")]);
  const step = (n, t, d) => h("div", {class: "card"}, h("div", {class: "row"}, h("span", {class: "pill acc"}, n), h("b", {}, t)), h("p", {class: "muted", style: "margin:8px 0 0"}, d));
  root.append(h("div", {class: "grid g2"},
    step("1 · Ingesta", "Leer, redactar, trocear", "Lee 170+ repos bare con git (árbol de archivos, workflows, READMEs), CSV de pipelines, reportes de incidentes y scripts de migración. Redacta secretos antes de indexar (verificado con gitleaks) y trocea por secciones."),
    step("2 · Manifiesto", "Hechos estructurados", "Detecta tipo de proyecto, pipeline, LFS y ramas _az desde el contenido real del repo. Las políticas de despliegue son código determinista, no el modelo."),
    step("3 · Recuperación híbrida", "BM25 + n-gramas + embeddings + RRF", "BM25 (FTS5) acierta con identificadores exactos como main_az o ERR_502. Los trigramas toleran erratas. Embeddings bge-m3 (locales, 1024 dim, multilingüe) capturan significado. Reciprocal Rank Fusion combina los tres sin calibrar puntajes."),
    step("4 · Rerank y diversidad", "Cobertura ponderada por IDF", "Reordena por términos raros cubiertos, coincidencia de título/repo e intención de la pregunta; limita fichas plantilla y fragmentos por fuente para no ahogar la evidencia."),
    step("5 · Respuesta citada", "Extractiva, o borrador con IA local", "Por defecto elige las frases más relevantes de las fuentes. Opcionalmente un modelo local (Ollama) redacta un borrador en streaming; con el modelo pequeño actual puede equivocarse, por eso siempre se muestran las fuentes."),
    step("6 · Aprendizaje", "Cada despliegue mejora el riesgo", "Los resultados registrados alimentan el riesgo, el readiness y las métricas DORA de cada proyecto.")));
  const best = k => Math.max(...["bm25", "ngram", "semantic", "hybrid"].map(m => ev[m][k] ?? -1));
  const row = (m, label) => h("tr", {}, h("td", {}, label), ["recall@5", "mrr"].map(k => h("td", {class: "num " + (ev[m][k] === best(k) ? "best" : "")}, ev[m][k] == null ? "n/d" : ev[m][k].toFixed(3))), h("td", {class: "num muted"}, ev[m].unavailable ? "no disponible: " + ev[m].unavailable : ev[m].ranks.map(r => r ?? "×").join(" ")));
  root.append(h("div", {class: "card", style: "margin-top:16px"}, h("h2", {}, `Evaluación sobre ${ev.questions.length} preguntas de oro`),
    h("table", {}, h("thead", {}, h("tr", {}, ["Recuperador", "recall@5", "MRR", "rango por pregunta"].map(x => h("th", {}, x)))),
      h("tbody", {}, row("bm25", "Solo BM25"), row("ngram", "Solo n-gramas"), row("semantic", "Solo semántico (bge-m3)"), row("hybrid", "Híbrido (RRF + rerank)"))),
    h("p", {class: "muted small"}, "Lectura honesta: 14 preguntas sirven para detectar regresiones, no para probar superioridad. El semántico (bge-m3) logra el mejor MRR; el híbrido añade las señales de metadatos (repo, título, intención) y rescata preguntas agregadas como “qué repos usan terraform”. Todos empatan en recall@5. × = no encontrado en el top evaluado.")));
  root.append(h("div", {class: "card", style: "margin-top:16px"}, h("h2", {}, "Contenido indexado"), bars(o.chunks_by_kind)));
}


/* ---------- helpers for real data ---------- */
function fmtDur(ms) { return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " s"; }
function ago(ts) { if (!ts) return "nunca"; const s = Date.now() / 1000 - ts; return s < 90 ? "hace instantes" : s < 5400 ? `hace ${Math.round(s / 60)} min` : s < 129600 ? `hace ${Math.round(s / 3600)} h` : `hace ${Math.round(s / 86400)} d`; }
function statusPill(s) { const m = {ok: "ok", running: "acc", failed: "bad", cancelled: "warn", interrupted: "warn"}; return h("span", {class: "pill " + (m[s] || "")}, s || "—"); }
function resultText(r) { return Object.entries(r || {}).filter(([k, v]) => typeof v !== "object").map(([k, v]) => `${k}: ${v}`).join(" · "); }
function sparkline(weekly) {
  const ns = "http://www.w3.org/2000/svg", w = 220, hh = 46;
  if (!weekly.length) return h("div", {class: "muted small"}, "Sin datos semanales");
  const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", `0 0 ${w} ${hh}`); svg.setAttribute("width", "100%"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Ejecuciones por semana (rojo = fallidas)");
  const mx = Math.max(...weekly.map(x => x.runs), 1), bw = w / weekly.length;
  weekly.forEach((x, i) => {
    const th = Math.max(2, (x.runs / mx) * (hh - 4)), fh = (x.failures / mx) * (hh - 4);
    for (const [hgt, color, y] of [[th, "var(--acc)", hh - th], [fh, "var(--bad)", hh - fh]]) {
      if (hgt <= 0) continue; const r = document.createElementNS(ns, "rect");
      r.setAttribute("x", i * bw + 1); r.setAttribute("y", y); r.setAttribute("width", Math.max(bw - 3, 2)); r.setAttribute("height", hgt); r.setAttribute("rx", 2); r.setAttribute("fill", color);
      const t = document.createElementNS(ns, "title"); t.textContent = `${x.week}: ${x.runs} ejecuciones, ${x.failures} fallidas`; r.append(t); svg.append(r);
    }
  });
  return svg;
}
async function startJob(job, params, after) {
  try { await post("/api/jobs/start", {job, params}); toast("Trabajo iniciado: " + job); if (after) after(); }
  catch (e) { toast("No se pudo iniciar: " + e.message); }
}

/* ---------- Operaciones (manual, repeatable jobs) ---------- */
async function viewOps(root) {
  root.append(h("h1", {}, "Operaciones"), h("p", {class: "sub"}, "Cada proceso se ejecuta a mano y se puede repetir las veces que quieras. Todo es de solo lectura sobre tus sistemas; el resultado y el log quedan guardados."));
  const grid = h("div", {class: "grid g2"}), logCard = h("div", {class: "card", style: "margin-top:16px"}), hist = h("div", {class: "card", style: "margin-top:16px"});
  root.append(grid, logCard, hist);
  const inputs = {}, bars = {}; let shown = null, jobsData = [];
  async function load() { jobsData = await api("/api/jobs"); drawJobs(); drawHist(); }
  function drawJobs() {
    clear(grid);
    jobsData.forEach(j => {
      const fields = j.params.map(p => {
        const key = j.name + "." + p.key; const prev = inputs[key];
        const el = p.type === "bool" ? h("input", {type: "checkbox", checked: prev ?? p.default}) : h("input", {class: "in", type: p.type === "int" ? "number" : "text", value: prev ?? p.default ?? "", min: p.min, max: p.max, style: "width:" + (p.type === "int" ? "90px" : "180px"), "aria-label": p.label || p.key});
        inputs[key] = p.type === "bool" ? el.checked : el.value; el.addEventListener("input", () => { inputs[key] = p.type === "bool" ? el.checked : el.value; });
        return h("label", {class: "row small muted"}, el, p.label || p.key);
      });
      const bar = h("div", {class: "tr", style: "height:6px;margin-top:10px;display:" + (j.running ? "block" : "none")}, h("div", {class: "fl", style: "width:0%"})); bars[j.name] = bar;
      const last = j.last;
      grid.append(h("div", {class: "card"},
        h("div", {class: "row spread"}, h("h2", {style: "margin:0"}, j.title), j.running ? statusPill("running") : last ? statusPill(last.status) : h("span", {class: "pill"}, "sin ejecutar")),
        h("p", {class: "muted small", style: "margin:6px 0 10px"}, j.desc),
        fields.length ? h("div", {class: "row", style: "margin-bottom:10px;gap:14px"}, fields) : null,
        h("div", {class: "row"},
          j.running ? h("button", {class: "btn", onclick: async () => { await post("/api/jobs/cancel", {job: j.name}); toast("Cancelación solicitada"); }}, "■ Cancelar")
                    : h("button", {class: "btn pri", onclick: () => startJob(j.name, Object.fromEntries(j.params.map(p => [p.key, p.type === "int" ? Number(inputs[j.name + "." + p.key]) : inputs[j.name + "." + p.key]])), load)}, last ? "↻ Ejecutar de nuevo" : "▶ Ejecutar"),
          h("button", {class: "btn", onclick: () => showRun(last && last.id)}, "Ver log")),
        bar, last ? h("div", {class: "muted small", style: "margin-top:10px"}, `${ago(last.finished || last.started)} · ${resultText(last.result).slice(0, 170)}`) : null));
    });
  }
  async function showRun(id) {
    clear(logCard); if (!id) { logCard.append(h("div", {class: "muted"}, "Aún no hay ejecuciones.")); return; }
    const d = await api("/api/jobs/run?id=" + id); shown = id;
    logCard.append(h("div", {class: "row spread"}, h("h2", {style: "margin:0"}, `Log · ${d.job} #${d.id}`), statusPill(d.status)),
      h("pre", {class: "code", id: "joblog", style: "margin-top:10px;max-height:300px"}, d.log || "(sin salida)"),
      d.result && Object.keys(d.result).length ? h("div", {class: "muted small", style: "margin-top:8px"}, resultText(d.result)) : null);
  }
  async function drawHist() {
    const rows = await api("/api/jobs/runs"); clear(hist);
    hist.append(h("h2", {}, "Historial de ejecuciones"), rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ["#", "Trabajo", "Estado", "Cuándo", "Duración", "Parámetros", ""].map(x => h("th", {}, x)))),
      h("tbody", {}, rows.map(r => h("tr", {}, h("td", {class: "num"}, r.id), h("td", {}, r.job), h("td", {}, statusPill(r.status)), h("td", {}, ago(r.started)),
        h("td", {class: "num"}, r.finished ? fmtDur((r.finished - r.started) * 1000) : "—"), h("td", {class: "muted small"}, JSON.stringify(r.params)),
        h("td", {}, h("button", {class: "btn", onclick: () => showRun(r.id)}, "log"), " ", h("button", {class: "btn", onclick: () => startJob(r.job, r.params, load)}, "repetir")))))) : h("div", {class: "muted"}, "Sin ejecuciones todavía."));
  }
  onLive(ev => {
    if (ev.type === "job:progress") { const b = bars[ev.data.job]; if (b) { b.style.display = "block"; b.firstChild.style.width = (ev.data.done / ev.data.total * 100) + "%"; } }
    else if (ev.type === "job:log" && ev.data.id === shown) { const p = $("#joblog"); if (p) { p.append(document.createTextNode("\n" + ev.data.line)); p.scrollTop = p.scrollHeight; } }
    else if (ev.type === "job:start") { load().then(() => showRun(ev.data.id)); }
    else if (ev.type === "job:done") { load(); if (ev.data.id === shown) showRun(ev.data.id); }
  });
  await load(); const last = jobsData.map(j => j.last && j.last.id).filter(Boolean).sort((x, y) => y - x)[0]; showRun(last);
}

/* ---------- Chat ---------- */
async function viewChat(root) {
  root.append(h("h1", {}, "Copiloto"), h("p", {class: "sub"}, "Pregunta en lenguaje natural. El copiloto consulta datos reales (SQL, historial de pipelines, git, incidentes) y cita sus fuentes. No ejecuta nada: te propone acciones que tú pulsas."));
  let hist = []; try { hist = JSON.parse(sessionStorage.getItem("copilot.chat") || "[]"); } catch (_) {}
  let ctxRepo = ""; try { ctxRepo = sessionStorage.getItem("copilot.chat.repo") || ""; } catch (_) {}
  const list = h("div", {class: "chatlist"}), input = h("input", {class: "in", style: "flex:1", placeholder: "Ej: ¿qué repos fallan más? · ¿cómo está payments-api? · ¿cuántos faltan por migrar?", maxlength: "500", "aria-label": "Mensaje"});
  const send = h("button", {class: "btn pri"}, "Enviar"); let busy = false;
  const save = () => { try { sessionStorage.setItem("copilot.chat", JSON.stringify(hist.slice(-20))); sessionStorage.setItem("copilot.chat.repo", ctxRepo); } catch (_) {} };
  function cited(text, sources) {
    return text.split(/(\[\d+\])/).map(seg => { const m = seg.match(/^\[(\d+)\]$/); if (!m) return seg;
      const s = sources.find(x => x.id === +m[1]); return h("sup", {class: "cite", title: s ? s.title : "fuente inexistente", onclick: () => { const el = document.getElementById("src-" + m[1] + "-" + sources.uid); if (el) { el.open = true; el.scrollIntoView({block: "nearest"}); } }}, seg); });
  }
  function bubble(role, content) { const b = h("div", {class: "msg " + role}, h("div", {class: "who"}, role === "user" ? "Tú" : "Copiloto"), h("div", {class: "txt"}, content)); list.append(b); list.scrollTop = list.scrollHeight; return b; }
  hist.forEach(m => bubble(m.role, m.content));
  if (!hist.length) bubble("assistant", "Hola. Puedo contarte el estado de la migración, qué pipelines fallan, cómo va un repo concreto, o buscar en incidentes y documentos. También puedo proponerte acciones (reindexar, importar historial…) que ejecutas tú con un botón.");
  async function ask(text) {
    if (busy || !text.trim()) return; busy = true; send.disabled = true; input.value = "";
    bubble("user", text); hist.push({role: "user", content: text}); save();
    const b = bubble("assistant", ""), txt = b.querySelector(".txt"); txt.textContent = "Consultando datos…";
    let full = "", sources = [], got = false; sources.uid = Date.now();
    try {
      const r = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json", "X-Copilot": "1"}, body: JSON.stringify({message: text, history: hist.slice(0, -1).slice(-6), repo: ctxRepo})});
      if (!r.ok || !r.body) throw new Error("HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const {done, value} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true});
        let i; while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2); const m = block.match(/^event: (\w+)\ndata: (.*)$/s); if (!m) continue; const d = JSON.parse(m[2]);
          if (m[1] === "context") {
            sources = d.sources; sources.uid = Date.now(); if (d.repo) { ctxRepo = d.repo; }
            if (d.used.length) b.append(h("div", {class: "row small muted", style: "margin-top:8px"}, "Herramientas: ", d.used.map(u => h("span", {class: "pill"}, u))));
            b.append(h("div", {class: "srcs"}, sources.map(s => h("details", {class: "srcd", id: `src-${s.id}-${sources.uid}`}, h("summary", {}, `[${s.id}] ${s.title}`, h("span", {class: "muted small"}, ` · ${s.kind}`)), h("div", {class: "small muted", style: "white-space:pre-wrap;padding:6px 0"}, (s.source ? s.source + "\n" : "") + s.text)))));
            if (d.actions.length) b.append(h("div", {class: "row", style: "margin-top:10px"}, h("span", {class: "small muted"}, "Acciones propuestas (requieren tu clic):"), d.actions.map(ac => h("button", {class: "btn", onclick: () => startJob(ac.job, ac.params)}, "▶ " + ac.label))));
          } else if (m[1] === "token") { if (!got) { got = true; txt.textContent = ""; } full += d.t; }
          else if (m[1] === "done") { const v = d.verify; b.append(h("div", {class: "row small muted", style: "margin-top:8px"}, h("span", {class: "pill " + (v.ok ? "ok" : "warn")}, v.ok ? "citas y cifras verificadas" : "revisar: " + (v.invalid_citations.length ? "cita inexistente " : "") + (v.ungrounded_numbers.length ? "cifras sin respaldo: " + v.ungrounded_numbers.join(", ") : "pocas citas")), `${d.model} · ${d.gpu ? "GPU" : "CPU"} · ${fmtDur(d.ms)}`, h("span", {class: "muted small"}, "· la verificación no detecta errores de interpretación: contrasta con las fuentes")));
          } else if (m[1] === "fail") { txt.textContent = "No se pudo generar la respuesta: " + d.error; }
          if (got) { clear(txt); txt.append(...cited(full, sources)); }
          list.scrollTop = list.scrollHeight;
        }
      }
      hist.push({role: "assistant", content: full}); save();
    } catch (e) { txt.textContent = "Error: " + e.message; }
    busy = false; send.disabled = false; input.focus();
  }
  send.addEventListener("click", () => ask(input.value)); input.addEventListener("keydown", e => { if (e.key === "Enter") ask(input.value); });
  const ex = ["¿Qué repos fallan más en sus pipelines?", "¿Cuántos repos faltan por migrar a GitHub?", "¿Qué repos están sin actividad?", "Métricas DORA del portafolio", "¿Cómo está payments-api?", "Importa el historial de actions"];
  root.append(h("div", {class: "card chat"}, list, h("div", {class: "row", style: "margin-top:12px"}, input, send)),
    h("div", {class: "chips"}, ex.map(x => h("span", {class: "chip", role: "button", tabindex: "0", onclick: () => ask(x)}, x)),
      h("span", {class: "chip", role: "button", tabindex: "0", onclick: () => { hist = []; ctxRepo = ""; save(); route(); }}, "↺ nueva conversación")));
  input.focus();
}

/* ---------- project: real data tab ---------- */
function tabReal(b, d, env, reload) {
  const r = d.real, g = r.git, gh = r.github, dd = r.dora, dp = r.dora_prod;
  const kv = (k, v) => h("div", {class: "row spread", style: "padding:5px 0;border-bottom:1px dashed var(--line)"}, h("span", {class: "muted"}, k), h("b", {style: "text-align:right;word-break:break-all"}, v));
  const doraBox = (title, x) => h("div", {style: "margin-bottom:14px"}, h("div", {class: "muted small"}, title), x.deploys ? h("div", {}, kv("Despliegues (90 d)", x.deploys), kv("Fallidos", `${x.failures} (${(x.change_failure_rate * 100).toFixed(0)}%)`), kv("Frecuencia", x.per_week + " / semana"), kv("MTTR", x.mttr_hours == null ? "sin recuperación registrada" : x.mttr_hours + " h"), kv("Banda", x.band)) : h("div", {class: "muted"}, "Sin despliegues en el periodo."));
  b.append(h("div", {class: "grid g2"},
    h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, "Pipelines reales (GitHub Actions)"),
      h("button", {class: "btn", onclick: () => startJob("history_gh", {days: 120, repo: d.repo.name}, () => setTimeout(reload, 4000))}, "↻ Importar historial de este repo")),
      doraBox("Todos los entornos", dd), doraBox("Solo producción", dp), h("div", {class: "muted small"}, dd.note || ""),
      h("div", {style: "margin-top:10px"}, h("div", {class: "muted small"}, "Ejecuciones por semana (rojo = fallidas)"), sparkline(r.weekly))),
    h("div", {class: "card"}, h("h2", {}, "Git y GitHub"),
      gh ? kv("En GitHub", `${gh.visibility} · último push ${(gh.pushed || "").slice(0, 10)}`) : kv("En GitHub", "no aparece (¿migrado con otro nombre? ¿sin migrar?)"),
      g ? [kv("Commits totales", g.commits_total), kv("Commits en los 90 d previos al clon", g.commits_90d), kv("Autores en ese periodo", g.authors_90d), kv("Último commit", g.last_commit), kv("Fecha del clon local", g.snapshot)] : h("div", {class: "muted"}, "Sin estadísticas de git (ejecuta «Estadísticas de git» en Operaciones)."),
      h("div", {class: "muted small", style: "margin-top:8px"}, "Las cifras de git son relativas a la fecha del clon local, no a hoy.")),
    h("div", {class: "card", style: "grid-column:1/-1"}, h("h2", {}, "Últimas ejecuciones"),
      r.runs.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Fecha", "Workflow", "Rama", "Entorno", "Resultado", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, r.runs.map(x => h("tr", {}, h("td", {class: "num"}, (x.created || "").slice(0, 16).replace("T", " ")), h("td", {}, (x.workflow || "(no arrancó)").split("/").pop().slice(0, 44)), h("td", {class: "muted"}, x.branch),
          h("td", {}, x.env), h("td", {}, h("span", {class: "pill " + (x.conclusion === "success" ? "ok" : x.conclusion === "cancelled" ? "warn" : x.conclusion ? "bad" : "")}, x.conclusion || x.status)),
          h("td", {}, (x.url || "").startsWith("https://github.com/") ? h("a", {href: x.url, target: "_blank", rel: "noopener noreferrer", class: "muted"}, "abrir ↗") : "")))))
        : h("div", {class: "muted"}, "Sin ejecuciones importadas para este repo."))));
}

/* ---------- panel: real data cards ---------- */
function realCards(o) {
  const rl = o.real, m = rl.migration, d = rl.dora, pct = m.index_total ? Math.round(m.migrated / m.index_total * 100) : 0;
  return h("div", {class: "grid g2", style: "margin-top:16px"},
    h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, "Migración Azure → GitHub"), h("a", {href: "#/ops", class: "muted small"}, "actualizar inventario →")),
      m.github_total ? [h("div", {class: "kpi"}, h("div", {class: "v"}, `${m.migrated}/${m.index_total}`), h("div", {class: "n"}, `${pct}% de los repos del índice ya existen en GitHub (por nombre)`)),
        h("div", {style: "height:9px;margin:10px 0;background:var(--panel2);border-radius:9px;overflow:hidden"}, h("div", {style: `height:100%;width:${pct}%;background:linear-gradient(90deg,var(--acc),var(--acc2))`})),
        h("div", {class: "muted small"}, `${m.pending} sin equivalente en GitHub (pendientes o con otro nombre) · ${m.github_only} existen solo en GitHub · ${m.github_total} visibles para tu cuenta`)]
        : h("div", {class: "muted"}, "Inventario de GitHub sin cargar. Ejecútalo en Operaciones.")),
    h("div", {class: "card"}, h("div", {class: "row spread"}, h("h2", {}, "Pipelines reales (90 d)"), h("a", {href: "#/ops", class: "muted small"}, "importar →")),
      d.deploys ? [h("div", {class: "row", style: "gap:22px"}, h("div", {class: "kpi"}, h("div", {class: "v"}, d.deploys), h("div", {class: "n"}, "despliegues")), h("div", {class: "kpi"}, h("div", {class: "v"}, (d.change_failure_rate * 100).toFixed(0) + "%"), h("div", {class: "n"}, "fallan")), h("div", {class: "kpi"}, h("div", {class: "v"}, d.mttr_hours == null ? "—" : d.mttr_hours + " h"), h("div", {class: "n"}, "MTTR"))),
        h("div", {class: "muted small", style: "margin-top:8px"}, `${rl.runs} ejecuciones de ${rl.repos_with_runs} repos. Último dato: ${(rl.last_run || "").slice(0, 10)}. ${d.note}`)]
        : h("div", {class: "muted"}, "Sin historial. Ejecuta «Historial de GitHub Actions» en Operaciones.")),
    rl.failing.length ? h("div", {class: "card"}, h("h2", {}, "Repos con más fallos"), rl.failing.map(f => h("div", {class: "row spread", style: "padding:6px 0;cursor:pointer", onclick: () => location.hash = "#/project/" + encodeURIComponent(f.repo)},
      h("span", {}, f.repo), h("span", {class: "pill bad"}, `${f.failures + f.startup_failures}/${f.runs} · ${(f.rate * 100).toFixed(0)}%`)))) : null,
    rl.startup_window ? h("div", {class: "card", style: "border-color:rgba(251,191,36,.4)"}, h("h2", {}, "Posible causa común"), h("p", {style: "margin:0"}, `${rl.startup_window.runs} ejecuciones nunca arrancaron («startup_failure») en ${rl.startup_window.repos} repos entre ${rl.startup_window.from} y ${rl.startup_window.to}.`),
      h("p", {class: "muted small"}, "Al concentrarse en una ventana y en varios repos apunta a algo del org y no del código (hipótesis sin verificar: cuota o facturación de Actions). Revísalo en la configuración de GitHub.")) : null);
}

/* ---------- router ---------- */
const NAV = [["#/", "Panel", "home"], ["#/projects", "Proyectos", "grid"], ["#/chat", "Copiloto", "chat"], ["#/ask", "Buscar", "search"], ["#/ops", "Operaciones", "play"], ["#/how", "Cómo funciona", "cpu"]];
function drawNav() {
  const nav = $("#nav"); clear(nav);
  const cur = location.hash || "#/";
  NAV.forEach(([href, label, ic]) => nav.append(h("a", {href, class: (cur === href || (href === "#/projects" && cur.startsWith("#/project"))) ? "on" : ""}, svgIcon(ic), label)));
}
async function route() {
  drawNav();
  LIVE.handlers.clear();
  const root = $("#app"); clear(root);
  const hash = location.hash || "#/";
  try {
    if (hash.startsWith("#/project/")) await viewProject(root, decodeURIComponent(hash.slice(10)));
    else if (hash === "#/projects") await viewProjects(root);
    else if (hash === "#/ask") await viewAsk(root);
    else if (hash === "#/chat") await viewChat(root);
    else if (hash === "#/ops") await viewOps(root);
    else if (hash === "#/how") await viewHow(root);
    else await viewHome(root);
  } catch (e) { root.append(h("div", {class: "card empty"}, "Error: " + e.message)); }
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
connectLive();
route();
api("/api/overview").then(o => { $("#foot-stats").textContent = `${o.repos} repos · ${Object.values(o.chunks_by_kind).reduce((a, b) => a + b, 0)} fragmentos`; }).catch(() => {});

function refreshHealth() {
  api("/api/health").then(h_ => {
    const g = h_.generation, el = $("#ai-state"); if (!el) return;
    el.textContent = `IA: ${g.model} · ${g.gpu ? "GPU" : "CPU"} · emb. ${h_.embeddings.up ? "ok" : "CAÍDO (búsqueda sin semántica)"}`;
    el.style.color = h_.embeddings.up ? "" : "var(--warn)";
    el.title = `generación: ${g.backend} · embeddings: ${h_.embeddings.model}`;
  }).catch(() => {});
}
refreshHealth(); setInterval(refreshHealth, 30000);
