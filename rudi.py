"""
rudi.py — context-map turn loop + product API functions.

API:
  get_slice(task)                      → context to inject before your LLM call
  store_decisions(decisions, inject_ids) → store + fold after your LLM call
  run_turn(task)                       → full LLM call + store in one shot
"""

import sys, re, json
sys.stdout.reconfigure(encoding="utf-8")
from anthropic import Anthropic
import store
import fold

client = Anthropic()
WORKER = "claude-sonnet-4-6"

WORKER_SYS = """You are operating with a persistent memory graph instead of a standard context window.
Treat the provided map of prior decisions as BINDING.

You MUST format your reply using exactly these two sections:

<SOLUTION>
Your natural response to the user.
</SOLUTION>

```json
{"decisions":[{"id":"d<N>","text":"<detailed_technical_summary>","hard_rules":["<rule_1>"],"depends_on":["d.."],"revises":"d..","exception_to":"d.."}]}
```
List only NEW technical decisions made this turn. Output {"decisions": []} if no new decisions are made.
Use "revises" ONLY if this replaces an earlier decision.
Use "exception_to" if this is a narrow carve-out but the parent policy stands.
Omit "revises" and "exception_to" if not applicable.
Use the next free ids (you'll be told which exist).

HARD RULES ARE BINDING. If a prior decision states a hard rule (never / must not / forbidden) and the task cannot be done without violating it, you MUST NOT implement the violating behaviour. Stop and ask the user to explicitly confirm the exception before proceeding.
If you are declaring a new strict security rule, access constraint, or forbidden action, you MUST place it as a string in the "hard_rules" array. Do not hide it in the "text" field."""


def _txt(resp):
    return "".join(b.text for b in resp.content if b.type == "text")


def _json_block(text, key):
    matches = list(re.finditer(r"```json\s*(.*?)```", text, re.S))
    if not matches:
        raise RuntimeError(f"CRITICAL: No JSON block found in output.\nOutput snippet: {text[-500:]}")
    raw = matches[-1].group(1)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"CRITICAL: JSON parse failure: {e}\nRaw block: {raw[:100]}...")
    if not isinstance(obj, dict) or key not in obj:
        raise RuntimeError(f"CRITICAL: JSON block missing required '{key}' key or not a dictionary.")
    return obj[key]


def librarian_select(task: str, active_nodes: list) -> set:
    ctx = "\n".join(f"{n['id']}: {n['text']}" for n in active_nodes)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=200,
        system="Return only a comma-separated list of node IDs relevant to the task.",
        messages=[{"role": "user", "content": f"MAP:\n{ctx}\n\nTASK: {task}\nIDs:"}]
    )
    raw = resp.content[0].text
    seeds = [x.strip() for x in raw.replace(",", " ").split() if x.strip() in [n['id'] for n in active_nodes]]
    return store.reachable(seeds, cross_stubs=False)


def folded_territory_stubs(task: str) -> set:
    nodes = store.all_nodes()
    folded = [n for n in nodes.values() if n["status"] == "folded"]
    task_words = set(w for w in re.findall(r'\w+', task.lower()) if len(w) > 3)
    intersecting_folded = []
    for f in folded:
        f_words = set(re.findall(r'\w+', f["text"].lower()))
        if task_words & f_words:
            intersecting_folded.append(f)
    stubs_to_flag = set()
    for f in intersecting_folded:
        for n in nodes.values():
            if n["status"] == "stub" and f["id"] in n["depends_on"]:
                stubs_to_flag.add(n["id"])
    return stubs_to_flag


# ── product API ───────────────────────────────────────────────────────────────

def get_slice(task: str) -> dict:
    """Return the context slice for a task. Call this BEFORE your LLM API call.

    Returns:
      context     — the formatted node list (human-readable)
      prompt      — the full user message to send as the first message to your LLM
      system      — the system prompt to use (WORKER_SYS)
      turn        — current turn number (pass back to store_decisions)
      inject_ids  — node IDs that were injected (pass back to store_decisions)
      mode        — "A" (all nodes) or "B" (retrieval fallback, >80 nodes)
      active_before — total active node count before this slice
    """
    nodes = store.all_nodes()
    active_nodes = [n for n in nodes.values() if n["status"] != "folded"]
    active_nodes.sort(
        key=lambda n: n.get("last_activated", n.get("turn", 0)) + n.get("reinforcement_count", 0) * 5,
        reverse=True
    )

    ACTIVE_THRESHOLD = 80
    if len(active_nodes) <= ACTIVE_THRESHOLD:
        inject_ids = {n["id"] for n in active_nodes}
        mode = "A"
    else:
        inject_ids = librarian_select(task, active_nodes) | set(store.pinned_ids())
        mode = "B"

    stubs_to_flag = folded_territory_stubs(task)
    inject_ids.update(stubs_to_flag)

    if not inject_ids:
        ctx = "(no prior decisions yet)"
    else:
        ctx_lines = []
        for n_id in sorted(inject_ids, key=lambda x: int(x[1:])):
            n = nodes[n_id]
            line = f"{n_id}: {n['text']}"
            if n.get("pinned"):              line += " [PINNED foundation]"
            if n["status"] == "superseded":  line += " [SUPERSEDED]"
            if n.get("exception_to"):        line += f" [EXCEPTION TO {n['exception_to']}]"
            if n_id in stubs_to_flag:        line += " [FOLDED AREA]"
            ctx_lines.append(line)
        ctx = "\n".join(ctx_lines)

    turn = store.get_turn()
    existing = ", ".join(sorted(inject_ids, key=lambda x: int(x[1:]))) or "(none)"
    prompt = f"MAP OF PRIOR DECISIONS:\n{ctx}\n\n[existing ids: {existing}]\n\nTASK: {task}"

    return {
        "context":       ctx,
        "prompt":        prompt,
        "system":        WORKER_SYS,
        "turn":          turn,
        "inject_ids":    sorted(inject_ids, key=lambda x: int(x[1:])),
        "mode":          mode,
        "active_before": len(active_nodes),
    }


def store_decisions(decisions: list, inject_ids: list | None = None) -> dict:
    """Store decisions from your LLM response and run fold. Call this AFTER your LLM call.

    Args:
      decisions   — list of decision dicts from the LLM JSON block
      inject_ids  — the inject_ids returned by get_slice (for reinforcement tracking)

    Returns:
      added       — list of node IDs that were stored this turn
      turn        — the turn number that was committed
      fold_events — list of fold events (stubs created)
      total_nodes — total node count after this turn
    """
    turn = store.get_turn() + 1
    inject_ids = inject_ids or []

    added = []
    if decisions:
        store.add_nodes_transactional(decisions, turn)
        for d in decisions:
            if d.get("id"):
                added.append(d["id"] + (" (revises)" if d.get("revises") else ""))

    if decisions:
        deps = set()
        for d in decisions:
            deps.update(d.get("depends_on", []))
            if d.get("revises"):      deps.add(d["revises"])
            if d.get("exception_to"): deps.add(d["exception_to"])
        deps_list = list(deps)
        if deps_list:
            store.reinforce_nodes(deps_list, turn,
                                  f"Cited by {len(decisions)} new decision(s)", is_durable=True)
        other_active = [nid for nid in inject_ids if nid not in set(deps_list)]
        if other_active:
            store.reinforce_nodes(other_active, turn,
                                  "Active context during decision", is_durable=False)
    elif inject_ids:
        store.reinforce_nodes(inject_ids, turn, "Context during Q&A", is_durable=False)

    store.set_turn(turn)
    store.pin_foundations()

    fold_events = fold.run_fold()

    return {
        "added":       added,
        "turn":        turn,
        "fold_events": fold_events,
        "total_nodes": len(store.all_nodes()),
    }


# ── internal: full LLM turn (used by /api/chat for local testing) ─────────────

def run_turn(task: str) -> dict:
    slice_data = get_slice(task)
    print(f"\n[Mode {slice_data['mode']}: injecting {len(slice_data['inject_ids'])} nodes, "
          f"active_before={slice_data['active_before']}]")

    messages = [{"role": "user", "content": slice_data["prompt"]}]
    out, tokens_in, tokens_out = "", 0, 0
    while True:
        resp = client.messages.create(
            model=WORKER, max_tokens=8192, system=WORKER_SYS, messages=messages
        )
        piece = _txt(resp)
        out += piece
        tokens_in  += resp.usage.input_tokens
        tokens_out += resp.usage.output_tokens
        if getattr(resp, "stop_reason", None) == "max_tokens":
            print("[rudi] max_tokens hit, requesting continuation...")
            messages.append({"role": "assistant", "content": piece})
            messages.append({"role": "user",      "content": "Continue."})
        else:
            break

    out = out.strip()
    print("\n" + "=" * 80 + "\n" + out + "\n" + "=" * 80)

    decs = _json_block(out, "decisions")
    result = store_decisions(decs, inject_ids=slice_data["inject_ids"])
    print(f"\n[map updated: +{len(result['added'])} node(s): {result['added'] or '—'}]  "
          f"total nodes: {result['total_nodes']}")

    solution_match = re.search(r"<SOLUTION>\s*(.*?)\s*</SOLUTION>", out, re.S)
    if solution_match:
        display_text = solution_match.group(1).strip()
    else:
        display_text = re.sub(r"```json.*?```", "", out, flags=re.S).strip()
        display_text = re.sub(r"(?s).*?(?:<SOLUTION>|2\.\s*SOLUTION[\s:-]*)", "", display_text).strip()
        if not display_text:
            display_text = out

    return {
        "output":        out,
        "display":       display_text,
        "tokens_in":     tokens_in,
        "tokens_out":    tokens_out,
        "mode":          slice_data["mode"],
        "active_before": slice_data["active_before"],
    }
