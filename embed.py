"""Incremental embeddings via local Ollama (bge-m3). Vectors cached in SQLite keyed by sha1(text),
so a reindex only embeds fragments that actually changed. Stdlib only; talks to 127.0.0.1 only."""
import array
import hashlib
import json
import math
import os
import urllib.request

from common import connect

OLLAMA = os.environ.get("COPILOT_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("COPILOT_EMBED_MODEL", "bge-m3")
MAX_CHARS = 1500
BATCH = 16


def _post(path: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_probe = {"t": 0.0, "ok": False, "why": ""}


def probe(max_age: float = 15.0) -> tuple[bool, str]:
    """Real check: embed one word. /api/version can answer while the model itself is broken (stale mount, OOM)."""
    import time
    if time.time() - _probe["t"] < max_age:
        return _probe["ok"], _probe["why"]
    try:
        embed_texts(["ok"])
        _probe.update(ok=True, why="")
    except Exception as e:  # noqa: BLE001
        _probe.update(ok=False, why=f"{type(e).__name__}")
    _probe["t"] = time.time()
    return _probe["ok"], _probe["why"]


def available() -> bool:
    return probe()[0]


def key(text: str) -> str:
    return hashlib.sha1(text[:MAX_CHARS].encode()).hexdigest()


def embed_texts(texts: list[str]) -> list[array.array]:
    out = _post("/api/embed", {"model": MODEL, "keep_alive": "30m", "input": [t[:MAX_CHARS] for t in texts]})["embeddings"]
    res = []
    for v in out:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        res.append(array.array("f", (x / n for x in v)))
    return res


def ensure(con=None, progress=None) -> dict:
    """Embed every chunk whose hash has no cached vector. Returns counts."""
    con = con or connect()
    con.execute("CREATE TABLE IF NOT EXISTS vecs(h TEXT PRIMARY KEY, dim INT, v BLOB)")
    have = {r[0] for r in con.execute("SELECT h FROM vecs")}
    todo, seen = [], set()
    for r in con.execute("SELECT title, body FROM chunks"):
        text = (r["title"] + "\n" + r["body"])
        k = key(text)
        if k not in have and k not in seen:
            seen.add(k)
            todo.append((k, text))
    for i in range(0, len(todo), BATCH):
        part = todo[i:i + BATCH]
        for (k, _), v in zip(part, embed_texts([t for _, t in part])):
            con.execute("INSERT OR REPLACE INTO vecs VALUES(?,?,?)", (k, len(v), v.tobytes()))
        con.commit()
        if progress:
            progress(min(i + BATCH, len(todo)), len(todo))
    return {"embedded": len(todo), "cached": len(have)}


if __name__ == "__main__":
    import time
    t0 = time.time()
    print(ensure(progress=lambda d, n: print(f"\r{d}/{n}", end="", flush=True)), f"{time.time() - t0:.0f}s")
