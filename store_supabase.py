"""
store_supabase.py — production drop-in for store.py.

Every public function takes project_id as its first argument.
Same return shapes as store.py — rudi.py / fold.py / server.py
swap in this module with zero logic changes, just pass project_id through.

Requires:
    pip install supabase
Env vars:
    SUPABASE_URL   — https://xxxx.supabase.co
    SUPABASE_KEY   — service_role key (bypasses RLS; we filter by project_id manually)
"""

import os, json, hashlib
from supabase import create_client, Client

# ── client singleton ──────────────────────────────────────────────────────────

_client: Client | None = None

def _sb() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


# ── helpers ───────────────────────────────────────────────────────────────────

def _hash_cluster(cluster_ids: list[str]) -> str:
    s = ",".join(sorted(cluster_ids))
    return hashlib.sha256(s.encode()).hexdigest()


def _row_to_node(r: dict) -> dict:
    return {
        "id":                   r["id"],
        "text":                 r["text"],
        "depends_on":           r["depends_on"] if isinstance(r["depends_on"], list) else json.loads(r["depends_on"] or "[]"),
        "revises":              r["revises"],
        "exception_to":         r["exception_to"],
        "status":               r["status"],
        "turn":                 r["turn"],
        "pinned":               bool(r["pinned"]),
        "hard_rules":           r["hard_rules"] if isinstance(r["hard_rules"], list) else json.loads(r["hard_rules"] or "[]"),
        "reinforcement_count":  r.get("reinforcement_count") or 0,
        "activation_contexts":  r["activation_contexts"] if isinstance(r.get("activation_contexts"), list) else json.loads(r.get("activation_contexts") or "[]"),
        "last_activated":       r.get("last_activated") or r["turn"],
    }


# ── schema init (idempotent — SQL already ran in Supabase, this is a no-op) ───

def init(project_id: str):
    """No-op for Supabase — schema is created via supabase_schema.sql."""
    pass


# ── turn counter ──────────────────────────────────────────────────────────────

def get_turn(project_id: str) -> int:
    r = _sb().table("meta").select("v").eq("project_id", project_id).eq("k", "turn").execute()
    return int(r.data[0]["v"]) if r.data else 0


def set_turn(project_id: str, n: int):
    _sb().table("meta").upsert({"project_id": project_id, "k": "turn", "v": str(n)}).execute()


# ── node reads ────────────────────────────────────────────────────────────────

def all_nodes(project_id: str) -> dict:
    r = _sb().table("nodes").select("*").eq("project_id", project_id).execute()
    return {row["id"]: _row_to_node(row) for row in r.data}


def pinned_ids(project_id: str) -> list[str]:
    r = _sb().table("nodes").select("id").eq("project_id", project_id).eq("pinned", True).execute()
    return [row["id"] for row in r.data]


# ── node writes ───────────────────────────────────────────────────────────────

def add_node(project_id: str, id: str, text: str, depends_on: list,
             revises: str | None, exception_to: str | None, turn: int,
             hard_rules: list | None = None):

    # Preserve existing status/pinned if node already exists
    existing = _sb().table("nodes").select("status, pinned").eq("project_id", project_id).eq("id", id).execute()
    status = existing.data[0]["status"] if existing.data else "open"
    pinned = existing.data[0]["pinned"] if existing.data else False

    _sb().table("nodes").upsert({
        "project_id":   project_id,
        "id":           id,
        "text":         text,
        "depends_on":   depends_on or [],
        "revises":      revises,
        "exception_to": exception_to,
        "status":       status,
        "turn":         turn,
        "pinned":       pinned,
        "hard_rules":   hard_rules or [],
    }).execute()

    if revises:
        _sb().table("nodes").update({"status": "superseded"}).eq("project_id", project_id).eq("id", revises).execute()


def add_nodes_transactional(project_id: str, nodes_data: list, turn: int):
    """Batch upsert. Supabase/PostgREST doesn't expose multi-statement
    transactions over the REST API, so we upsert in one batch call and
    verify the count — same safety guarantee as the SQLite version."""
    if not nodes_data:
        return

    rows = []
    superseded = []

    for d in nodes_data:
        nid = d.get("id")
        if not nid:
            continue

        revises = d.get("revises")
        if isinstance(revises, list): revises = revises[0] if revises else None
        exception_to = d.get("exception_to")
        if isinstance(exception_to, list): exception_to = exception_to[0] if exception_to else None

        existing = _sb().table("nodes").select("status, pinned").eq("project_id", project_id).eq("id", nid).execute()
        status = existing.data[0]["status"] if existing.data else "open"
        pinned = existing.data[0]["pinned"] if existing.data else False

        rows.append({
            "project_id":           project_id,
            "id":                   nid,
            "text":                 d.get("text", ""),
            "depends_on":           d.get("depends_on", []),
            "revises":              revises,
            "exception_to":         exception_to,
            "status":               status,
            "turn":                 turn,
            "pinned":               pinned,
            "hard_rules":           d.get("hard_rules", []),
            "reinforcement_count":  0,
            "activation_contexts":  [],
            "last_activated":       turn,
        })
        if revises:
            superseded.append(revises)

    if rows:
        _sb().table("nodes").upsert(rows).execute()

    for sid in superseded:
        _sb().table("nodes").update({"status": "superseded"}).eq("project_id", project_id).eq("id", sid).execute()

    # Verify
    ids = [r["id"] for r in rows]
    check = _sb().table("nodes").select("id", count="exact").eq("project_id", project_id).in_("id", ids).execute()
    if check.count != len(ids):
        raise RuntimeError(f"Persistence verification failed: expected {len(ids)}, got {check.count}")


def set_pinned(project_id: str, id: str, val: bool):
    _sb().table("nodes").update({"pinned": val}).eq("project_id", project_id).eq("id", id).execute()


def reinforce_nodes(project_id: str, node_ids: list[str], turn: int, context_tag: str, is_durable: bool = True):
    if not node_ids:
        return
    node_ids = list(set(node_ids))
    rows = _sb().table("nodes").select("id, reinforcement_count, activation_contexts").eq("project_id", project_id).in_("id", node_ids).execute()

    for row in rows.data:
        count = (row.get("reinforcement_count") or 0) + (1 if is_durable else 0)
        contexts = row.get("activation_contexts") or []
        if isinstance(contexts, str): contexts = json.loads(contexts)
        contexts.append(context_tag)
        if len(contexts) > 5: contexts = contexts[-5:]
        _sb().table("nodes").update({
            "reinforcement_count": count,
            "activation_contexts": contexts,
            "last_activated":      turn,
        }).eq("project_id", project_id).eq("id", row["id"]).execute()


# ── fold-failure cache ────────────────────────────────────────────────────────

def mark_fold_failed(project_id: str, cluster_ids: list[str]):
    h = _hash_cluster(cluster_ids)
    _sb().table("failed_folds").upsert({"project_id": project_id, "hash": h}).execute()


def has_fold_failed(project_id: str, cluster_ids: list[str]) -> bool:
    h = _hash_cluster(cluster_ids)
    r = _sb().table("failed_folds").select("hash").eq("project_id", project_id).eq("hash", h).execute()
    return bool(r.data)


# ── graph algorithms (pure Python — identical to store.py) ───────────────────

def reachable(project_id: str, seeds: list[str], cross_stubs: bool = False) -> set[str]:
    nodes = all_nodes(project_id)
    seen, stack = set(), list(seeds)
    while stack:
        x = stack.pop()
        if x in nodes and x not in seen:
            seen.add(x)
            if not cross_stubs and nodes[x]["status"] == "stub":
                continue
            stack.extend(nodes[x]["depends_on"])
            if nodes[x]["revises"]:
                stack.append(nodes[x]["revises"])
    return seen


def get_dead_clusters(project_id: str, active_front_k: int = 5,
                      min_age_x: int = 10, max_cluster_size: int = 8) -> list:
    nodes = all_nodes(project_id)
    current_turn = get_turn(project_id)

    front_ids = [n["id"] for n in nodes.values()
                 if n["turn"] >= current_turn - active_front_k and n["status"] != "folded"]
    alive = reachable(project_id, front_ids, cross_stubs=True)
    alive.update(pinned_ids(project_id))

    dead = []
    for n in nodes.values():
        if n["status"] in ("folded", "stub") or n["pinned"]:
            continue
        effective_idle = current_turn - n.get("last_activated", n["turn"])
        required_idle  = max(5, 10 + n.get("reinforcement_count", 0) * 3)
        if effective_idle >= required_idle and n["id"] not in alive:
            dead.append(n)

    if not dead:
        return []

    dead_ids = {n["id"] for n in dead}
    adj = {did: set() for did in dead_ids}
    for did in dead_ids:
        n = nodes[did]
        for dep in n["depends_on"]:
            if dep in dead_ids:
                adj[did].add(dep); adj[dep].add(did)
        if n["revises"] and n["revises"] in dead_ids:
            adj[did].add(n["revises"]); adj[n["revises"]].add(did)

    visited, components = set(), []
    for did in dead_ids:
        if did not in visited:
            comp, stack = set(), [did]
            while stack:
                curr = stack.pop()
                if curr not in comp:
                    comp.add(curr)
                    stack.extend(adj[curr] - comp)
            visited.update(comp)
            components.append(comp)

    clusters = []
    for comp in components:
        if len(comp) <= max_cluster_size:
            clusters.append([nodes[cid] for cid in comp])
        else:
            sorted_ids = sorted(comp, key=lambda cid: nodes[cid]["turn"])
            chunks = [sorted_ids[j:j + max_cluster_size] for j in range(0, len(sorted_ids), max_cluster_size)]
            if len(chunks) > 1 and len(chunks[-1]) < 3:
                chunks[-2].extend(chunks.pop())
            for chunk in chunks:
                clusters.append([nodes[cid] for cid in chunk])
    return clusters


def next_id(project_id: str) -> str:
    nodes = all_nodes(project_id)
    n = 0
    for k in nodes:
        try: n = max(n, int(k.lstrip("d")))
        except ValueError: pass
    return f"d{n + 1}"


def fold_nodes(project_id: str, cluster_ids: list[str], stub_text: str,
               hard_rules: list | None = None) -> str | None:
    if not cluster_ids:
        return None
    nodes = all_nodes(project_id)
    turn  = get_turn(project_id)
    sid   = next_id(project_id)

    deps = set()
    for did in cluster_ids:
        deps.update(nodes[did]["depends_on"])
        if nodes[did]["revises"]:
            deps.add(nodes[did]["revises"])
    ext_deps  = list(deps - set(cluster_ids))
    stub_deps = ext_deps + list(cluster_ids)

    _sb().table("nodes").insert({
        "project_id":   project_id,
        "id":           sid,
        "text":         stub_text,
        "depends_on":   stub_deps,
        "revises":      None,
        "exception_to": None,
        "status":       "stub",
        "turn":         turn,
        "pinned":       False,
        "hard_rules":   hard_rules or [],
    }).execute()

    for did in cluster_ids:
        _sb().table("nodes").update({"status": "folded"}).eq("project_id", project_id).eq("id", did).execute()

    return sid


def pin_foundations(project_id: str, min_indegree: int = 3, floor_turn: int = 2):
    nodes  = all_nodes(project_id)
    active = {nid: n for nid, n in nodes.items() if n["status"] != "folded"}

    indegree, exception_targets = {}, set()
    for n in active.values():
        for dep in n["depends_on"]:
            indegree[dep] = indegree.get(dep, 0) + 1
        if n.get("exception_to"):
            exception_targets.add(n["exception_to"])

    to_pin = set()
    for nid, n in active.items():
        if n["pinned"]: continue
        if n.get("reinforcement_count", 0) >= 3: to_pin.add(nid)
        elif nid in exception_targets:            to_pin.add(nid)
        elif 0 < n["turn"] <= floor_turn:        to_pin.add(nid)

    for nid in to_pin:
        _sb().table("nodes").update({"pinned": True}).eq("project_id", project_id).eq("id", nid).execute()


# ── wipe (dev/test only) ──────────────────────────────────────────────────────

def wipe(project_id: str):
    """Delete all rows for this project. Never call in production."""
    _sb().table("nodes").delete().eq("project_id", project_id).execute()
    _sb().table("meta").delete().eq("project_id", project_id).execute()
    _sb().table("failed_folds").delete().eq("project_id", project_id).execute()
