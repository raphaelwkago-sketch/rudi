"""
fold.py — compresses reachability-dead branches into Stubs.
"""

import sys, re
from anthropic import Anthropic
import store

client = Anthropic()
WORKER = "claude-sonnet-4-6"

FOLD_SYS = """You are Rudi's garbage collector.
Your job is to compress a cluster of settled, dead decisions into ONE high-level stub.
Preserve:
1. The final result/outcome.
2. The core reasoning (the WHY).
3. Any explicitly rejected alternatives.
4. EVERY hard rule (never, don't, avoid, must not, forbidden).
5. ALL TECHNICAL CONSTRAINTS: Exact cookie keys, specific ORM/DB stacks, file paths, and exact variable names. Do NOT summarize these away.

Output ONLY the text of the stub. Do not output anything else."""

def run_fold():
    clusters = store.get_dead_clusters()
    if not clusters:
        print("[fold] No reachability-dead clusters found.")
        return []

    events = []
    for cluster in clusters:
        cluster_ids = [n["id"] for n in cluster]
        cluster_texts = [n["text"] for n in cluster]
        cluster_hard_rules = []
        for n in cluster:
            if n.get("hard_rules"):
                cluster_hard_rules.extend(n["hard_rules"])

        if len(cluster_ids) < 3:
            continue

        if store.has_fold_failed(cluster_ids):
            print(f"[fold] Skipping cluster (previously failed size guard): {cluster_ids}")
            continue

        print(f"[fold] Found dead cluster: {cluster_ids}")

        ctx = "\n".join(f"{n['id']}: {n['text']}" for n in cluster)
        resp = client.messages.create(
            model=WORKER, max_tokens=600, system=FOLD_SYS,
            messages=[{"role": "user", "content": f"CLUSTER TO FOLD:\n{ctx}"}]
        )
        stub_text = resp.content[0].text.strip()

        if cluster_hard_rules:
            print(f"[fold] Passing {len(cluster_hard_rules)} hard rule(s) directly to stub.")

        orig_len = sum(len(t) for t in cluster_texts)
        if len(stub_text) >= orig_len * 0.9:
            print(f"[fold] Size guard triggered. Stub ({len(stub_text)} chars) not significantly smaller than original ({orig_len} chars). Skipping.")
            store.mark_fold_failed(cluster_ids)
            continue

        sid = store.fold_nodes(cluster_ids, stub_text, hard_rules=cluster_hard_rules)
        print(f"[fold] Successfully folded {len(cluster_ids)} nodes into {sid}.")
        events.append({"stub": sid, "folded_ids": cluster_ids})

    return events
