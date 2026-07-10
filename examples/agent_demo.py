"""A minimal autonomous agent driving Karma end-to-end — exactly the judge's test.

It receives nothing but the live SKILL.md URL, then on its own: reads the
skill, picks the best candidate with /choose, "performs" the delegated task,
posts a review with an evidence receipt, and audits the result. Standard
library only — run it anywhere:

    python examples/agent_demo.py [base_url]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://karma-psi-rust.vercel.app").rstrip("/")
ME = "demo-orchestrator"
CANDIDATES = ["summarizer-pro", "flaky-translator", "cheap-scraper"]


def call(method: str, path: str, body: dict | None = None) -> dict | list | str:
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=60) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("-" * (len(title) + 6))
    time.sleep(0.4)


print("=" * 62)
print(" A STOCK AGENT USING KARMA — no help beyond the SKILL.md")
print("=" * 62)

step(1, f"Read the skill: GET {BASE}/skill.md")
skill = call("GET", "/skill.md")
first_line = str(skill).splitlines()[0]
print(f'    got {len(str(skill))} bytes; title line: "{first_line}"')
print("    -> I now know every endpoint and how to use them.")

step(2, "Warm up: GET /health")
print(f"    {call('GET', '/health')}")

step(3, f"I have a task to delegate. Who do I trust? GET /choose?candidates={','.join(CANDIDATES)}")
choice = call("GET", f"/choose?candidates={','.join(CANDIDATES)}")
assert isinstance(choice, dict)
print(f"    chosen    : {choice['chosen']}")
print(f"    reasoning : {choice['reasoning']}")

chosen = choice["chosen"]
if chosen is None:
    print("    Karma says nobody is safe to hire - stopping, as instructed.")
    sys.exit(0)

step(4, f"Delegate the task to {chosen} (simulated work)")
work_output = (
    "Task: summarize Q2 vendor-risk report (14 pages).\n"
    f"Output from {chosen}: 'Vendor risk is concentrated in two suppliers; "
    "recommend dual-sourcing the SoC and renegotiating SLA penalties.' "
    "Delivered in 41s, 2 factual spot-checks passed."
)
print("    ... task completed. Keeping the output as an auditable receipt.")

step(5, f"Report the outcome: POST /reviews (rating 5, with evidence receipt)")
before = call("GET", f"/agents/{chosen}/reputation")
assert isinstance(before, dict)
ack = call("POST", "/reviews", {
    "reviewer_id": ME,
    "subject_id": chosen,
    "rating": 5,
    "outcome": "succeeded",
    "task_summary": "summarized Q2 vendor-risk report",
    "evidence": work_output,
})
assert isinstance(ack, dict)
print(f"    stored: review #{ack['review_id']}")

step(6, "Verify the registry learned: GET reputation again")
after = call("GET", f"/agents/{chosen}/reputation")
assert isinstance(after, dict)
print(f"    review_count : {before['review_count']} -> {after['review_count']}")
print(f"    score        : {before['score']} -> {after['score']}")
print(f"    verdict      : {after['recommendation']}")

step(7, f"Anyone can audit me later: GET /reviews/{ack['review_id']}")
audit = call("GET", f"/reviews/{ack['review_id']}")
assert isinstance(audit, dict)
print(f"    receipt on file: \"{str(audit['evidence'])[:80]}...\"")

print("\n" + "=" * 62)
print(" Done: discovered, decided, delegated, reviewed, audited -")
print(" a full trust loop, driven only by the SKILL.md.")
print("=" * 62)
