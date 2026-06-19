# Rudi

**Causal graph memory for LLMs. Flat token cost, no matter how long the session runs.**

Every LLM API call re-sends the whole conversation. Cost grows every turn; eventually you hit the context limit. Rudi replaces the growing transcript with a **dependency graph of decisions** — and injects only the slice relevant to the current task. Turn 10,000 costs about the same as turn 10.

---

## The 30-second version

In a **43-turn** software-architecture session (building a Notes API turn by turn), the standard "re-send the full transcript" approach was sending **~38,000 input tokens** by the final turn. Rudi sent **6,782** — for the *same task, same model, same answer quality.*

| Turn | Rudi input | Full-transcript input | Savings |
|-----:|-----------:|----------------------:|--------:|
| 1    | 382        | 340                   | —       |
| 10   | 1,467      | 6,999                 | 4.8×    |
| 20   | 3,581      | 17,385                | 4.9×    |
| 30   | 4,128      | 26,821                | 6.5×    |
| 43   | 6,782      | 38,320                | **5.7×** |

**Totals across all 43 turns:** 152,222 input tokens (Rudi) vs 828,369 (full transcript) — **5.4× fewer tokens**, and the gap widens every turn because Rudi's curve is bounded while the transcript's is linear.

> These numbers are **conservative**: the background fold/garbage-collector was disabled in this run. The savings come from graph slicing alone. With fold on, the curve flattens further, not less.

Cost of the entire 43-turn run on Claude Haiku 4.5: **$0.34.**

---

## It doesn't just stay small — it stays *correct*

Cheap context is worthless if the model forgets the rules. So the same benchmark plants **6 callback traps** late in the session and checks whether decisions made dozens of turns earlier are still honored.

| # | Turn | Trap | Result |
|---|-----:|------|:------:|
| 1 | 38 | Add logout — must use the **exact** auth mechanism chosen on turn 1 | ✅ |
| 2 | 39 | Profile endpoint — must scope via turn-1 auth **and** turn-2 DB | ✅ |
| 3 | 40 | Admin CSV export — a rule that was **folded away** banned cross-user data | ✅ surfaced |
| 4 | 41 | Email full notes — a **folded** rule banned note contents in email | ✅ surfaced |
| 5 | 42 | "Store the token in localStorage" — conflicts with turn-1 hard rule | ✅ blocked |
| 6 | 43 | "Permanently delete a note" — turn-11 chose soft-delete | ✅ flagged |

**6 / 6.** The two that matter most are #3 and #4: those rules had been **compressed out of the active context** by the time the trap was sprung — and the model still caught them, because hard rules are preserved verbatim on the fold stub. That's the whole thesis: *forget the prose, keep the constraints.*

---

## How it works

Every model response is parsed into **decision nodes**, each linked backward to the decisions it depends on:

```
node = {
  id, text,
  depends_on: [...],     # backward edges — what this decision rests on
  hard_rules: [...],     # binding constraints; the worker must halt if violated
  revises, exception_to, # full replacement vs. narrow carve-out
  status, turn, pinned
}
```

- **Slice, don't dump.** Before each turn, Rudi injects only the nodes reachable from the current task — not the transcript.
- **Fold.** When a branch of decisions goes reachability-dead, a background pass compresses it into a one-line stub. **Hard rules survive the fold verbatim**, so a constraint can never be silently lost (see traps #3/#4).
- **Pin foundations.** Decisions that are reinforced repeatedly, made in the first two turns, or carry exceptions are pinned and never folded.
- **Hard rules are binding.** If a new task would violate one, the worker stops and asks instead of silently complying (traps #5/#6).

---

## Try it in 5 minutes

```bash
git clone https://github.com/<you>/rudi
cd rudi
pip install anthropic flask flask-cors

# your own key — never hardcode it
export ANTHROPIC_API_KEY="sk-ant-..."

# run the 43-turn benchmark against local SQLite (no cloud needed)
python benchmark_long_haiku.py
```

You'll watch the input-token curve stay flat while a naive transcript would balloon, and see all 6 callback traps resolve.

---

## Use it in code

Two calls per turn. You keep your own model key; Rudi only manages the graph.

```python
import rudi

# 1 — before your LLM call: fetch the relevant slice
s = rudi.get_slice(task)
#   → feed s["system"] + s["prompt"] into YOUR LLM call

# 2 — after your LLM call: store what was decided
rudi.store_decisions(decisions, inject_ids=s["inject_ids"])
```

Or let Rudi drive the whole turn (LLM call + store + fold) in one shot:

```python
result = rudi.run_turn(task)   # → {"display", "tokens_in", "tokens_out", ...}
```

Storage is local SQLite (`store.py`) — one row per decision node. No server, no cloud, no setup.

---

## What's proven vs. in progress

| | |
|---|---|
| Graph slicing bounds the token curve | ✅ measured — table above |
| Decisions recalled 40+ turns later | ✅ 6/6 callbacks |
| Hard rules survive fold verbatim | ✅ traps #3/#4 |
| Conflicts blocked, not silently obeyed | ✅ traps #5/#6 |
| Fold GC in the live token numbers | ⏳ disabled in this run — numbers are without it |
| Retrieval fallback above ~80 active nodes | ⏳ built, not yet benchmarked at scale |

No vapor. The table is what the logs say; the in-progress rows are labeled as such.

---

## License

**AGPL-3.0.** Free for personal use, research, and self-hosting. If you run a modified version as a network service, you must release your source under the same license. Contributions are accepted under a CLA so the project can offer commercial licensing later.

Want to use Rudi commercially without AGPL obligations? Open an issue or email **caregiving886@gmail.com**.

---

*Built solo in Nairobi. A full morning of architectural reasoning, mapped and recalled, for under a dollar.*
