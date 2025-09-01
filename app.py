import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
DEFAULT_TTL = int(os.getenv("DEFAULT_TTL_SECONDS", "900"))  # 15 minutes
ALLOW_REUSE = os.getenv("ALLOW_REUSE", "0") == "1"

app = Flask(__name__)
CORS(app)

# ---------------------------
# Storage backends
# ---------------------------

class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._codes: Dict[str, Dict[str, Any]] = {}

    def add(self, code: str, ttl_seconds: int, metadata: Optional[Dict[str, Any]] = None):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._codes[code] = {
                "code": code,
                "expires_at": expires_at,
                "metadata": metadata or {},
                "used": False
            }
        return expires_at

    def check_and_consume(self, code: str, allow_reuse: bool = False):
        with self._lock:
            entry = self._codes.get(code)
            if not entry:
                return None, "not_found"
            if entry["expires_at"] < datetime.now(timezone.utc):
                return None, "expired"
            if entry["used"] and not allow_reuse:
                return None, "used"
            entry["used"] = True
            return entry, None

    def purge(self):
        now = datetime.now(timezone.utc)
        with self._lock:
            to_del = [c for c, e in self._codes.items() if e["expires_at"] < now or e["used"]]
            for c in to_del:
                del self._codes[c]
        return len(to_del)

# Optional: SQLite persistence (commented out by default)
class SQLiteStore:
    def __init__(self, path="codes.db"):
        self.path = path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                metadata TEXT
            )""")
            conn.commit()

    def add(self, code: str, ttl_seconds: int, metadata: Optional[Dict[str, Any]] = None):
        expires_at = int(time.time()) + ttl_seconds
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("REPLACE INTO codes(code, expires_at, used, metadata) VALUES (?, ?, 0, ?)",
                      (code, expires_at, json_dumps(metadata or {})))
            conn.commit()
        return datetime.fromtimestamp(expires_at, tz=timezone.utc)

    def check_and_consume(self, code: str, allow_reuse: bool = False):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("SELECT code, expires_at, used, metadata FROM codes WHERE code=?", (code,))
            row = c.fetchone()
            if not row:
                return None, "not_found"
            code, expires_at, used, metadata = row
            if expires_at < int(time.time()):
                return None, "expired"
            if used and not allow_reuse:
                return None, "used"
            if not used:
                c.execute("UPDATE codes SET used=1 WHERE code=?", (code,))
                conn.commit()
            entry = {
                "code": code,
                "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc),
                "used": True,
                "metadata": json_loads(metadata) if metadata else {}
            }
            return entry, None

    def purge(self):
        now = int(time.time())
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM codes WHERE expires_at < ? OR used = 1", (now,))
            n = c.rowcount if c.rowcount is not None else 0
            conn.commit()
        return n

# Choose storage backend
USE_SQLITE = os.getenv("USE_SQLITE", "0") == "1"
if USE_SQLITE:
    # Requires `import json` helpers:
    import json as _json
    def json_dumps(x): return _json.dumps(x, separators=(",",":"))
    def json_loads(s): return _json.loads(s)
    store = SQLiteStore()
else:
    def json_dumps(x): import json; return json.dumps(x, separators=(",",":"))
    def json_loads(s): import json; return json.loads(s)
    store = MemoryStore()

# ---------------------------
# Helpers
# ---------------------------

def require_api_key():
    key = request.headers.get("X-API-KEY", "")
    if not ADMIN_API_KEY or key != ADMIN_API_KEY:
        return jsonify({"status": "error", "error": "unauthorized"}), 401
    return None

def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

# ---------------------------
# Routes
# ---------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": isoformat(datetime.now(timezone.utc))})

@app.route("/addcode", methods=["POST"])
def add_code():
    auth = require_api_key()
    if auth:
        return auth

    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or request.args.get("code") or "").strip()
    if not code:
        return jsonify({"status": "error", "error": "missing_code"}), 400

    ttl = payload.get("ttl_seconds") or request.args.get("ttl_seconds") or DEFAULT_TTL
    try:
        ttl = int(ttl)
        if ttl <= 0 or ttl > 86400:
            raise ValueError
    except Exception:
        return jsonify({"status": "error", "error": "invalid_ttl"}), 400

    metadata = payload.get("metadata")
    expires_at = store.add(code, ttl, metadata)
    return jsonify({"status": "added", "code": code, "expires_at": isoformat(expires_at)})

@app.route("/checkcode", methods=["GET", "POST"])
def check_code():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        code = (payload.get("code") or "").strip()
    else:
        code = (request.args.get("code") or "").strip()

    if not code:
        return jsonify({"status": "error", "error": "missing_code"}), 400

    entry, err = store.check_and_consume(code, allow_reuse=ALLOW_REUSE)
    if err:
        return jsonify({"status": "error", "error": "invalid_or_expired"}), 404

    return jsonify({"status": "ok", "code": entry["code"], "metadata": entry.get("metadata", {})})

@app.route("/purge", methods=["POST"])
def purge():
    auth = require_api_key()
    if auth:
        return auth
    n = store.purge()
    return jsonify({"status": "ok", "purged": n})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
