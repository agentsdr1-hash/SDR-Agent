"""
Standalone probe (see _daily_limit_probe.py's docstring for why this
pattern exists) -- confirms the demo customers seeded by init_db() do NOT
come back after a reset clears the customers table, even across a second
init_db() call (i.e. what happens on the next deploy/restart after a
reset). Prints "PASS" or "FAIL: <reason>" to stdout.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, get_conn  # noqa: E402
from app.services.administration import reset_all_data  # noqa: E402


def main():
    init_db()  # first "startup" -- seeds the 2 demo customers
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    if count != 2:
        print(f"FAIL: expected 2 seeded demo customers after first init_db(), got {count}")
        return

    reset_all_data()  # simulates the Admin "Reset all data" action
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    if count != 0:
        print(f"FAIL: expected 0 customers immediately after reset, got {count}")
        return

    init_db()  # second "startup" -- e.g. the next deploy/restart after the reset
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    if count != 0:
        print(f"FAIL: demo customers were resurrected by the second init_db() -- got {count}")
        return

    print("PASS")


if __name__ == "__main__":
    main()
