"""Real operational signals: git history of every repo (local) and GitHub Actions runs (gh CLI, read-only).
Nothing here deploys or writes outside the local SQLite DB. Auth comes from the user's own `gh` session."""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from common import WORK, connect

GH_ORG = os.environ.get("COPILOT_GH_ORG", "")      # GitHub organisation or user whose repositories are read
AZDO_ORG = os.environ.get("COPILOT_AZDO_ORG", "")  # Azure DevOps organisation (only for the Azure history job)


def require_org():
    if not GH_ORG:
        raise RuntimeError("Define COPILOT_GH_ORG (organización o usuario de GitHub) en el entorno del servidor o en .env")
    return GH_ORG
DEPLOY_RX = re.compile(r"deploy|despleg|release|publish", re.I)
ENV_RULES = [("preprod", r"pre-?prod|staging|\bstg\b"), ("prod", r"\bprod(uction)?\b|producci"), ("qa", r"\bqa\b"), ("dev", r"\bdev(elop)?\b")]
BRANCH_ENV = {"main": "prod", "master": "prod", "qa": "qa", "develop": "dev", "dev": "dev"}


def guess_env(workflow: str, branch: str) -> str:
    t = f"{workflow} {branch}".lower()
    for env, rx in ENV_RULES:
        if re.search(rx, t):
            return env
    return BRANCH_ENV.get((branch or "").lower(), "unknown")


# ----------------------------------------------------------------- git (local, no network)
def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace", timeout=90)
    return r.stdout if r.returncode == 0 else ""


def _repo_stats(path):
    br = _git(path, "for-each-ref", "--sort=-committerdate", "--count=1", "--format=%(refname)", "refs/heads").strip()
    if not br:
        return None
    snap = datetime.fromtimestamp((path / "HEAD").stat().st_mtime)          # clones are snapshots: measure relative to it
    total = int((_git(path, "rev-list", "--count", br).strip() or 0))
    since = (snap - timedelta(days=90)).strftime("%Y-%m-%d")
    recent = _git(path, "log", br, f"--since={since}", "--format=%ae").split()
    last = _git(path, "log", "-1", "--format=%cs", br).strip()
    first = _git(path, "log", br, "--reverse", "--format=%cs", "--max-parents=0").split("\n")[0].strip()
    days = (snap - datetime.strptime(last, "%Y-%m-%d")).days if last else None
    return (path.name, snap.strftime("%Y-%m-%d"), total, len(recent), len(set(recent)), last, days, first)


def gitstats(ctx):
    con = connect()
    repos = sorted(p for p in (WORK / "migration").iterdir() if p.is_dir() and (p / "HEAD").exists())
    ctx.log(f"Analizando historial git de {len(repos)} repos (local)")
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(_repo_stats, repos):
            done += 1
            if ctx.cancelled():
                ctx.log("Cancelado")
                break
            if res:
                con.execute("INSERT OR REPLACE INTO gitstats VALUES(?,?,?,?,?,?,?,?,datetime('now'))", res)
            ctx.progress(done, len(repos))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM gitstats").fetchone()[0]
    stale = con.execute("SELECT COUNT(*) FROM gitstats WHERE days_since>365").fetchone()[0]
    one = con.execute("SELECT COUNT(*) FROM gitstats WHERE commits_90d>0 AND authors_90d=1").fetchone()[0]
    ctx.log(f"{n} repos analizados (relativo a la fecha del clon local) · {stale} sin commits en el año previo al clon · {one} con un único autor en los 90 d previos")
    return {"repos": n, "stale_1y": stale, "single_author_90d": one}


# ----------------------------------------------------------------- GitHub inventory (migration status)
def github_inventory(ctx):
    p = subprocess.run(["gh", "repo", "list", require_org(), "--limit", "1000", "--json", "name,pushedAt,isArchived,visibility"],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:200])
    repos = json.loads(p.stdout)
    con = connect()
    con.execute("DELETE FROM gh_repos")
    for r in repos:
        con.execute("INSERT OR REPLACE INTO gh_repos VALUES(?,?,?,?,datetime('now'))", (r["name"], r["pushedAt"], int(r["isArchived"]), r["visibility"]))
    con.commit()
    st = migration_status(con)
    ctx.log(f"{st['github_total']} repos visibles en GitHub · {st['migrated']} del índice ya están allí · "
            f"{st['pending']} del índice no aparecen · {st['github_only']} existen solo en GitHub")
    return st


def migration_status(con):
    idx = {r[0].lower(): r[0] for r in con.execute("SELECT name FROM repos")}
    gh = {r[0].lower(): r for r in con.execute("SELECT name, pushed, visibility FROM gh_repos")}
    both = set(idx) & set(gh)
    return {"index_total": len(idx), "github_total": len(gh), "migrated": len(both), "pending": len(set(idx) - set(gh)),
            "github_only": len(set(gh) - set(idx)),
            "pending_repos": sorted(idx[k] for k in set(idx) - set(gh))[:200],
            "github_only_repos": sorted(gh[k][0] for k in set(gh) - set(idx))[:200]}


# ----------------------------------------------------------------- GitHub Actions (gh, read-only)
def _gh_runs(repo, since, max_pages):
    rows, missing = [], False
    for page in range(1, max_pages + 1):
        p = subprocess.run(["gh", "api", "--method", "GET", f"repos/{require_org()}/{repo}/actions/runs",
                            "-f", "per_page=100", "-f", f"page={page}", "-f", f"created=>={since}",
                            "--jq", ".workflow_runs[] | {id,name,head_branch,event,status,conclusion,created_at,run_started_at,updated_at,html_url}"],
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            missing = "404" in p.stderr or "Not Found" in p.stderr
            if not missing and page == 1:
                raise RuntimeError(p.stderr.strip()[:200])
            break
        batch = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        rows += batch
        if len(batch) < 100:
            break
    return repo, rows, missing


def history_gh(ctx):
    days = int(ctx.params.get("days", 180))
    only = (ctx.params.get("repo") or "").strip()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    con = connect()
    if not con.execute("SELECT 1 FROM gh_repos LIMIT 1").fetchone():
        ctx.log("Inventario de GitHub vacío: lo actualizo primero")
        github_inventory(ctx)
    names = [r[0] for r in con.execute("SELECT name FROM gh_repos" + (" WHERE name=?" if only else " ORDER BY name"), (only,) if only else ())]
    if not names:
        raise RuntimeError("No hay repos en GitHub" + (f" con nombre {only!r}" if only else ""))
    q = subprocess.run(["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"], capture_output=True, text=True, timeout=30)
    ctx.log(f"Importando ejecuciones de GitHub Actions desde {since} para {len(names)} repos (cuota restante: {q.stdout.strip() or '?'})")
    total = missing = errors = done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for repo, rows, miss in ex.map(lambda n: _safe(n, since), names):
            done += 1
            if ctx.cancelled():
                ctx.log("Cancelado")
                break
            if rows is None:
                errors += 1
                ctx.log(f"  ! {repo}: error de lectura")
            elif miss:
                missing += 1
            for r in rows or []:
                wf, br = r["name"] or "", r["head_branch"] or ""
                con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["id"], "github", repo, wf, br, r["event"], r["status"], r["conclusion"], r["created_at"],
                             r["run_started_at"], r["updated_at"], guess_env(wf, br), int(bool(DEPLOY_RX.search(wf))), r["html_url"]))
                total += 1
            con.commit()                                   # short transactions: never block other writers
            ctx.progress(done, len(names))
    con.commit()
    n_repos = con.execute("SELECT COUNT(DISTINCT repo) FROM runs").fetchone()[0]
    ctx.log(f"{total} ejecuciones importadas · {n_repos} repos con historial · {missing} sin repo en GitHub · {errors} errores")
    return {"runs": total, "repos_with_runs": n_repos, "not_in_github": missing, "errors": errors, "since": since}


def _safe(name, since):
    try:
        return _gh_runs(name, since, 5)
    except Exception:  # noqa: BLE001
        return name, None, False


# ----------------------------------------------------------------- analytics over stored runs
def _p(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def dora(con, repo=None, env="prod", days=90):
    """Deployment frequency, change-failure rate and MTTR from imported runs (heuristic: workflows named deploy/release)."""
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = "SELECT repo, workflow, conclusion, created FROM runs WHERE is_deploy=1 AND conclusion IN ('success','failure') AND created>=?"
    args = [cut]
    if repo:
        sql += " AND repo=?"; args.append(repo)
    if env and env != "all":
        sql += " AND env=?"; args.append(env)
    rows = con.execute(sql + " ORDER BY created", args).fetchall()
    if not rows:
        return {"deploys": 0, "days": days, "env": env}
    fails = sum(1 for r in rows if r["conclusion"] == "failure")
    mttr, last_fail = [], {}
    for r in rows:
        k = (r["repo"], r["workflow"])
        if r["conclusion"] == "failure":
            last_fail.setdefault(k, _p(r["created"]))
        elif k in last_fail:
            mttr.append((_p(r["created"]) - last_fail.pop(k)).total_seconds() / 3600)
    cfr, per_week = fails / len(rows), len(rows) / (days / 7)
    band = "elite" if cfr <= .05 and per_week >= 7 else "alto" if cfr <= .10 and per_week >= 1 else "medio" if cfr <= .15 else "bajo"
    return {"deploys": len(rows), "failures": fails, "per_week": round(per_week, 2), "change_failure_rate": round(cfr, 3),
            "mttr_hours": round(sum(mttr) / len(mttr), 1) if mttr else None, "band": band, "days": days, "env": env,
            "note": "Heurística: workflows con 'deploy/release/publish' en el nombre; entorno inferido del nombre/rama. Sin lead time."}


def top_failing(con, days=90, min_deploys=5, limit=8):
    """Failure rate per repo. 'startup_failure' (workflow never started) is counted separately so it isn't hidden."""
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("""SELECT repo, COUNT(*) n, SUM(conclusion='failure') f, SUM(conclusion='startup_failure') s FROM runs
        WHERE conclusion IN ('success','failure','startup_failure') AND created>=? GROUP BY repo HAVING n>=?
        ORDER BY 1.0*(f+s)/n DESC, n DESC LIMIT ?""", (cut, min_deploys, limit)).fetchall()
    return [{"repo": r["repo"], "runs": r["n"], "failures": r["f"], "startup_failures": r["s"], "rate": round((r["f"] + r["s"]) / r["n"], 3)} for r in rows]


def startup_window(con):
    """Cluster of 'workflow never started' failures: many repos in the same window suggests an org-level cause."""
    r = con.execute("SELECT COUNT(*), COUNT(DISTINCT repo), MIN(created), MAX(created) FROM runs WHERE conclusion='startup_failure'").fetchone()
    if not r[0]:
        return None
    return {"runs": r[0], "repos": r[1], "from": r[2][:10], "to": r[3][:10]}


def recent_runs(con, repo, limit=20):
    return [dict(r) for r in con.execute("SELECT workflow,branch,conclusion,status,created,env,is_deploy,url FROM runs WHERE repo=? ORDER BY created DESC LIMIT ?", (repo, limit))]


def weekly(con, repo, weeks=12):
    """Runs per ISO week for a sparkline: [(week_start, total, failures)]"""
    cut = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
    out = {}
    for r in con.execute("SELECT created, conclusion FROM runs WHERE repo=? AND created>=? AND conclusion IN ('success','failure')", (repo, cut)):
        d = _p(r["created"]); k = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        t, f = out.get(k, (0, 0)); out[k] = (t + 1, f + (r["conclusion"] == "failure"))
    return [{"week": k, "runs": v[0], "failures": v[1]} for k, v in sorted(out.items())]


# ----------------------------------------------------------------- Azure DevOps (REST, read-only) — NOT TESTED
def history_azure(ctx):
    """Needs AZURE_DEVOPS_EXT_PAT (read-only 'Build: Read' scope) in the server's environment. The token is read at run time,
    never stored, logged or written to the DB. Untested: no PAT was available when this was written."""
    import base64
    import os
    import urllib.parse
    import urllib.request
    if not AZDO_ORG:
        raise RuntimeError("Define COPILOT_AZDO_ORG (organización de Azure DevOps) en el entorno del servidor o en .env")
    pat = os.environ.get("AZURE_DEVOPS_EXT_PAT", "")
    if not pat:
        raise RuntimeError("Falta AZURE_DEVOPS_EXT_PAT en el entorno del servidor (PAT de solo lectura, ámbito Build: Read). No se usa ningún PAT guardado en disco.")
    days = int(ctx.params.get("days", 180))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
    con = connect()
    projects = sorted({urllib.parse.unquote(r[0]) for r in con.execute("SELECT org_project FROM repos WHERE org_project!=''")})
    total = 0
    for i, proj in enumerate(projects, 1):
        url = (f"https://dev.azure.com/{urllib.parse.quote(AZDO_ORG)}/{urllib.parse.quote(proj)}/_apis/build/builds?api-version=7.1"
               f"&minTime={since}&$top=1000&queryOrder=finishTimeDescending")
        req = urllib.request.Request(url, headers={"Authorization": auth, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                builds = json.load(resp).get("value", [])
        except Exception as e:  # noqa: BLE001 - never echo headers/tokens
            ctx.log(f"  ! {proj}: {type(e).__name__}")
            continue
        for b in builds:
            res = {"succeeded": "success", "failed": "failure", "canceled": "cancelled", "partiallySucceeded": "failure"}.get(b.get("result"), None)
            wf, br = (b.get("definition") or {}).get("name", ""), (b.get("sourceBranch") or "").replace("refs/heads/", "")
            con.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (-int(b["id"]), "azure", (b.get("repository") or {}).get("name", wf), wf, br, b.get("reason"), b.get("status"), res,
                         b.get("queueTime"), b.get("startTime"), b.get("finishTime"), guess_env(wf, br), int(bool(DEPLOY_RX.search(wf))),
                         ((b.get("_links") or {}).get("web") or {}).get("href")))
            total += 1
        con.commit()
        ctx.log(f"  {proj}: {len(builds)} ejecuciones")
        ctx.progress(i, len(projects))
    return {"runs": total, "projects": len(projects), "since": since}
