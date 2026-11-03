#!/usr/bin/env python3
"""Deploy Copilot (local). Never deploys: it only reads the index and prints a plan.

  plans.py ask "swap warm-up 502 timeout"    BM25 search with citations
  plans.py repo my-service-api               manifest + related evidence
  plans.py plan my-service-api --env prod
  plans.py record REPO --env prod --strategy slot-swap --result rollback --notes "..."
  plans.py list [--type function-dotnet]
"""
import argparse
import re
import textwrap

from common import connect

# strategy per detected type: (name, steps, rollback)
STRATEGIES = {
    "function-dotnet": ("slot-swap", [
        "Deploy a slot `staging` (no tráfico de prod)",
        "Warm-up: llamar health/endpoint ligero 3-5 min",
        "Smoke test contra el slot staging",
        "Swap staging <-> production",
        "Observar errores 5xx/latencia 15 min"], "Swap de vuelta (el slot anterior queda intacto)"),
    "function-node": ("slot-swap", [
        "Deploy a slot `staging`", "Warm-up + smoke test",
        "Swap staging <-> production", "Observar 15 min"], "Swap de vuelta"),
    "appservices-dotnet": ("slot-swap", [
        "Deploy a slot `staging`", "Warm-up 3-5 min (conexiones SQL/pool)",
        "Smoke test", "Swap", "Observar 5xx/SNAT 15 min"], "Swap de vuelta"),
    "container": ("revision-traffic-split", [
        "Crear nueva revisión (ACA) sin tráfico",
        "Health check de la revisión", "Split 10% -> observar",
        "Split 50% -> observar", "100% y desactivar revisión anterior"],
        "Mover 100% del tráfico a la revisión anterior"),
    "terraform": ("plan-review-apply", [
        "`terraform plan` y guardar el plan", "Revisión humana del plan (destroy/replace = bloqueo)",
        "`terraform apply` del plan aprobado", "Verificar recursos"],
        "Revertir commit y re-aplicar; blue/green no aplica"),
    "frontend-angular": ("preview-promote", [
        "Build + deploy a preview/staging", "Smoke test UI", "Promover a producción"],
        "Re-promover build anterior"),
    "frontend-or-node": ("preview-promote", [
        "Build + deploy a preview/staging", "Smoke test", "Promover a producción"],
        "Re-promover build anterior"),
    "mobile-react-native": ("staged-store-rollout", [
        "Build firmado (CI)", "Beta interna", "Rollout escalonado 5% -> 20% -> 100%"],
        "Detener rollout; blue/green no aplica"),
    "mobile-native": ("staged-store-rollout", [
        "Build firmado (CI)", "Beta interna", "Rollout escalonado"],
        "Detener rollout; blue/green no aplica"),
}
DEFAULT_STRATEGY = ("manual-review", ["Tipo no detectado: definir estrategia a mano"], "n/d")
RISK_WORDS = re.compile(r"(?i)\b(rollback|502|500|5xx|timeout|snat|warm|swap|incidente|fall[oó]|ca[ií]da|p[eé]rdida)\b")


def fts_query(text: str) -> str:
    toks = re.findall(r"[\wáéíóúñ.-]{2,}", text.lower())
    return " OR ".join(f'"{t}"' for t in toks) or '""'


def search(con, q, limit=6, kind=None, repo=None):
    sql = ("SELECT kind, repo, source, title, snippet(chunks,0,'»','«','…',24) AS snip, "
           "bm25(chunks, 1.0, 3.0) AS score FROM chunks WHERE chunks MATCH ?")
    args = [fts_query(q)]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if repo:
        sql += " AND repo = ?"
        args.append(repo)
    sql += " ORDER BY score LIMIT ?"
    args.append(limit)
    return con.execute(sql, args).fetchall()


def show_hits(hits):
    if not hits:
        print("  (sin evidencia)")
    for i, h in enumerate(hits, 1):
        snip = re.sub(r"\s+", " ", h["snip"])
        print(f"  [{i}] ({h['kind']}) {h['source']} — {h['title'][:60]}")
        print(textwrap.indent(textwrap.fill(snip, 100), "      "))


def get_repo(con, name):
    r = con.execute("SELECT * FROM repos WHERE lower(name)=lower(?)", (name,)).fetchone()
    if r:
        return r
    like = con.execute("SELECT name FROM repos WHERE name LIKE ? LIMIT 8", (f"%{name}%",)).fetchall()
    msg = "Repo no encontrado: " + name
    if like:
        msg += ". ¿Quisiste decir? " + ", ".join(x["name"] for x in like)
    raise SystemExit(msg)


def cmd_ask(con, a):
    print(f"Q: {a.query}\n")
    show_hits(search(con, a.query, a.n, a.kind))


def cmd_list(con, a):
    sql, args = "SELECT name,type,branch,workflow,last_commit FROM repos", []
    if a.type:
        sql += " WHERE type=?"
        args.append(a.type)
    for r in con.execute(sql + " ORDER BY name", args):
        print(f"{r['name']:<48} {r['type']:<20} {r['branch']:<8} {r['workflow'] or '-':<28} {r['last_commit']}")


def cmd_repo(con, a):
    r = get_repo(con, a.name)
    print(f"# {r['name']}")
    for k in r.keys():
        print(f"  {k:<16} {r[k]}")
    print("\nEvidencia relacionada:")
    show_hits(search(con, r["name"], 5))


def policy_checks(r, env):
    """Deterministic policy-as-code. LLM never decides these."""
    out = []
    ok = lambda c, msg: out.append(("PASS" if c else "FAIL", msg))  # noqa: E731
    warn = lambda c, msg: out.append(("WARN", msg)) if c else None  # noqa: E731
    ok(bool(r["has_workflow"]), "Tiene workflow/pipeline de CI/CD definido")
    ok(r["type"] != "unknown", "Tipo de repo detectado")
    warn(r["csv_type"] and r["type"] != "unknown" and r["csv_type"].split("-")[0] != r["type"].split("-")[0],
         f"Tipo detectado ({r['type']}) difiere del CSV ({r['csv_type']}): confirmar")
    warn(r["az_diverged"], f"`{r['branch']}` difiere de su rama Azure (_az): confirmar cuál es la fuente de verdad antes de desplegar")
    warn(r["has_lfs"], "Usa Git LFS: verificar que el runner tenga `git lfs` instalado")
    warn(r["has_terraform"] and r["type"] != "terraform", "Contiene Terraform además del código: revisar cambios de infra")
    if env == "prod":
        out.append(("REQ", "Aprobación humana obligatoria antes de producción"))
        out.append(("REQ", "Secretos solo en gestor de secretos / variables de entorno del environment"))
    return out


def cmd_plan(con, a):
    r = get_repo(con, a.name)
    strat, steps, rollback = STRATEGIES.get(r["type"], DEFAULT_STRATEGY)
    print(f"# Plan de despliegue — {r['name']} → {a.env}")
    print(f"Tipo: {r['type']} | Rama base: {r['branch']} | Workflow: {r['workflow'] or 'ninguno'}")
    print(f"Estrategia: {strat}\n")
    print("Pasos:")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")
    print(f"\nRollback: {rollback}\n")
    print("Políticas:")
    checks = policy_checks(r, a.env)
    for lvl, msg in checks:
        print(f"  [{lvl}] {msg}")
    blocked = any(l == "FAIL" for l, _ in checks)

    hist = con.execute("SELECT env,strategy,result,notes,ts FROM deploys WHERE repo=? ORDER BY id DESC LIMIT 20",
                       (r["name"],)).fetchall()
    bad = [h for h in hist if h["result"] != "ok"]
    print("\nHistorial propio:")
    if hist:
        print(f"  {len(hist)} despliegues, {len(bad)} no-OK. Último: {hist[0]['result']} ({hist[0]['ts']})")
        for h in bad[:3]:
            print(f"   - {h['ts']} {h['env']} {h['strategy']} {h['result']}: {h['notes']}")
    else:
        print("  sin registros (usa `record` tras cada despliegue para que el riesgo mejore)")

    print("\nEvidencia (incidentes/docs) relacionada:")
    hits = [h for h in search(con, f"{r['name']} {strat} rollback 502 timeout", 20)
            if h["kind"] in ("incident", "doc", "workflow") and (
                RISK_WORDS.search(h["snip"]) or r["name"].lower() in h["source"].lower())][:4]
    show_hits(hits)

    score = len([c for c in checks if c[0] == "FAIL"]) * 3 + len([c for c in checks if c[0] == "WARN"]) + len(bad) * 2
    level = "ALTO" if blocked or score >= 6 else "MEDIO" if score >= 3 else "BAJO"
    print(f"\nRiesgo: {level} (puntaje {score}). {'BLOQUEADO por política.' if blocked else ''}")
    print("Nota: este plan no ejecuta nada. Revisión humana antes de producción.")


def cmd_record(con, a):
    get_repo(con, a.repo)
    con.execute("INSERT INTO deploys(repo,env,strategy,result,notes) VALUES(?,?,?,?,?)",
                (a.repo, a.env, a.strategy, a.result, a.notes))
    con.commit()
    print("registrado")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask"); s.add_argument("query"); s.add_argument("-n", type=int, default=6)
    s.add_argument("--kind", choices=["incident", "doc", "workflow", "readme", "script", "manifest"])
    s.set_defaults(f=cmd_ask)
    s = sub.add_parser("list"); s.add_argument("--type"); s.set_defaults(f=cmd_list)
    s = sub.add_parser("repo"); s.add_argument("name"); s.set_defaults(f=cmd_repo)
    s = sub.add_parser("plan"); s.add_argument("name"); s.add_argument("--env", default="prod", choices=["dev", "qa", "prod"])
    s.set_defaults(f=cmd_plan)
    s = sub.add_parser("record"); s.add_argument("repo"); s.add_argument("--env", required=True)
    s.add_argument("--strategy", required=True); s.add_argument("--result", required=True, choices=["ok", "rollback", "failed"])
    s.add_argument("--notes", default=""); s.set_defaults(f=cmd_record)
    a = p.parse_args()
    a.f(connect(), a)


if __name__ == "__main__":
    main()
