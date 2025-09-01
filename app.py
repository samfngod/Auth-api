import os
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from threading import Lock

app = Flask(__name__)

# Hardcoded ADMIN_API_KEY
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "f9A7d3!X2vQ#8LmRp6ZyT0wB1uH4eKjS")
DEFAULT_TTL = int(os.getenv("DEFAULT_TTL_SECONDS", "900"))
ALLOW_REUSE = os.getenv("ALLOW_REUSE", "0") == "1"

_codes = {}
_lock = Lock()

def now():
    return datetime.now(timezone.utc)

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": now().isoformat()})

@app.post("/addcode")
def addcode():
    if not ADMIN_API_KEY or request.headers.get("X-API-KEY") != ADMIN_API_KEY:
        return jsonify({"status": "error", "error": "unauthorized"}), 401
    j = request.get_json(silent=True) or {}
    code = (j.get("code") or "").strip()
    if not code:
        return jsonify({"status":"error","error":"missing_code"}), 400
    ttl = int(j.get("ttl_seconds") or DEFAULT_TTL)
    exp = now() + timedelta(seconds=ttl)
    with _lock:
        _codes[code] = {"expires_at": exp, "used": False, "metadata": j.get("metadata") or {}}
    return jsonify({"status": "added", "code": code, "expires_at": exp.isoformat()})

@app.route("/checkcode", methods=["GET","POST"])
def checkcode():
    code = (request.args.get("code") or (request.get_json(silent=True) or {}).get("code") or "").strip()
    if not code:
        return jsonify({"status":"error","error":"missing_code"}), 400
    with _lock:
        e = _codes.get(code)
        if not e: return jsonify({"status":"error","error":"invalid_or_expired"}), 404
        if e["expires_at"] < now(): return jsonify({"status":"error","error":"invalid_or_expired"}), 404
        if e["used"] and not ALLOW_REUSE: return jsonify({"status":"error","error":"invalid_or_expired"}), 404
        e["used"] = True
        return jsonify({"status": "ok", "code": code, "metadata": e.get("metadata", {})})
@app.get("/")
def index():
    return jsonify({"message": "API running", "endpoints": ["/health", "/addcode", "/checkcode"]})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
