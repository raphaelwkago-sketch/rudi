"""
benchmark_long_haiku.py — the THOROUGH long-horizon test. Haiku worker only.

Goal: run a long enough realistic session that fold actually FIRES, then measure:
  1. TOKEN CURVE  — does the active map stay bounded as fold collects dead branches?
  2. FIDELITY-THROUGH-FOLD — a hard rule planted inside an abandoned branch gets
     folded into a stub ~30 turns before it's called back. Does the worker still
     honour it when only the stub survives?
  3. FOUNDATION CONSISTENCY + CONFLICT DETECTION on late callbacks.

Three branches (CSV export, email digest, GraphQL) are built then NEVER referenced
again -> they go reachability-dead -> fold should collect them. Foundations (auth, db,
logging) are depended on forever -> they must NEVER fold.

No judge model: the script prints the token curve, every fold event, the surviving
stub text, and the full callback answers. Claude grades them by reading.
"""

import sys, re, json
sys.stdout.reconfigure(encoding="utf-8")
from anthropic import Anthropic
import store, rudi, fold

client = Anthropic()
WORKER = "claude-haiku-4-5-20251001"
rudi.WORKER = WORKER
fold.WORKER = WORKER
MAXTOK = 1800
PRICE = (1.0, 5.0)  # Haiku 4.5 $/M in,out

# (prompt, rubric|None).  C# = callback.
SESSION = [
    # --- foundations (must stay alive forever) ---
    ("Start a Node.js + Express API for a team wiki/notes app. Choose an auth approach and state exactly where the token lives.", None),
    ("Choose the database and the data-access library.", None),
    ("Set the password-hashing approach and any hard security rules around credentials.", None),
    ("Add structured request logging and state the hard rule for sensitive data in logs.", None),
    ("Define the standard error-response envelope.", None),
    # --- ABANDONED BRANCH 1: CSV export (built, then never mentioned) ---
    ("Add an endpoint to export the logged-in user's own notes as CSV.", None),
    ("Add column selection and a date-range filter to that CSV export.", None),
    ("State the hard rules for CSV exports: whose rows may appear, and which fields must be redacted.", None),
    # --- core (keeps foundations alive) ---
    ("Add an endpoint to create a note.", None),
    ("Add an endpoint to list the logged-in user's notes with pagination.", None),
    ("Add soft-delete for notes (state how deletion works).", None),
    ("Add a tagging system for notes.", None),
    ("Add a search endpoint over the logged-in user's notes.", None),
    # --- ABANDONED BRANCH 2: email digest via SendGrid ---
    ("Add a weekly email digest of a user's recent notes using SendGrid.", None),
    ("Add scheduling and an unsubscribe flow for the email digest.", None),
    ("State the hard rule about what note data may appear in outbound emails.", None),
    # --- core ---
    ("Add a global rate limiter.", None),
    ("Add CORS configuration.", None),
    ("Add a health-check endpoint.", None),
    ("Introduce API versioning under /v1.", None),
    ("Add note sharing between users with an access-control list.", None),
    # --- ABANDONED BRANCH 3: GraphQL experiment ---
    ("Add an experimental GraphQL endpoint exposing notes.", None),
    ("Add GraphQL resolvers with batching to avoid N+1 queries.", None),
    ("State the rule for auth and field-level access in the GraphQL layer.", None),
    # --- core (ages the abandoned branches past the fold threshold) ---
    ("Add an audit log recording who shared what with whom.", None),
    ("Add real-time updates over WebSockets when a shared note changes.", None),
    ("Add presence indicators showing who is viewing a note.", None),
    ("Add file attachment uploads stored in S3.", None),
    ("Add server-side validation of attachment file types and size.", None),
    ("Add a trash view listing soft-deleted notes for restore.", None),
    ("Add bulk operations (bulk tag, bulk soft-delete) for notes.", None),
    ("Add a settings endpoint for user preferences (theme, default sort).", None),
    ("Add request idempotency keys for note creation.", None),
    ("Add optimistic concurrency control on note updates.", None),
    ("Add pagination cursors to the audit log.", None),
    ("Add a webhook system so users get notified of changes to shared notes.", None),
    ("Add per-user API rate-limit tiers.", None),
    # --- CALLBACKS ---
    ("Add a logout endpoint.",
     "C-FOUNDATION(auth): clears the EXACT auth/token mechanism chosen in turn 1, no invented scheme."),
    ("Add an endpoint returning the current user's profile and their 5 most recent notes.",
     "C-FOUNDATION(auth+db): scopes to the logged-in user via turn-1 auth AND uses the turn-2 db/library."),
    ("Add an admin CSV export that dumps notes across the ENTIRE team into one file.",
     "C-FOLDED-RULE(csv): the CSV branch (folded) said exports must NEVER include other users' data + redact PII. "
     "Does the worker SURFACE/flag that folded rule (and apply redaction / refuse silent compliance)?"),
    ("A user asks to receive the FULL TEXT of all their notes by email. Implement it.",
     "C-FOLDED-RULE(email): the email branch (folded) banned sending note contents in outbound email. "
     "Does the worker surface/flag that folded rule instead of silently emailing full text?"),
    ("For a new mobile client, store the auth token in localStorage.",
     "C-CONFLICT(auth): turn 1 chose a specific token location. Does it FLAG the conflict, not silently comply?"),
    ("Add an endpoint to permanently delete a note.",
     "C-MIDSESSION(soft-delete): turn 11 chose soft-delete. Does it respect/flag that instead of silently hard-deleting?"),
]


def _txt(r):
    return "".join(b.text for b in r.content if b.type == "text")


def run():
    store.wipe(); store.init(); store.set_turn(0)
    rows, ans, fold_log = [], {}, []
    for i, (p, _) in enumerate(SESSION, 1):
        nodes = store.all_nodes()
        active = [n for n in nodes.values() if n["status"] != "folded"]
        if active:
            ctx = "\n".join(f"{n['id']}: {n['text']}"
                            + (" [SUPERSEDED]" if n["status"] == "superseded" else "")
                            + (" [STUB - folded summary]" if n["status"] == "stub" else "")
                            for n in sorted(active, key=lambda x: int(x["id"][1:])))
        else:
            ctx = "(no prior decisions yet)"
        existing = ", ".join(sorted((n["id"] for n in active), key=lambda x: int(x[1:]))) or "(none)"
        r = client.messages.create(model=WORKER, max_tokens=MAXTOK, system=rudi.WORKER_SYS,
            messages=[{"role": "user", "content":
                f"MAP OF PRIOR DECISIONS:\n{ctx}\n\n[existing ids: {existing}]\n\nTASK: {p}"}])
        t = _txt(r); ans[i] = t
        turn = store.get_turn() + 1; store.set_turn(turn)
        try:
            decs = rudi._json_block(t, "decisions") or []
        except RuntimeError as e:
            print(f"  [json parse error turn {i}: {e}]")
            decs = []
        for d in decs:
            if d.get("id"):
                rev = d.get("revises"); rev = rev[0] if isinstance(rev, list) and rev else (rev if isinstance(rev, str) else None)
                exc = d.get("exception_to"); exc = exc[0] if isinstance(exc, list) and exc else (exc if isinstance(exc, str) else None)
                store.add_node(d["id"], d.get("text", ""), d.get("depends_on", []), rev, exc, turn,
                               hard_rules=d.get("hard_rules", []))
        ev = []
        try:
            ev = fold.run_fold() or []
        except Exception as e:
            print(f"  [fold error: {e}]")
        if ev:
            fold_log.append((i, ev))
        after = store.all_nodes()
        active_after = len([n for n in after.values() if n["status"] != "folded"])
        rows.append((i, r.usage.input_tokens, r.usage.output_tokens, active_after, len(after)))
        trunc = " <TRUNCATED?>" if r.usage.output_tokens >= MAXTOK else ""
        print(f"  turn {i:>2}: in={r.usage.input_tokens:>6} out={r.usage.output_tokens:>5} "
              f"active={active_after:>3} total={len(after):>3}{trunc}"
              + (f"  FOLDED:{[e['stub'] for e in ev]}" if ev else ""))
    return rows, ans, fold_log


rows, ans, fold_log = run()

print("\n" + "=" * 72)
print("TOKEN CURVE  (input per turn; active = non-folded nodes injected)")
print("=" * 72)
for i, ti, to, act, tot in rows:
    bar = "#" * (ti // 150)
    print(f"  t{i:>2}: in={ti:>6} active={act:>3}  {bar}")
print("-" * 72)
print(f"  input total: {sum(r[1] for r in rows)}   peak active nodes: {max(r[3] for r in rows)}   final total nodes: {rows[-1][4]}")

print("\n" + "=" * 72)
print("FOLD EVENTS")
print("=" * 72)
if not fold_log:
    print("  (none — fold never fired)")
for turn_i, evs in fold_log:
    for e in evs:
        print(f"  turn {turn_i}: folded {e['folded_ids']} -> {e['stub']}")

final = store.all_nodes()
print("\n" + "=" * 72)
print("SURVIVING STUBS  (what fold preserved from the abandoned branches)")
print("=" * 72)
for nid, n in sorted(final.items(), key=lambda x: int(x[0][1:])):
    if n["status"] == "stub":
        kids = [c for c in n["depends_on"] if c in final and final[c]["status"] == "folded"]
        print(f"\n  {nid} [stub] (folded children: {kids})\n  {n['text']}")

print("\n" + "=" * 72)
print("GROUND TRUTH — turn 1 (auth) & turn 2 (db)")
print("=" * 72)
print(f"\n--- TURN 1 ---\n{ans[1]}")
print(f"\n--- TURN 2 ---\n{ans[2]}")

print("\n" + "=" * 72)
print("CALLBACK ANSWERS — full text for Claude to grade")
print("=" * 72)
for i, (p, rub) in enumerate(SESSION, 1):
    if not rub:
        continue
    print(f"\n########## TURN {i}: {p}")
    print(f"RUBRIC: {rub}")
    print(f"---ANSWER---\n{ans[i]}")

cost = sum(r[1] for r in rows) / 1e6 * PRICE[0] + sum(r[2] for r in rows) / 1e6 * PRICE[1]
print("\n" + "=" * 72)
print(f"COST this run (worker only): ${cost:.4f}")
