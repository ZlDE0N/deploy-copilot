#!/usr/bin/env python3
"""Local web UI + JSON API. Binds to 127.0.0.1 only. Never deploys anything.

Hardening: Host header allow-list (DNS rebinding), custom header on POST (CSRF), strict CSP,
static files served only from ./ui, no directory traversal, no secrets in responses (index is redacted)."""
import json
import mimetypes
import os
import sys
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import evaluate
import gen
import embed
import ingest
from events import Bus, Watcher
from common import connect
from plans import DEFAULT_STRATEGY, STRATEGIES, policy_checks, search as fts_search
import chat
import jobs
import llm
import signals
import structured
from rag import Index, extractive_answer, stream_local_answer, verify_citations

UI = Path(__file__).parent / "ui"
PORT = int(os.environ.get("COPILOT_PORT", "8765"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
IX = Index()
BUS = Bus()
REINDEX_LOCK = threading.Lock()
LOCK = threading.Lock()                 # sqlite connection per request, but Index.con is shared
EVAL_CACHE = {}


def reindex(log=None, force_embed=False):
    """Re-ingest sources, embed only new fragments (all of them if force_embed) and rebuild the in-memory index."""
    log = log or (lambda m: None)
    if not REINDEX_LOCK.acquire(blocking=False):
        log("Ya hay un reindexado en curso")
        return {"skipped": True}
    try:
        BUS.publish("reindex:start")
        t0, before, emb = time.time(), IX.stats["chunks"], {"embedded": 0}
        with IX.lock:
            log("Leyendo fuentes (repos, pipelines, incidentes, docs)…")
            ingest.main()
            if embed.available():
                try:
                    if force_embed:
                        log("Re-embebiendo TODO (bloquea las búsquedas varios minutos)…")
                        connect().execute("DELETE FROM vecs").connection.commit()
                    emb = embed.ensure(progress=lambda d, n: log(f"  embeddings {d}/{n}") if d == n or d % 160 == 0 else None)
                except Exception as e:  # noqa: BLE001 - keep keyword retrieval alive if Ollama fails
                    BUS.publish("error", {"where": "embed", "msg": f"{type(e).__name__}: {e}"})
                    log(f"Embeddings no disponibles: {type(e).__name__}")
            else:
                log("Ollama no responde: se omiten los embeddings")
            IX.build()
        EVAL_CACHE.clear()
        out = {"chunks": IX.stats["chunks"], "delta": IX.stats["chunks"] - before, "embedded": emb.get("embedded", 0),
               "semantic": IX.stats.get("semantic_vectors", 0), "ms": int((time.time() - t0) * 1000)}
        BUS.publish("reindex:done", out)
        log(f"Listo: {out['chunks']} fragmentos ({out['delta']:+d}), {out['embedded']} embebidos, {out['ms']} ms")
        return out
    finally:
        REINDEX_LOCK.release()


def db():
    return connect()


def repo_row(con, name):
    return con.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()


def risk_for(con, r, env):
    checks = policy_checks(r, env)
    bad = con.execute("SELECT COUNT(*) FROM deploys WHERE repo=? AND result!='ok'", (r["name"],)).fetchone()[0]
    score = sum(3 for c in checks if c[0] == "FAIL") + sum(1 for c in checks if c[0] == "WARN") + bad * 2
    d = signals.dora(con, r["name"], "all", 90)
    if d["deploys"] >= 5 and d["change_failure_rate"] >= 0.3:
        checks.append(("WARN", f"Tasa de fallo de pipelines alta en 90 d: {d['failures']}/{d['deploys']} ({d['change_failure_rate']*100:.0f}%)"))
        score += 3 if d["change_failure_rate"] < 0.5 else 6
    blocked = any(c[0] == "FAIL" for c in checks)
    return {"checks": [{"level": a, "msg": b} for a, b in checks], "score": score, "blocked": blocked,
            "level": "ALTO" if blocked or score >= 6 else "MEDIO" if score >= 3 else "BAJO"}


def real_summary(con):
    m = signals.migration_status(con)
    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT repo), MAX(created) FROM runs").fetchone()
    return {"runs": n[0], "repos_with_runs": n[1], "last_run": n[2], "dora": signals.dora(con, None, "all", 90),
            "failing": signals.top_failing(con, 90, 5, 5), "startup_window": signals.startup_window(con),
            "migration": {k: v for k, v in m.items() if not k.endswith("_repos")}}


def api_overview(_q):
    con = db()
    rows = con.execute("SELECT * FROM repos").fetchall()
    by_type, pending, az, lfs, ready = {}, 0, 0, 0, {"listo": 0, "casi": 0, "trabajo": 0}
    scores = []
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        pending += 1 if r["workflow"] and "azure-pipelines" in r["workflow"] else 0
        az += 1 if r["az_diverged"] else 0
        lfs += r["has_lfs"]
        rd = gen.readiness(con, r)
        ready[rd["level"]] += 1
        scores.append((rd["score"], r["name"], r["type"]))
    scores.sort(reverse=True)
    kinds = {x["kind"]: x["n"] for x in con.execute("SELECT kind, COUNT(*) n FROM chunks GROUP BY kind")}
    n_dep = con.execute("SELECT COUNT(*) FROM deploys").fetchone()[0]
    return {"repos": len(rows), "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "pipelines_pending_actions": pending, "with_az_branches": az, "with_lfs": lfs, "readiness": ready,
            "avg_readiness": round(sum(s for s, _, _ in scores) / max(len(scores), 1)),
            "chunks_by_kind": kinds, "deploys_recorded": n_dep, "live_clients": BUS.clients(), "real": real_summary(con),
            "top_ready": [{"name": n, "score": s, "type": t} for s, n, t in scores[:6]],
            "least_ready": [{"name": n, "score": s, "type": t} for s, n, t in scores[-6:]]}


def api_repos(q):
    con = db()
    out = []
    for r in con.execute("SELECT * FROM repos ORDER BY name"):
        rd = gen.readiness(con, r)
        out.append({"name": r["name"], "type": r["type"], "branch": r["branch"], "workflow": r["workflow"],
                    "last_commit": r["last_commit"], "project": r["org_project"], "readiness": rd["score"],
                    "level": rd["level"], "az": bool(r["az_diverged"]), "lfs": bool(r["has_lfs"]),
                    "strategy": STRATEGIES.get(r["type"], DEFAULT_STRATEGY)[0]})
    return out


def api_repo(q):
    name, env = q.get("name", [""])[0], q.get("env", ["prod"])[0]
    if env not in ("dev", "qa", "prod"):
        return {"error": "env inválido"}, 400
    con = db()
    r = repo_row(con, name)
    if not r:
        return {"error": "repo no encontrado"}, 404
    strat, steps, rollback = STRATEGIES.get(r["type"], DEFAULT_STRATEGY)
    ev = IX.search(f"{r['name']} {strat} rollback incidente timeout", 6, repo=None)["hits"]
    ev = [h for h in ev if h["kind"] in ("incident", "doc", "workflow") or h["repo"] == r["name"]][:4]
    hist = [dict(x) for x in con.execute("SELECT env,strategy,result,notes,ts FROM deploys WHERE repo=? ORDER BY id DESC LIMIT 20", (name,))]
    return {"repo": {k: r[k] for k in r.keys()}, "strategy": strat, "steps": steps, "rollback": rollback,
            "risk": risk_for(con, r, env), "readiness": gen.readiness(con, r), "workflow": gen.build_workflow(r, env),
            "importer": gen.importer_hint(r), "runbook": gen.runbook(r, env), "checklist": gen.checklist(r, env),
            "dora": gen.dora(con, name), "history": hist,
            "real": {"dora": signals.dora(con, name, "all", 90), "dora_prod": signals.dora(con, name, "prod", 90),
                     "runs": signals.recent_runs(con, name, 25), "weekly": signals.weekly(con, name, 16),
                     "git": ({k: g[k] for k in g.keys()} if (g := con.execute("SELECT * FROM gitstats WHERE repo=?", (name,)).fetchone()) else None),
                     "github": ({k: gh[k] for k in gh.keys()} if (gh := con.execute("SELECT * FROM gh_repos WHERE name=?", (name,)).fetchone()) else None)},
            "evidence": [{k: h[k] for k in ("kind", "source", "title", "snippet")} for h in ev]}


def api_search(q):
    text = q.get("q", [""])[0][:300]
    if not text.strip():
        return {"error": "consulta vacía"}, 400
    kind = q.get("kind", [None])[0] or None
    if kind not in (None, "incident", "doc", "workflow", "readme", "script", "manifest"):
        return {"error": "kind inválido"}, 400
    res = IX.search(text, 8, kind=kind)
    exact = structured.answer(text)
    ans = extractive_answer(text, res["hits"], IX.tidf)
    for h in res["hits"]:
        h.pop("_body", None)
    return {"answer": ans, "structured": exact, **res}


def api_eval(_q):
    if not EVAL_CACHE:
        EVAL_CACHE.update(evaluate.run(IX))
    return EVAL_CACHE


def api_waves(_q):
    con = db()
    waves = {"Ola 1 · listos (≥70)": [], "Ola 2 · casi (45-69)": [], "Ola 3 · trabajo previo (<45)": []}
    for r in con.execute("SELECT * FROM repos ORDER BY name"):
        rd = gen.readiness(con, r)
        key = list(waves)[0] if rd["score"] >= 70 else list(waves)[1] if rd["score"] >= 45 else list(waves)[2]
        waves[key].append({"name": r["name"], "type": r["type"], "score": rd["score"]})
    return {k: sorted(v, key=lambda x: -x["score"]) for k, v in waves.items()}


def api_record(body):
    con = db()
    if not repo_row(con, body.get("repo", "")):
        return {"error": "repo no encontrado"}, 404
    if body.get("result") not in ("ok", "rollback", "failed") or body.get("env") not in ("dev", "qa", "prod"):
        return {"error": "parámetros inválidos"}, 400
    con.execute("INSERT INTO deploys(repo,env,strategy,result,notes) VALUES(?,?,?,?,?)",
                (body["repo"], body["env"], str(body.get("strategy", ""))[:60], body["result"], str(body.get("notes", ""))[:500]))
    con.commit()
    BUS.publish("deploy:recorded", {"repo": body["repo"], "env": body["env"], "result": body["result"]})
    return {"ok": True}


def job_reindex(ctx):
    return reindex(ctx.log, bool(ctx.params.get("force_embed")))


def job_evaluate(ctx):
    ctx.log("Evaluando las preguntas de oro con BM25, n-gramas, semántico e híbrido…")
    res = evaluate.run(IX)
    EVAL_CACHE.clear(); EVAL_CACHE.update(res)
    metrics = {k: {"recall@5": res[k]["recall@5"], "mrr": res[k]["mrr"]} for k in ("bm25", "ngram", "semantic", "hybrid")}
    con = connect()
    prev = con.execute("SELECT metrics FROM eval_runs ORDER BY id DESC LIMIT 1").fetchone()
    for k, v in metrics.items():
        old = (json.loads(prev["metrics"]).get(k) or {}) if prev else {}
        if v["mrr"] is None:
            ctx.log(f"  {k:<9} NO DISPONIBLE ({res[k].get('unavailable')}): no se registra como 0")
        else:
            ctx.log(f"  {k:<9} recall@5={v['recall@5']} mrr={v['mrr']}" + (f"  (antes mrr={old['mrr']})" if old.get("mrr") is not None else ""))
    if any(v["mrr"] is None for v in metrics.values()):
        raise RuntimeError("Evaluación incompleta: un recuperador no está disponible (ver log). No se guarda como resultado válido.")
    con.execute("INSERT INTO eval_runs(metrics) VALUES(?)", (json.dumps(metrics),)); con.commit()
    return metrics


def job_secrets(ctx):
    import shutil
    import subprocess
    import tempfile
    gl = shutil.which("gitleaks")
    if not gl:
        raise RuntimeError("gitleaks no está instalado")
    con = connect()
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/index.txt", "w").write("\n".join(r[0] for r in con.execute("SELECT body FROM chunks")))
        out = f"{d}/r.json"
        subprocess.run([gl, "detect", "--no-git", "--source", d, "--redact", "-f", "json", "-r", out], capture_output=True, timeout=300)
        found = json.load(open(out)) if os.path.exists(out) else []
    by_rule = {}
    for f in found:
        by_rule[f["RuleID"]] = by_rule.get(f["RuleID"], 0) + 1
    ctx.log(f"{len(found)} hallazgos en el texto del índice: {by_rule or 'ninguno'}")
    if found:
        ctx.log("Revisa si son identificadores benignos (p. ej. projectKey de Sonar, ejemplos curl localhost) o secretos reales.")
    return {"findings": len(found), "by_rule": by_rule}


def job_health(ctx):
    st = llm.status()
    con = connect()
    out = {"generation": st["generation"]["backend"], "model": st["generation"]["model"], "gpu": st["generation"]["gpu"],
           "embeddings_up": st["embeddings"]["up"], "chunks": con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
           "vectors": con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0], "runs": con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]}
    for k, v in out.items():
        ctx.log(f"  {k}: {v}")
    return out


_DAYS = [{"key": "days", "label": "Días hacia atrás", "type": "int", "min": 7, "max": 730, "default": 120}]
jobs.BUS = BUS
jobs.register("reindex", "Reindexar fuentes", "Relee repos, pipelines, incidentes y docs; embebe solo lo nuevo.", job_reindex,
              [{"key": "force_embed", "label": "Re-embeber todo (lento, bloquea búsquedas ~10 min)", "type": "bool", "default": False}])
jobs.register("github_inventory", "Inventario de GitHub", "Lista los repos del org visibles para tu cuenta y calcula el estado de migración.", signals.github_inventory)
jobs.register("history_gh", "Historial de GitHub Actions", "Importa ejecuciones de pipelines (solo lectura vía gh) de todo el org.", signals.history_gh,
              _DAYS + [{"key": "repo", "label": "Solo este repo (opcional)", "type": "text", "default": ""}])
jobs.register("history_azure", "Historial de Azure DevOps (sin probar)", "Requiere AZURE_DEVOPS_EXT_PAT de solo lectura en el entorno del servidor.", signals.history_azure, _DAYS)
jobs.register("gitstats", "Estadísticas de git", "Commits, autores y actividad de los clones locales (relativo a la fecha del clon).", signals.gitstats)
jobs.register("evaluate", "Evaluar recuperación", "Recall@5 y MRR de los 4 recuperadores; guarda el resultado y lo compara con el anterior.", job_evaluate)
jobs.register("secrets_scan", "Escanear secretos del índice", "Ejecuta gitleaks sobre el texto indexado.", job_secrets)
jobs.register("health", "Comprobar salud", "Estado de la IA local, embeddings y datos.", job_health)
jobs.mark_stale()

WATCH = Watcher(BUS, reindex)
def api_health(_q):
    return llm.status()


def api_jobs(_q):
    return jobs.listing()


def api_job_runs(q):
    return jobs.runs(q.get("job", [None])[0] or None, 20)


def api_job_run(q):
    try:
        d = jobs.run_detail(int(q.get("id", ["0"])[0]))
    except ValueError:
        return {"error": "id inválido"}, 400
    return d or ({"error": "no encontrado"}, 404)


def api_migration(_q):
    return signals.migration_status(db())


def api_evals(_q):
    return [{"ts": r["ts"], "metrics": json.loads(r["metrics"])} for r in db().execute("SELECT ts, metrics FROM eval_runs ORDER BY id DESC LIMIT 20")]


GET = {"/api/jobs": api_jobs, "/api/jobs/runs": api_job_runs, "/api/jobs/run": api_job_run, "/api/migration": api_migration,
       "/api/evals": api_evals, "/api/health": api_health, "/api/overview": api_overview, "/api/repos": api_repos, "/api/repo": api_repo,
       "/api/search": api_search, "/api/eval": api_eval, "/api/waves": api_waves}
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class H(BaseHTTPRequestHandler):
    server_version = "DeployCopilot"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _host_ok(self):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self._json({"error": "host no permitido"}, 403)
            return False
        return True

    def do_GET(self):
        if not self._host_ok():
            return
        u = urlparse(self.path)
        if u.path.startswith("/api/") and u.path != "/api/events" and self.headers.get("X-Copilot") != "1":
            return self._json({"error": "cabecera requerida"}, 403)   # blocks cross-site GETs (custom header forces CORS preflight)
        if u.path == "/api/events":
            return self._sse()
        if u.path == "/api/answer":
            return self._answer(parse_qs(u.query))
        if u.path in GET:
            try:
                res = GET[u.path](parse_qs(u.query))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            code = 200
            if isinstance(res, tuple):
                res, code = res
            return self._json(res, code)
        rel = unquote(u.path).lstrip("/") or "index.html"
        f = (UI / rel).resolve()
        if UI.resolve() not in f.parents or not f.is_file():
            return self._json({"error": "no encontrado"}, 404)
        self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

    def _answer(self, q):
        """Streamed RAG answer: sources first, then model tokens, then done. Server-Sent Events over fetch."""
        text = q.get("q", [""])[0][:300].strip()
        kind = q.get("kind", [None])[0] or None
        if not text or kind not in (None, "incident", "doc", "workflow", "readme", "script", "manifest"):
            return self._json({"error": "consulta inválida"}, 400)
        res = IX.search(text, 8, kind=kind)
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                     ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff")):
            self.send_header(k, v)
        self.end_headers()

        def emit(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        try:
            pub = [{k: h[k] for k in ("kind", "repo", "source", "title", "score", "snippet", "why")} for h in res["hits"]]
            exact = structured.answer(text)
            emit("sources", {"hits": pub, "trace": res["trace"], "structured": exact,
                             "extractive": extractive_answer(text, res["hits"], IX.tidf)})
            t0, n, full = time.time(), 0, []
            first = None
            for delta in stream_local_answer(text, res["hits"], structured.as_fact(exact) if exact else ""):
                n += 1
                first = first or time.time() - t0
                full.append(delta)
                emit("token", {"t": delta})
            g = llm.status()["generation"]
            emit("done", {"tokens": n, "first_token_ms": int((first or 0) * 1000), "ms": int((time.time() - t0) * 1000),
                          "model": g["model"], "backend": g["backend"], "gpu": g["gpu"],
                          "verify": verify_citations("".join(full), res["hits"])})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001
            try:
                emit("fail", {"error": f"{type(e).__name__}: {e}"})
            except OSError:
                pass

    def _sse(self):
        try:
            last = int(self.headers.get("Last-Event-ID", "0") or 0)
        except ValueError:
            last = 0
        q = BUS.subscribe(last)
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"), ("Connection", "keep-alive"),
                     ("X-Accel-Buffering", "no"), ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff")):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    msg = f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)

    def _chat(self, body):
        msg = str(body.get("message", "")).strip()[:500]
        if not msg:
            return self._json({"error": "mensaje vacío"}, 400)
        hist = body.get("history") if isinstance(body.get("history"), list) else []
        prep = chat.prepare(IX, msg, hist[-8:], str(body.get("repo", ""))[:120])
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                     ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff")):
            self.send_header(k, v)
        self.end_headers()

        def emit(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        try:
            emit("context", {"sources": [{"id": s["id"], "title": s["title"], "kind": s["kind"], "source": s.get("source", ""), "text": s["text"][:600]} for s in prep["sources"]],
                             "actions": prep["actions"], "repo": prep["repo"], "used": prep["used"], "ambiguous": prep["ambiguous"]})
            t0, full, first = time.time(), [], None
            for delta in llm.stream(prep["messages"], max_tokens=420):
                first = first or time.time() - t0
                full.append(delta)
                emit("token", {"t": delta})
            g = llm.status()["generation"]
            emit("done", {"model": g["model"], "gpu": g["gpu"], "first_token_ms": int((first or 0) * 1000), "ms": int((time.time() - t0) * 1000),
                          "verify": chat.verify("".join(full), prep["sources"])})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001
            try:
                emit("fail", {"error": f"{type(e).__name__}: {e}"})
            except OSError:
                pass

    def do_POST(self):
        if not self._host_ok():
            return
        if self.headers.get("X-Copilot") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._json({"error": "cabecera requerida"}, 403)
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 30_000:
                return self._json({"error": "cuerpo demasiado grande"}, 413)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, TypeError):
            return self._json({"error": "JSON inválido"}, 400)
        if path == "/api/chat":
            return self._chat(body)
        if path == "/api/reindex":
            WATCH.trigger.set()
            return self._json({"ok": True})
        if path == "/api/jobs/start":
            try:
                return self._json({"ok": True, "id": jobs.start(str(body.get("job", "")), body.get("params") or {})})
            except KeyError:
                return self._json({"error": "trabajo desconocido"}, 404)
            except (RuntimeError, ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 409 if isinstance(e, RuntimeError) else 400)
        if path == "/api/jobs/cancel":
            return self._json({"ok": jobs.cancel(str(body.get("job", "")))})
        if path != "/api/record":
            return self._json({"error": "no encontrado"}, 404)
        res = api_record(body)
        code = 200
        if isinstance(res, tuple):
            res, code = res
        self._json(res, code)


if __name__ == "__main__":
    WATCH.start()
    threading.Thread(target=llm.warm, daemon=True).start()
    BUS.publish("server:ready", {"chunks": IX.stats["chunks"]})
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    print(f"Deploy Copilot → http://127.0.0.1:{PORT}  (chunks={IX.stats['chunks']}, solo local)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
