#!/usr/bin/env python3
"""Retrieval evaluation (RAGAS-style context metrics, no LLM judge): recall@k, MRR, per-question ranks.
Compares sparse-only, dense-only and the hybrid pipeline so changes are measured, not guessed."""
import json
from pathlib import Path

from rag import Index

_EVAL_DIR = Path(__file__).parent / "eval"
# golden.json is your own reference set (kept out of version control); the example file is a template to copy.
GOLD = _EVAL_DIR / "golden.json" if (_EVAL_DIR / "golden.json").exists() else _EVAL_DIR / "golden.example.json"


def first_rank(sources, expect):
    for i, s in enumerate(sources, 1):
        if expect.lower() in s.lower():
            return i
    return None


def run(ix=None, k=5):
    ix = ix or Index()
    gold = json.loads(GOLD.read_text())
    modes = {"bm25": lambda q: [ix.rows[r]["source"] + "|" + ix.rows[r]["repo"] + "|" + ix.rows[r]["kind"] for r in ix.sparse(q, None, None)],
             "ngram": lambda q: [ix.rows[r]["source"] + "|" + ix.rows[r]["repo"] + "|" + ix.rows[r]["kind"] for r in ix.dense(q, None, None)],
             "semantic": lambda q: [ix.rows[r]["source"] + "|" + ix.rows[r]["repo"] + "|" + ix.rows[r]["kind"] for r in ix.semantic(q, None, None)[0]],
             "hybrid": lambda q: [h["source"] + "|" + h["repo"] + "|" + h["kind"] for h in ix.search(q, 10)["hits"]]}
    res = {}
    sem_err = ix.semantic("prueba", None, None)[1]
    for name, fn in modes.items():
        if name == "semantic" and sem_err:
            res[name] = {"recall@%d" % k: None, "mrr": None, "ranks": [], "unavailable": sem_err}
            continue
        ranks = [first_rank(fn(g["q"]), g["expect"]) for g in gold]
        res[name] = {"recall@%d" % k: round(sum(1 for r in ranks if r and r <= k) / len(gold), 3),
                     "mrr": round(sum(1 / r for r in ranks if r) / len(gold), 3), "ranks": ranks}
    res["questions"] = [g["q"] for g in gold]
    return res


if __name__ == "__main__":
    r = run()
    for m in ("bm25", "ngram", "semantic", "hybrid"):
        if r[m].get("unavailable"):
            print(f"{m:<8} NO DISPONIBLE ({r[m]['unavailable']}); no se reporta como 0")
        else:
            print(f"{m:<8} recall@5={r[m]['recall@5']:<6} mrr={r[m]['mrr']:<6} ranks={r[m]['ranks']}")
