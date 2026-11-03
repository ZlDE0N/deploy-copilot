"""Exact answers for aggregate questions ("cuántos repos…", "lista de repos que…") straight from SQLite.
LLMs must not count: this router answers those questions deterministically and returns the SQL it ran."""
import re
import unicodedata

from common import connect

# keyword in the question -> (label, WHERE clause, params). First match wins, most specific first.
FILTERS = [
    (r"react native|react-native", "React Native", "type=?", ("mobile-react-native",)),
    (r"mobile|movil|ios|android", "Mobile nativo", "type=?", ("mobile-native",)),
    (r"terraform|iac|bicep|infraestructura como codigo", "Terraform / IaC", "type=?", ("terraform",)),
    (r"container|contenedor|docker|aca\b", "Container", "type=?", ("container",)),
    (r"angular", "Angular", "type=?", ("frontend-angular",)),
    (r"function.*node|node.*function", "Function Node", "type=?", ("function-node",)),
    (r"function|funcion", "Functions (.NET y Node)", "type IN (?,?)", ("function-dotnet", "function-node")),
    (r"app ?service|appservice|api.*dotnet|\.net|dotnet", "App Service .NET", "type=?", ("appservices-dotnet",)),
    (r"frontend|front-end|node|react", "Frontend / Node", "type=?", ("frontend-or-node",)),
    (r"sin clasificar|desconocid|unknown", "Sin clasificar", "type=?", ("unknown",)),
    (r"lfs", "Usan Git LFS (según .gitattributes)", "has_lfs=1", ()),
    (r"azure pipelines?|azure-pipelines", "Aún en Azure Pipelines", "workflow LIKE ?", ("%azure-pipelines%",)),
    (r"sin pipeline|sin ci|sin workflow", "Sin pipeline", "has_workflow=0", ()),
    (r"divergen|difieren|_az", "Rama ≠ copia _az", "az_diverged=1", ()),
]
COUNT_Q = re.compile(r"\b(cuant[oa]s?|n[uú]mero de|total de|lista(?:me)?|cu[aá]les|qu[eé] repos?|que repos?)\b")
REPO_W = re.compile(r"\b(repos?|repositorios?|proyectos?)\b")


def _n(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t.lower()) if not unicodedata.combining(c))


def answer(q: str) -> dict | None:
    t = _n(q)
    if not (COUNT_Q.search(t) and REPO_W.search(t)):
        return None
    con = connect()
    for rx, label, where, params in FILTERS:
        if re.search(rx, t):
            sql = f"SELECT name FROM repos WHERE {where} ORDER BY name"
            names = [r[0] for r in con.execute(sql, params)]
            return {"label": label, "count": len(names), "repos": names[:60], "truncated": len(names) > 60,
                    "sql": re.sub(r"\?", lambda m, it=iter(params): repr(next(it)), sql), "total": con.execute("SELECT COUNT(*) FROM repos").fetchone()[0]}
    if re.search(r"\b(cuant[oa]s?|total)\b", t):
        total = con.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        return {"label": "Todos los repos indexados", "count": total, "repos": [], "truncated": False,
                "sql": "SELECT COUNT(*) FROM repos", "total": total}
    return None


def as_fact(a: dict) -> str:
    names = ", ".join(a["repos"][:25]) + (" …" if a["count"] > 25 else "")
    return f"DATOS EXACTOS (SQL sobre el índice vivo): {a['label']} = {a['count']} de {a['total']} repos. Repos: {names or 'n/a'}."
