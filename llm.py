"""Local generation backends. Stdlib only, nothing leaves the machine.

  dmr    : Docker Model Runner. llama.cpp runs on the HOST (Metal GPU); reached through Docker Desktop's
           unix socket, so no Docker setting (RAM, TCP port) has to change.
  ollama : container fallback (CPU only on macOS).

COPILOT_GEN_BACKEND = auto (default) | dmr | ollama
"""
import http.client
import json
import os
import socket
import time
import urllib.request

import embed

SOCK = os.environ.get("COPILOT_DMR_SOCK", os.path.expanduser("~/Library/Containers/com.docker.docker/Data/inference.sock"))
DMR_MODEL = os.environ.get("COPILOT_DMR_MODEL", "ai/qwen2.5")
OLLAMA_MODEL = os.environ.get("COPILOT_GEN_MODEL", "qwen2.5:1.5b-instruct")
PREFERRED = os.environ.get("COPILOT_GEN_BACKEND", "auto")


class _Unix(http.client.HTTPConnection):
    def __init__(self, timeout: float = 300):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCK)


def _dmr_get(path: str, timeout: float = 3):
    c = _Unix(timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


def dmr_status() -> dict:
    if not os.path.exists(SOCK):
        return {"up": False, "reason": "socket no encontrado (¿Docker Desktop apagado?)"}
    try:
        st, body = _dmr_get("/engines/v1/models")
        ids = [m.get("id") for m in body.get("data", [])]
        return {"up": st == 200, "model_present": DMR_MODEL in ids, "models": ids}
    except Exception as e:  # noqa: BLE001
        return {"up": False, "reason": f"{type(e).__name__}: {e}"}


def backend() -> str:
    if PREFERRED in ("dmr", "ollama"):
        return PREFERRED
    s = dmr_status()
    return "dmr" if s.get("up") and s.get("model_present") else "ollama"


def status() -> dict:
    d = dmr_status()
    b = backend()
    return {"generation": {"backend": b, "model": DMR_MODEL if b == "dmr" else OLLAMA_MODEL,
                           "gpu": b == "dmr", "dmr": d},
            "embeddings": dict(zip(("up", "reason"), embed.probe())) | {"backend": "ollama", "model": embed.MODEL}}


def _stream_dmr(messages, max_tokens, temperature):
    body = json.dumps({"model": DMR_MODEL, "stream": True, "temperature": temperature,
                       "max_tokens": max_tokens, "messages": messages})
    last_err = "sin respuesta"
    for attempt in range(3):                      # cold model load can answer 5xx once; retry
        c = _Unix(300)
        try:
            c.request("POST", "/engines/llama.cpp/v1/chat/completions", body, {"content-type": "application/json"})
            r = c.getresponse()
            if r.status != 200:
                last_err = f"HTTP {r.status}: {r.read()[:160].decode(errors='replace')}"
                time.sleep(2 + attempt * 2)
                continue
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                delta = json.loads(line[5:])["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            return
        finally:
            c.close()
    raise RuntimeError(f"Model Runner: {last_err}")


def _stream_ollama(messages, max_tokens, temperature):
    body = {"model": OLLAMA_MODEL, "stream": True, "keep_alive": "10m", "messages": messages,
            "options": {"temperature": temperature, "num_ctx": 4096, "num_predict": max_tokens}}
    req = urllib.request.Request(embed.OLLAMA + "/api/chat", json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        for line in resp:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("message", {}).get("content"):
                yield d["message"]["content"]
            if d.get("done"):
                return


def stream(messages, max_tokens: int = 350, temperature: float = 0.1):
    """Yield text deltas. Falls back from dmr to ollama if the host runner fails before producing output."""
    b = backend()
    if b == "dmr":
        started = False
        try:
            for d in _stream_dmr(messages, max_tokens, temperature):
                started = True
                yield d
            return
        except Exception:
            if started or PREFERRED == "dmr":
                raise
    yield from _stream_ollama(messages, max_tokens, temperature)


def warm() -> None:
    """Load the generation model into memory so the first user question doesn't pay the cold start."""
    try:
        for _ in stream([{"role": "user", "content": "ok"}], max_tokens=1):
            break
    except Exception:  # noqa: BLE001 - warming is best-effort
        pass
