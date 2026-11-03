#!/usr/bin/env python3
"""Index local sources into SQLite FTS5. Read-only on sources. Redacts secrets.

Sources:
  - migration/<repo>/          bare git repos -> manifest + workflows + README
  - repo_github/*.csv          pipeline inventory (repo,type,file,branch)
  - REPORTE_*.md, SECURITY-AUDIT.md, Documentacion/**/*.md   incidents & docs
  - migration/*.sh             migration scripts (the technique itself)
"""
import csv
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import WORK, connect, redact, safe_cap

MIGRATION = WORK / "migration"
PRIORITY = ["main", "master", "main_az", "develop", "qa"]
MAX_BLOB = 200_000  # bytes per indexed file


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, errors="replace", timeout=timeout)
    return r.stdout if r.returncode == 0 else ""


def pick_branch(repo: Path) -> tuple[str, list[str]]:
    out = git(repo, "for-each-ref", "--sort=-committerdate",
              "--format=%(refname:short)", "refs/heads")
    refs = [x for x in out.split() if x]
    az = [x for x in refs if x.endswith("_az")]
    for p in PRIORITY:
        if p in refs:
            return p, az
    return (refs[0] if refs else ""), az


def detect_type(files: list[str], pkg: str) -> str:
    low = [f.lower() for f in files]
    names = {f.rsplit("/", 1)[-1] for f in low}
    has = lambda ext: any(f.endswith(ext) for f in low)  # noqa: E731
    if has(".tf"):
        return "terraform"
    if "host.json" in names:
        return "function-node" if "package.json" in names and not has(".csproj") else "function-dotnet"
    if "react-native" in pkg:
        return "mobile-react-native"
    if has(".xcodeproj") or "build.gradle" in names:
        return "mobile-native"
    if "angular.json" in names:
        return "frontend-angular"
    if "dockerfile" in names:
        return "container"
    if has(".csproj"):
        return "appservices-dotnet"
    if "package.json" in names:
        return "frontend-or-node"
    if has(".go"):
        return "go"
    if has(".py"):
        return "python"
    return "unknown"


def index_repo(repo: Path) -> dict:
    branch, az = pick_branch(repo)
    if not branch:
        return {"name": repo.name, "empty": True}
    files = git(repo, "ls-tree", "-r", "--name-only", branch).splitlines()
    pkg = git(repo, "show", f"{branch}:package.json")[:20000] if "package.json" in files else ""
    wf = [f for f in files if f.startswith(".github/workflows/") or
          f.rsplit("/", 1)[-1] in ("azure-pipelines.yml", "azure-pipelines.yaml")]
    attrs = git(repo, "show", f"{branch}:.gitattributes") if ".gitattributes" in files else ""
    last = git(repo, "log", "-1", "--format=%cs", branch).strip()
    twin = next((a for a in az if a == f"{branch}_az"), None) or ("main_az" if "main_az" in az and branch in ("main", "master") else None)
    diverged = int(bool(twin) and git(repo, "rev-parse", branch).strip() != git(repo, "rev-parse", twin).strip())
    url = git(repo, "config", "--get", "remote.origin.url")
    m = re.search(r"dev\.azure\.com/[^/]+/([^/]+)/_git", url)
    docs = []
    for f in wf:
        txt = redact(safe_cap(git(repo, "show", f"{branch}:{f}"), 4 * MAX_BLOB))[:MAX_BLOB]
        docs.append(("workflow", repo.name, f"{repo.name}:{f}@{branch}", f, txt))
    for f in files:
        if f.lower() in ("readme.md", "readme") or f.lower().startswith("docs/") and f.endswith(".md"):
            txt = redact(safe_cap(git(repo, "show", f"{branch}:{f}"), 4 * MAX_BLOB))[:MAX_BLOB]
            docs.append(("readme", repo.name, f"{repo.name}:{f}@{branch}", f, txt))
    return {
        "name": repo.name, "branch": branch, "type": detect_type(files, pkg),
        "workflow": ",".join(f.rsplit("/", 1)[-1] for f in wf),
        "has_workflow": int(bool(wf)),
        "has_dockerfile": int(any(f.lower().endswith("dockerfile") for f in files)),
        "has_terraform": int(any(f.endswith(".tf") for f in files)),
        "has_lfs": int("filter=lfs" in attrs), "az_diverged": diverged,
        "az_branches": ",".join(az), "n_files": len(files), "last_commit": last,
        "org_project": m.group(1) if m else "", "docs": docs,
    }


def code_chunks(text: str, max_len: int = 1400):
    """Split scripts/YAML on blank lines into ~max_len pieces so long files are retrievable by section."""
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) > max_len:
            yield buf.strip()
            buf = ""
        buf += para + "\n\n"
        while len(buf) > max_len * 1.5:
            yield buf[:max_len].strip()
            buf = buf[max_len:]
    if buf.strip():
        yield buf.strip()


def md_chunks(text: str, max_len: int = 1800):
    """Split markdown by headings; sub-split long sections by paragraph."""
    parts = re.split(r"(?m)^(?=#{1,3} )", text)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        title = p.splitlines()[0].lstrip("# ").strip()[:120]
        while len(p) > max_len:
            cut = p.rfind("\n\n", 0, max_len)
            cut = cut if cut > 200 else max_len
            yield title, p[:cut]
            p = p[cut:].strip()
        if p:
            yield title, p


def main() -> None:
    con = connect()
    con.execute("DELETE FROM chunks")
    con.execute("DELETE FROM repos")
    n_chunks = 0

    def add(kind, repo, source, title, body):
        nonlocal n_chunks
        body, title, source = redact(body), redact(title), redact(source)   # second pass per chunk + metadata
        if body.strip():
            con.execute("INSERT INTO chunks(body,title,kind,repo,source) VALUES(?,?,?,?,?)",
                        (body, title, kind, repo, source))
            n_chunks += 1

    # 1) repos
    repos = sorted(p for p in MIGRATION.iterdir() if p.is_dir() and (p / "HEAD").exists())
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(index_repo, repos))

    # 2) CSV inventory (declared type + workflow)
    csv_info = {}
    for f in sorted((WORK / "repo_github").glob("*.csv")):
        with open(f, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                if row.get("repo"):
                    csv_info[row["repo"].lower()] = row

    for r in results:
        if r.get("empty"):
            continue
        ci = csv_info.get(r["name"].lower(), {})
        r["csv_type"] = ci.get("type", "")
        con.execute(
            "INSERT OR REPLACE INTO repos(name,branch,type,csv_type,workflow,has_workflow,has_dockerfile,has_terraform,"
            "has_lfs,az_branches,n_files,last_commit,org_project,az_diverged) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["name"], r["branch"], r["type"], r["csv_type"], r["workflow"],
             r["has_workflow"], r["has_dockerfile"], r["has_terraform"], r["has_lfs"],
             r["az_branches"], r["n_files"], r["last_commit"], r["org_project"], r["az_diverged"]))
        summary = (f"Repo {r['name']}: tipo detectado {r['type']}, tipo en CSV {r['csv_type'] or 'n/d'}, "
                   f"rama base {r['branch']}, ramas _az: {r['az_branches'] or 'ninguna'}, "
                   f"workflow: {r['workflow'] or 'ninguno'}, proyecto Azure DevOps {r['org_project'] or 'n/d'}.")
        add("manifest", r["name"], f"repo:{r['name']}", r["name"], summary)
        for kind, repo, source, title, txt in r["docs"]:
            parts = md_chunks(txt) if kind == "readme" else ((title, c) for c in code_chunks(txt))
            for t, c in parts:
                add(kind, repo, source, t, c)

    # 3) incidents, audits, docs (markdown)
    md_files = list(WORK.glob("REPORTE_*.md")) + [WORK / "SECURITY-AUDIT.md"]
    md_files += list((WORK / "Documentacion").rglob("*.md"))
    for f in md_files:
        if f.exists():
            kind = "incident" if f.name.startswith("REPORTE_INCIDENTE") else "doc"
            for t, c in md_chunks(redact(safe_cap(f.read_text(errors="replace"), 4 * MAX_BLOB))):
                add(kind, "", str(f.relative_to(WORK)), t, c)

    # 4) migration technique (scripts)
    for f in sorted(MIGRATION.glob("*.sh")):
        for c in code_chunks(redact(safe_cap(f.read_text(errors="replace"), 4 * MAX_BLOB))):
            add("script", "", str(f.relative_to(WORK)), f.name, c)

    con.commit()
    n_repos = con.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    print(f"repos={n_repos} chunks={n_chunks} db=data/copilot.db")


if __name__ == "__main__":
    main()
