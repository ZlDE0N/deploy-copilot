"""Shared helpers: paths, DB, secret redaction. Zero third-party deps."""
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT.parent                      # sources live next to the project folder
DB_PATH = ROOT / "data" / "copilot.db"

# Secrets never enter the index. Order matters (specific first).
_REDACTIONS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GH_TOKEN]"),
    # user-info in URLs: https://TOKEN@host  or  https://user:pass@host
    (re.compile(r"(https?://)[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|pat|connectionstring|sas)\b(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|[^\s\"',;]{6,})"),
     "GENERIC"),
    (re.compile(r"(?i)(AccountKey|SharedAccessKey|Password)=([^;\s\"']+)"), "CONN"),
    (re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b"), "[REDACTED_LONG_TOKEN]"),
]


MAX_REDACT_INPUT = 1_000_000   # chars; bounds worst-case regex cost

# ENV_STYLE_KEY=value  (e.g. NEXT_PUBLIC_X_SECRET=abc, db_password: abc). `_` defeats \b above.
# No identifier prefix is matched: it stays in the text, so a long name can never hide the value, and the scan is
# linear (bounded suffix, no per-position retries).
_ENV_SECRET = re.compile(
    r"(?i)((?:secret|token|passw(?:or)?d|pwd|api_?key|private_?key|credential|client_?key)"
    r"[A-Za-z0-9_.-]{0,200}[\"']?\s{0,20}[:=]\s{0,20}[\"']?)([^\s\"',;]{6,})")
# any key/subscription name followed by a long opaque literal
_KEY_VALUE = re.compile(
    r"(?i)((?:key|subscription|apim)[A-Za-z0-9_.-]{0,200}[\"']?\s{0,20}[:=]\s{0,20}[\"']?)"
    r"([A-Za-z0-9+/=-]{20,})")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*")
# Azure AD client secret: 3 chars + digit + "Q~" + tail. Fixed-width head, unbounded tail: nothing survives past a cap.
_AAD_SECRET = re.compile(r"[A-Za-z0-9_.~-]{3}\dQ~[A-Za-z0-9_.~-]{20,}")


# A value is left untouched ONLY if the whole value is a known variable-reference syntax (or an existing redaction marker).
# Anything else, including values that merely START like a reference, is a literal and gets redacted.
_REF = re.compile(
    r"\$\{\{[^{}\s][^{}]{0,200}\}\}"            # ${{secrets.NAME}}  (GitHub Actions)
    r"|\$\{[A-Za-z_][A-Za-z0-9_]{0,100}\}"        # ${NAME}
    r"|\$\([A-Za-z_][A-Za-z0-9_.]{0,100}\)"       # $(name)  (Azure Pipelines / shell)
    r"|\$[A-Za-z_][A-Za-z0-9_]{0,100}"             # $NAME
    r"|%[A-Za-z_][A-Za-z0-9_]{0,100}%"              # %NAME%
    r"|\[REDACTED[A-Z_]*\]"                        # already redacted (keeps redact() idempotent)
)


def _is_ref(value: str) -> bool:
    return _REF.fullmatch(value.strip("\"'")) is not None


def _env_repl(m: "re.Match[str]") -> str:
    return m.group(0) if _is_ref(m.group(2)) else m.group(1) + "[REDACTED]"


def _env_repl_key(m: "re.Match[str]") -> str:
    # value class excludes `$ % [ {`, so it can never be a reference: always a literal
    return m.group(1) + "[REDACTED]"


def _conn_repl(m: "re.Match[str]") -> str:
    return m.group(0) if _is_ref(m.group(2)) else m.group(1) + "=[REDACTED]"


def _generic_repl(m: "re.Match[str]") -> str:
    return m.group(0) if _is_ref(m.group(3)) else m.group(1) + m.group(2) + "[REDACTED]"


def safe_cap(text: str, limit: int) -> str:
    """Bound input size without ever leaving half a secret at the cut: cut at the last newline, or drop a margin."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut] if cut > limit // 2 else text[:max(limit - 512, 0)]


def redact(text: str) -> str:
    text = safe_cap(text, MAX_REDACT_INPUT)
    # name-based rules FIRST: the generic long-token rule below would otherwise swallow the key name
    # (e.g. a 100-char identifier ending in "key") and leave its value in clear text.
    text = _ENV_SECRET.sub(_env_repl, text)
    text = _KEY_VALUE.sub(_env_repl_key, text)
    for rx, rep in _REDACTIONS:
        text = rx.sub({"GENERIC": _generic_repl, "CONN": _conn_repl}.get(rep, rep), text)
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _AAD_SECRET.sub("[REDACTED_AAD_SECRET]", text)
    return text


SCHEMA = """
CREATE TABLE IF NOT EXISTS repos(
  name TEXT PRIMARY KEY, branch TEXT, type TEXT, csv_type TEXT, workflow TEXT,
  has_workflow INT, has_dockerfile INT, has_terraform INT, has_lfs INT,
  az_branches TEXT, n_files INT, last_commit TEXT, org_project TEXT
);
CREATE TABLE IF NOT EXISTS deploys(
  id INTEGER PRIMARY KEY, repo TEXT, env TEXT, strategy TEXT,
  result TEXT, notes TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, source TEXT, repo TEXT COLLATE NOCASE, workflow TEXT, branch TEXT, event TEXT,
  status TEXT, conclusion TEXT, created TEXT, started TEXT, updated TEXT, env TEXT, is_deploy INT, url TEXT
);
CREATE INDEX IF NOT EXISTS runs_repo ON runs(repo, created);
CREATE TABLE IF NOT EXISTS gh_repos(
  name TEXT PRIMARY KEY COLLATE NOCASE, pushed TEXT, archived INT, visibility TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS gitstats(
  repo TEXT PRIMARY KEY COLLATE NOCASE, snapshot TEXT, commits_total INT, commits_90d INT, authors_90d INT, last_commit TEXT,
  days_since INT, first_commit TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS job_runs(
  id INTEGER PRIMARY KEY, job TEXT, status TEXT, params TEXT, started REAL, finished REAL, result TEXT, log TEXT
);
CREATE TABLE IF NOT EXISTS eval_runs(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, metrics TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
  body, title, kind UNINDEXED, repo UNINDEXED, source UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
"""


def connect(shared: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=not shared)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE repos ADD COLUMN az_diverged INT DEFAULT 0")
    except sqlite3.OperationalError:
        pass                                     # column already there
    return con
