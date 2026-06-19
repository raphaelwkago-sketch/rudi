# Rudi API server
#
# Env vars required:
#   ANTHROPIC_API_KEY  — your Anthropic key
#   SUPABASE_URL       — https://xxxx.supabase.co
#   SUPABASE_KEY       — service_role key (server-side, never sent to browser)
#   SUPABASE_ANON_KEY  — anon/public key (safe to send to browser for auth)
#
# Set on Windows:  setx SUPABASE_URL "https://xxxx.supabase.co"
# Set on Linux:    export SUPABASE_URL="https://xxxx.supabase.co"

import os, hashlib, secrets
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import store_supabase as store
import rudi

app = Flask(__name__, static_folder='ui')
CORS(app)


def _project_id() -> str | None:
    """Hash the caller's Rudi API key into a project_id."""
    key = request.headers.get("X-API-Key", "").strip()
    if not key:
        return None
    return hashlib.sha256(key.encode()).hexdigest()


def _user_id_from_jwt() -> str | None:
    """Validate a Supabase Auth JWT and return the user's UUID."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    try:
        res = store._sb().auth.get_user(token)
        return str(res.user.id)
    except Exception:
        return None


# ── public config (anon key is safe to expose) ────────────────────────────────

@app.route('/config')
def config():
    return jsonify({
        "supabase_url":      os.environ.get("SUPABASE_URL", ""),
        "supabase_anon_key": os.environ.get("SUPABASE_ANON_KEY", ""),
    })


# ── static dashboard ──────────────────────────────────────────────────────────

@app.route('/')
def serve_index():
    return send_from_directory('ui', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('ui', path)


# ── API key management (requires Supabase Auth JWT) ───────────────────────────

@app.route('/keys', methods=['GET'])
def get_key():
    """Return the current API key for the logged-in user."""
    user_id = _user_id_from_jwt()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    row = store._sb().table("api_keys") \
        .select("key_value, created_at, last_used_at") \
        .eq("user_id", user_id).execute()
    if not row.data:
        return jsonify({"key": None})
    return jsonify(row.data[0])


@app.route('/keys', methods=['POST'])
def create_key():
    """Generate (or rotate) an API key for the logged-in user."""
    user_id = _user_id_from_jwt()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    key = f"rudi_sk_{secrets.token_urlsafe(32)}"
    project_id = hashlib.sha256(key.encode()).hexdigest()
    store._sb().table("api_keys").upsert({
        "user_id":    user_id,
        "key_value":  key,
        "project_id": project_id,
    }, on_conflict="user_id").execute()
    return jsonify({"key": key, "project_id": project_id})


# ── product endpoints ─────────────────────────────────────────────────────────

@app.route('/slice', methods=['POST'])
def get_slice():
    """Get the context slice to inject before your LLM call.

    Request:  POST /slice
              X-API-Key: rudi_sk_...
              {"task": "user's message"}

    Response: {"context": "...", "prompt": "...", "system": "...",
               "turn": N, "inject_ids": [...], "mode": "A"|"B"}
    """
    pid = _project_id()
    if not pid:
        return jsonify({"error": "Missing X-API-Key header"}), 401
    data = request.json or {}
    task = data.get('task', '').strip()
    if not task:
        return jsonify({"error": "task is required"}), 400
    try:
        result = rudi.get_slice(task, pid)
        store._sb().table("api_keys") \
            .update({"last_used_at": "now()"}).eq("project_id", pid).execute()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/decisions', methods=['POST'])
def post_decisions():
    """Store decisions from your LLM response and run fold.

    Request:  POST /decisions
              X-API-Key: rudi_sk_...
              {"decisions": [...], "inject_ids": [...]}

    Response: {"added": [...], "turn": N, "fold_events": [...], "total_nodes": N}
    """
    pid = _project_id()
    if not pid:
        return jsonify({"error": "Missing X-API-Key header"}), 401
    data = request.json or {}
    decisions  = data.get('decisions', [])
    inject_ids = data.get('inject_ids', [])
    try:
        result = rudi.store_decisions(decisions, pid, inject_ids=inject_ids)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── legacy: full LLM chat (for local testing only) ───────────────────────────

@app.route('/api/nodes', methods=['GET'])
def get_nodes():
    pid = _project_id()
    if not pid:
        return jsonify({"error": "Missing X-API-Key header"}), 401
    return jsonify({"nodes": store.all_nodes(pid)})


@app.route('/api/chat', methods=['POST'])
def chat():
    pid = _project_id()
    if not pid:
        return jsonify({"error": "Missing X-API-Key header"}), 401
    data = request.json or {}
    task = data.get('task', '').strip()
    if not task:
        return jsonify({"error": "task is required"}), 400
    try:
        return jsonify(rudi.run_turn(task, project_id=pid))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("Rudi API running on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
