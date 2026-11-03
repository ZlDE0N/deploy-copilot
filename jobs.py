"""Manual, repeatable jobs. Allow-listed (name -> function); params are validated; every run is stored with its log
and streamed to the UI over the event bus. A job never runs unless a human starts it (or the schedule they enabled)."""
import json
import threading
import time

from common import connect

REGISTRY: dict[str, dict] = {}
RUNNING: dict[str, "Ctx"] = {}
LOCK = threading.Lock()
BUS = None  # set by server


def register(name, title, desc, fn, params=None, heavy=False):
    REGISTRY[name] = {"name": name, "title": title, "desc": desc, "fn": fn, "params": params or [], "heavy": heavy}


class Ctx:
    def __init__(self, run_id, job, params):
        self.run_id, self.job, self.params = run_id, job, params
        self.lines, self._cancel, self._last_flush = [], False, 0.0

    def log(self, msg):
        self.lines.append(f"{time.strftime('%H:%M:%S')} {msg}")
        BUS and BUS.publish("job:log", {"id": self.run_id, "job": self.job, "line": self.lines[-1]})
        self._flush()

    def progress(self, done, total):
        BUS and BUS.publish("job:progress", {"id": self.run_id, "job": self.job, "done": done, "total": total})

    def cancelled(self):
        return self._cancel

    def _flush(self, force=False):
        if force or time.time() - self._last_flush > 1.5:
            self._last_flush = time.time()
            try:
                c = connect()
                c.execute("UPDATE job_runs SET log=? WHERE id=?", ("\n".join(self.lines[-400:]), self.run_id))
                c.commit()
            except Exception:  # noqa: BLE001 - log persistence is best-effort; the final write in _run is authoritative
                pass


def _clean_params(spec, params):
    out = {}
    for p in spec["params"]:
        v = (params or {}).get(p["key"], p.get("default"))
        if p["type"] == "int":
            v = int(v)
            if not p["min"] <= v <= p["max"]:
                raise ValueError(f"{p['key']} fuera de rango [{p['min']}, {p['max']}]")
        elif p["type"] == "text":
            v = str(v or "")[:120]
        elif p["type"] == "bool":
            v = bool(v)
        out[p["key"]] = v
    return out


def start(name, params=None):
    spec = REGISTRY.get(name)
    if not spec:
        raise KeyError("trabajo desconocido")
    params = _clean_params(spec, params)
    with LOCK:
        if name in RUNNING:
            raise RuntimeError("ya hay una ejecución en curso de este trabajo")
        con = connect()
        cur = con.execute("INSERT INTO job_runs(job,status,params,started) VALUES(?,?,?,?)", (name, "running", json.dumps(params), time.time()))
        con.commit()
        ctx = Ctx(cur.lastrowid, name, params)
        RUNNING[name] = ctx
    BUS and BUS.publish("job:start", {"id": ctx.run_id, "job": name, "params": params})
    threading.Thread(target=_run, args=(spec, ctx), daemon=True).start()
    return ctx.run_id


def _run(spec, ctx):
    status, result = "ok", {}
    try:
        result = spec["fn"](ctx) or {}
        if ctx.cancelled():
            status = "cancelled"
    except Exception as e:  # noqa: BLE001 - a failing job must never take the server down
        status, result = "failed", {"error": f"{type(e).__name__}: {e}"}
        ctx.log(f"ERROR {result['error']}")
    finally:
        ctx._flush(force=True)
        c = connect()
        c.execute("UPDATE job_runs SET status=?, finished=?, result=?, log=? WHERE id=?",
                  (status, time.time(), json.dumps(result, default=str), "\n".join(ctx.lines[-400:]), ctx.run_id))
        c.commit()
        with LOCK:
            RUNNING.pop(ctx.job, None)
        BUS and BUS.publish("job:done", {"id": ctx.run_id, "job": ctx.job, "status": status, "result": result,
                                         "ms": int((time.time() - c.execute('SELECT started FROM job_runs WHERE id=?', (ctx.run_id,)).fetchone()[0]) * 1000)})


def cancel(name):
    ctx = RUNNING.get(name)
    if ctx:
        ctx._cancel = True
        ctx.log("Cancelación solicitada…")
        return True
    return False


def listing():
    con = connect()
    out = []
    for name, s in REGISTRY.items():
        last = con.execute("SELECT id,status,started,finished,result FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        out.append({"name": name, "title": s["title"], "desc": s["desc"], "params": s["params"], "heavy": s["heavy"],
                    "running": name in RUNNING, "run_id": RUNNING[name].run_id if name in RUNNING else None,
                    "last": ({"id": last["id"], "status": last["status"], "started": last["started"], "finished": last["finished"],
                              "result": json.loads(last["result"] or "{}")} if last else None)})
    return out


def runs(name=None, limit=15):
    con = connect()
    sql = "SELECT id,job,status,params,started,finished,result FROM job_runs" + (" WHERE job=?" if name else "") + " ORDER BY id DESC LIMIT ?"
    return [{**dict(r), "params": json.loads(r["params"] or "{}"), "result": json.loads(r["result"] or "{}")}
            for r in con.execute(sql, ((name, limit) if name else (limit,)))]


def run_detail(run_id):
    r = connect().execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
    return {**dict(r), "params": json.loads(r["params"] or "{}"), "result": json.loads(r["result"] or "{}")} if r else None


def mark_stale():
    """After a crash/restart, runs left as 'running' can never finish: close them honestly."""
    c = connect()
    c.execute("UPDATE job_runs SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
    c.commit()
