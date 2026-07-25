"""
Isolated probe for compose_smart_reply() -- same reasoning as
_daily_limit_probe.py: it reads KB entries and stock families from the
DB, so it needs a freshly-seeded, isolated APEX_DB_PATH rather than
whatever app.db.DB_PATH happened to resolve to first in the shared
pytest process.

Prints a JSON list of {input, subject, body, confidence, matched_summary}
for a fixed set of probe reply texts, covering the exact bug this was
built to fix: a reply that already states the grade must not be asked
for the grade again.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db  # noqa: E402
from app.services.kb_qa import compose_smart_reply  # noqa: E402

PROBE_INPUTS = [
    ("grade_only", "Grade A please, do you have steel gratings?"),
    ("nothing_given", "Do you have flat bars in stock?"),
    ("everything_given", "We need Grade A, 50x5mm, 20 tons of flat bars"),
    ("grade_and_qty_only", "Grade A, 20 tons please"),
]


def main():
    init_db()  # seeds the KB entries, including "Do you supply steel gratings?"
    results = []
    for label, text in PROBE_INPUTS:
        draft = compose_smart_reply("Jane", "Acme", text)
        results.append({"label": label, "input": text, **draft})
    print(json.dumps(results))


if __name__ == "__main__":
    main()
