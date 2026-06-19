"""
store.py — the persistent map. SQLite, so the map survives across turns/restarts.

A node = one decision: id, text, what it depends_on, what it revises, status
(open/superseded), the turn it was made, and whether it's a pinned foundation.
"""

import sqlite3, json, os, hashlib

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rudi.db")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS nodes(
            id TEXT PRIMARY KEY,
            text TEXT,
            depends_on TEXT,        -- json list of ids
            revises TEXT,           -- id this supersedes, or NULL
            exception_to TEXT,      -- id this is a narrow exception to, or NULL
            status TEXT DEFAULT 'open',   -- 'open' | 'superseded' | 'folded' | 'stub'
            turn INTEGER,
            pinned INTEGER DEFAULT 0,
            hard_rules TEXT DEFAULT '[]',
            reinforcement_count INTEGER DEFAULT 0,
            activation_contexts TEXT DEFAULT '[]',
            last_activated INTEGER DEFAULT 0)""")
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN hard_rules TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN reinforcement_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN activation_contexts TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE nodes ADD COLUMN last_activated INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        c.execute("CREATE TABLE IF NOT EXISTS failed_folds(hash TEXT PRIMARY KEY)")

def _hash_cluster(cluster_ids):
    s = ",".join(sorted(cluster_ids))
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def mark_fold_failed(cluster_ids):
    h = _hash_cluster(cluster_ids)
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO failed_folds(hash) VALUES(?)", (h,))

def has_fold_failed(cluster_ids):
    h = _hash_cluster(cluster_ids)
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM failed_folds WHERE hash=?", (h,)).fetchone())


def get_turn():
    with _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k='turn'").fetchone()
        return int(r["v"]) if r else 0


def set_turn(n):
    with _conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES('turn',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(n),))


def all_nodes():
    with _conn() as c:
        rows = c.execute("SELECT * FROM nodes").fetchall()
    return {r["id"]: {
        "id": r["id"], "text": r["text"],
        "depends_on": json.loads(r["depends_on"] or "[]"),
        "revises": r["revises"], "exception_to": r["exception_to"], "status": r["status"],
        "turn": r["turn"], "pinned": r["pinned"],
        "hard_rules": json.loads(r["hard_rules"] or "[]"),
        "reinforcement_count": r["reinforcement_count"] or 0,
        "activation_contexts": json.loads(r["activation_contexts"] or "[]"),
        "last_activated": r["last_activated"] or r["turn"]} for r in rows}


def add_node(id, text, depends_on, revises, exception_to, turn, hard_rules=None):
    with _conn() as c:
        prev = c.execute("SELECT status, pinned FROM nodes WHERE id=?", (id,)).fetchone()
        status = prev["status"] if prev else "open"
        pinned = prev["pinned"] if prev else 0
        c.execute("""
            INSERT INTO nodes(id, text, depends_on, revises, exception_to, status, turn, pinned, hard_rules)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                text=excluded.text,
                depends_on=excluded.depends_on,
                revises=excluded.revises,
                exception_to=excluded.exception_to,
                status=excluded.status,
                turn=excluded.turn,
                pinned=excluded.pinned,
                hard_rules=excluded.hard_rules
        """, (id, text, json.dumps(depends_on or []), revises, exception_to, status, turn, pinned, json.dumps(hard_rules or [])))
        if revises:
            c.execute("UPDATE nodes SET status='superseded' WHERE id=?", (revises,))

def add_nodes_transactional(nodes_data, turn):
    if not nodes_data:
        return
        
    conn = _conn()
    conn.isolation_level = None # Explicit control
    c = conn.cursor()
    try:
        c.execute("BEGIN TRANSACTION")
        
        for d in nodes_data:
            id = d.get("id")
            if not id: continue
            
            text = d.get("text", "")
            depends_on = d.get("depends_on", [])
            
            revises = d.get("revises")
            if isinstance(revises, list): revises = revises[0] if revises else None
                
            exception_to = d.get("exception_to")
            if isinstance(exception_to, list): exception_to = exception_to[0] if exception_to else None
                
            hard_rules = d.get("hard_rules", [])
            
            prev = c.execute("SELECT status, pinned FROM nodes WHERE id=?", (id,)).fetchone()
            status = prev["status"] if prev else "open"
            pinned = prev["pinned"] if prev else 0
            
            c.execute("""
                INSERT INTO nodes(id, text, depends_on, revises, exception_to, status, turn, pinned, hard_rules, reinforcement_count, activation_contexts, last_activated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text,
                    depends_on=excluded.depends_on,
                    revises=excluded.revises,
                    exception_to=excluded.exception_to,
                    status=excluded.status,
                    turn=excluded.turn,
                    pinned=excluded.pinned,
                    hard_rules=excluded.hard_rules
            """, (id, text, json.dumps(depends_on or []), revises, exception_to, status, turn, pinned, json.dumps(hard_rules or []), 0, "[]", turn))
            
            if revises:
                c.execute("UPDATE nodes SET status='superseded' WHERE id=?", (revises,))
                
        # Verification
        ids = [d["id"] for d in nodes_data if d.get("id")]
        placeholders = ",".join(["?"] * len(ids))
        row = c.execute(f"SELECT count(id) as cnt FROM nodes WHERE id IN ({placeholders})", ids).fetchone()
        
        if row["cnt"] != len(ids):
            raise RuntimeError(f"CRITICAL: Persistence verification failed. Expected {len(ids)} nodes, read back {row['cnt']}.")
            
        c.execute("COMMIT")
    except Exception as e:
        c.execute("ROLLBACK")
        print(f"[store] TRANSACTION ROLLED BACK due to: {e}")
        raise
    finally:
        conn.close()

def reinforce_nodes(node_ids, turn, context_tag, is_durable=True):
    if not node_ids:
        return
    
    # Remove duplicates
    node_ids = list(set(node_ids))
    with _conn() as c:
        for nid in node_ids:
            row = c.execute("SELECT reinforcement_count, activation_contexts FROM nodes WHERE id=?", (nid,)).fetchone()
            if not row:
                continue
                
            count = row["reinforcement_count"] or 0
            contexts = json.loads(row["activation_contexts"] or "[]")
            
            if is_durable:
                count += 1
                
            contexts.append(context_tag)
            if len(contexts) > 5:
                contexts = contexts[-5:]
                
            c.execute("""
                UPDATE nodes 
                SET last_activated=?, reinforcement_count=?, activation_contexts=?
                WHERE id=?
            """, (turn, count, json.dumps(contexts), nid))
def set_pinned(id, val):
    with _conn() as c:
        c.execute("UPDATE nodes SET pinned=? WHERE id=?", (1 if val else 0, id))


def pinned_ids():
    with _conn() as c:
        return [r["id"] for r in c.execute("SELECT id FROM nodes WHERE pinned=1").fetchall()]


def pin_foundations(min_indegree=3, floor_turn=2):
    """Protect load-bearing decisions from ever being folded.

    A node is pinned (one-way — never unpinned) if ANY of:
      1. Its in-degree >= min_indegree (many other nodes depend on it via
         depends_on — NOT revises, which is supersession noise).
      2. It is the target of an exception_to link (the parent policy is
         by definition still standing and load-bearing).
      3. It was created in turn <= floor_turn (safety floor: early foundations
         like auth/db are protected before enough dependents accrue).

    Once pinned, a node stays pinned forever. This is intentional — a decision
    that was once load-bearing doesn't become safe to fold just because later
    nodes stopped referencing it directly.
    """
    nodes = all_nodes()
    active = {nid: n for nid, n in nodes.items()
              if n["status"] not in ("folded",)}

    # Compute in-degree: count of other active nodes that list this node
    # in their depends_on (NOT revises — that's supersession, not dependency).
    indegree = {}
    exception_targets = set()
    for n in active.values():
        for dep in n["depends_on"]:
            indegree[dep] = indegree.get(dep, 0) + 1
        if n.get("exception_to"):
            exception_targets.add(n["exception_to"])

    to_pin = set()
    for nid, n in active.items():
        if n["pinned"]:
            continue  # already pinned, skip
        if n.get("reinforcement_count", 0) >= 3:
            to_pin.add(nid)
        elif nid in exception_targets:
            to_pin.add(nid)
        elif n["turn"] > 0 and n["turn"] <= floor_turn:
            to_pin.add(nid)

    if to_pin:
        with _conn() as c:
            for nid in to_pin:
                c.execute("UPDATE nodes SET pinned=1 WHERE id=?", (nid,))


def next_id():
    n = 0
    for k in all_nodes():
        try:
            n = max(n, int(k.lstrip("d")))
        except ValueError:
            pass
    return f"d{n + 1}"


def reachable(seeds, cross_stubs=False):
    """Backward reachability over depends_on and revises edges."""
    nodes = all_nodes()
    seen, stack = set(), list(seeds)
    while stack:
        x = stack.pop()
        if x in nodes and x not in seen:
            seen.add(x)
            if not cross_stubs and nodes[x]["status"] == "stub":
                continue  # R1: Stop traversal at stub boundary
            stack.extend(nodes[x]["depends_on"])
            if nodes[x]["revises"]:
                stack.append(nodes[x]["revises"])
    return seen

def get_dead_clusters(active_front_k=5, min_age_x=10, max_cluster_size=8):
    nodes = all_nodes()
    current_turn = get_turn()
    
    front_ids = [n["id"] for n in nodes.values() if n["turn"] >= current_turn - active_front_k and n["status"] != "folded"]
    alive = reachable(front_ids, cross_stubs=True)
    alive.update(pinned_ids())
    
    dead = []
    for n in nodes.values():
        if n["status"] in ("folded", "stub") or n["pinned"]:
            continue
            
        effective_idle_turns = current_turn - n.get("last_activated", n["turn"])
        required_idle = max(5, 10 + n.get("reinforcement_count", 0) * 3)
        
        if effective_idle_turns >= required_idle and n["id"] not in alive:
            dead.append(n)
    
    if not dead:
        return []
        
    dead_ids = {n["id"] for n in dead}
    
    adj = {did: set() for did in dead_ids}
    for did in dead_ids:
        n = nodes[did]
        for dep in n["depends_on"]:
            if dep in dead_ids:
                adj[did].add(dep)
                adj[dep].add(did)
        if n["revises"] and n["revises"] in dead_ids:
            adj[did].add(n["revises"])
            adj[n["revises"]].add(did)
            
    visited = set()
    components = []
    for did in dead_ids:
        if did not in visited:
            comp = set()
            stack = [did]
            while stack:
                curr = stack.pop()
                if curr not in comp:
                    comp.add(curr)
                    stack.extend(adj[curr] - comp)
            visited.update(comp)
            components.append(comp)

    # Split oversized components into sub-clusters by temporal proximity.
    # Sort nodes by turn, cut into chunks of <= max_cluster_size.
    # If the last chunk has < 3 nodes, merge it into the previous chunk
    # so it doesn't get stranded (fold.py skips clusters < 3).
    clusters = []
    for comp in components:
        if len(comp) <= max_cluster_size:
            clusters.append([nodes[cid] for cid in comp])
        else:
            sorted_ids = sorted(comp, key=lambda cid: nodes[cid]["turn"])
            chunks = []
            for j in range(0, len(sorted_ids), max_cluster_size):
                chunks.append(sorted_ids[j:j + max_cluster_size])
            # Merge small remnant into previous chunk
            if len(chunks) > 1 and len(chunks[-1]) < 3:
                chunks[-2].extend(chunks[-1])
                chunks.pop()
            for chunk in chunks:
                clusters.append([nodes[cid] for cid in chunk])

    return clusters

def fold_nodes(cluster_ids, stub_text, hard_rules=None):
    if not cluster_ids: return None
    nodes = all_nodes()
    turn = get_turn()
    sid = next_id()
    
    # Gather all dependencies to keep the chain intact
    deps = set()
    for did in cluster_ids:
        deps.update(nodes[did]["depends_on"])
        if nodes[did]["revises"]:
            deps.add(nodes[did]["revises"])
            
    # External foundations the cluster rested on (keep the chain upward intact)…
    ext_deps = list(deps - set(cluster_ids))
    # …PLUS the folded children themselves, so R2/expand-on-demand can find the stub
    # that owns a folded node (rudi.folded_territory_stubs checks child-id membership).
    stub_deps = ext_deps + list(cluster_ids)

    with _conn() as c:
        c.execute("INSERT INTO nodes(id,text,depends_on,revises,exception_to,status,turn,pinned,hard_rules) VALUES(?,?,?,?,?,?,?,?,?)",
                  (sid, stub_text, json.dumps(stub_deps), None, None, "stub", turn, 0, json.dumps(hard_rules or [])))
        for did in cluster_ids:
            c.execute("UPDATE nodes SET status='folded' WHERE id=?", (did,))
            
    return sid


def wipe():
    with _conn() as c:
        c.execute("DROP TABLE IF EXISTS nodes")
        c.execute("DROP TABLE IF EXISTS meta")
        c.execute("DROP TABLE IF EXISTS failed_folds")


# 47-node demo graph from the hard-run, for instant testing (`:seed`).
_SEED = [
    ("d1", [], None, None, "Use JWT-based authentication for the API"),
    ("d2", ["d1"], None, None, "Short-lived access tokens (~15min) signed with HS256"),
    ("d3", ["d1"], None, None, "Long-lived refresh tokens stored in DB for revocation"),
    ("d4", ["d1"], None, None, "Deliver tokens via httpOnly cookies"),
    ("d5", [], None, None, "Hash passwords with bcrypt"),
    ("d6", ["d3"], None, None, "Use PostgreSQL as the primary database"),
    ("d7", ["d6"], None, None, "Use Prisma as the ORM/migration tool"),
    ("d8", [], None, None, "Use pino + pino-http for structured request logging"),
    ("d9", ["d4", "d8"], None, None, "Redact auth cookies/headers and passwords from logs"),
    ("d10", [], None, None, "Expose GET /health endpoint, unauthenticated"),
    ("d11", ["d6", "d10"], None, None, "Health check deep DB ping, 503 if DB unreachable"),
    ("d12", [], None, None, "CORS middleware with explicit origin allowlist"),
    ("d13", ["d4", "d12"], None, None, "CORS credentials:true to allow cookie auth cross-origin"),
    ("d14", [], None, None, "express-rate-limit with global baseline limit"),
    ("d15", ["d2", "d3", "d5", "d14"], None, None, "Stricter rate limit on auth endpoints"),
    ("d16", ["d14"], None, None, "Redis-backed rate-limit store for multiple instances"),
    ("d17", [], None, None, "Zod for request schema validation with inferred TS types"),
    ("d18", ["d17"], None, None, "Validation middleware validates body/query/params at the edge"),
    ("d19", ["d18"], None, None, "Validation failures return 400 with consistent error envelope"),
    ("d20", ["d17"], None, None, "Strip unknown keys to prevent mass-assignment"),
    ("d21", ["d8", "d19"], None, None, "Standard error response object: code, message, details, requestId"),
    ("d22", ["d21"], None, None, "Central error-handling middleware; handlers throw typed AppError"),
    ("d23", ["d22", "d8"], None, None, "Unexpected errors return generic 500; real cause only logged"),
    ("d24", ["d2", "d3", "d14", "d21"], None, None, "Auth failures -> 401, rate-limit -> 429 in error contract"),
    ("d25", ["d3"], None, None, "Log-out-everywhere revokes all refresh-token rows for the user"),
    ("d26", ["d2"], None, None, "tokenVersion claim on access tokens; increment to invalidate all"),
    ("d27", ["d26", "d6"], None, None, "Auth middleware checks tokenVersion against user record"),
    ("d28", ["d4", "d25", "d26"], None, None, "POST /auth/logout-all bumps tokenVersion, clears tokens+cookie"),
    ("d29", ["d6"], None, None, "Offline-first: client is source of truth, server is sync/merge target"),
    ("d30", ["d7", "d29"], None, None, "Notes use client-generated UUID primary keys"),
    ("d31", ["d29", "d6"], None, None, "Soft-delete tombstones (deletedAt) instead of hard deletes; we NEVER hard-delete"),
    ("d32", ["d29"], None, None, "Sync metadata per note: updatedAt, version counter, change cursor"),
    ("d33", ["d29", "d32"], None, None, "Sync endpoints: POST /sync/push and GET /sync/pull?since=cursor"),
    ("d34", ["d32", "d33"], None, None, "Conflict resolution: last-write-wins by updatedAt + version counter"),
    ("d35", ["d30", "d33"], None, None, "Sync push operations must be idempotent using client UUIDs"),
    ("d36", ["d18", "d33"], None, None, "Sync batches validated via existing Zod layer"),
    ("d37", ["d29", "d6"], None, None, "Client local store (IndexedDB/SQLite); server keeps sync store"),
    ("d38", ["d6", "d29"], "d6", None, "Use MongoDB document store as server DB, replacing PostgreSQL"),
    ("d39", ["d7", "d38"], "d7", None, "MongoDB native driver/Mongoose, replacing Prisma"),
    ("d40", ["d30", "d35", "d38"], None, None, "Document _id = client UUID; upsert-by-_id for idempotent sync"),
    ("d41", ["d3", "d27", "d38"], None, None, "Store refresh tokens and tokenVersion in Mongo collections"),
    ("d42", ["d32", "d33", "d38"], None, None, "Per-document monotonic field / change streams for sync cursor"),
    ("d43", ["d11", "d38"], "d11", None, "Deep health check pings MongoDB instead of Postgres"),
    ("d44", ["d29", "d37"], None, None, "Primary notes search runs client-side over local store (offline)"),
    ("d45", ["d38"], None, None, "Server search GET /notes/search?q= via MongoDB text index"),
    ("d46", ["d45"], None, None, "Atlas Search kept as future upgrade path, not adopted now"),
    ("d47", ["d31", "d44", "d45"], None, None, "Search must exclude soft-deleted (tombstoned) docs; users only read their OWN notes"),
]


def seed_demo():
    wipe()
    init()
    for row in _SEED:
        id, deps, revises, exception_to, text = row[:5]
        add_node(id, text, deps, revises, exception_to, 0)
    # pin the two ambient foundations: auth scheme + current DB
    set_pinned("d1", True)
    set_pinned("d38", True)
    set_turn(0)
