"""
checks.py - Automatic data checks, run after staging and marts are built.

Each check is a SQL query returning one number, and the value it must have.
A failed "stop" check ends the pipeline before the model trains on bad
data; a failed "warn" check is reported but lets the run continue.

These are the checks first run by hand on Sep 29-30, 2026 (see the guide's
SQL run log). Expected values are rules, not today's totals, so they keep
working as new data arrives.

Usage (from the project folder):
    python src/checks.py
"""
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import get_engine  # noqa: E402

# (name, severity, sql returning one number, rule the number must meet)
CHECKS = [
    ("Raw table is not empty", "stop",
     "select count(*) from raw.requests",
     lambda v: v > 0),

    ("row_id is unique in raw (duplicate rows)", "stop",
     "select count(*) - count(distinct row_id) from raw.requests",
     lambda v: v == 0),

    ("count column is always 1 (one row = one request)", "stop",
     "select count(*) from raw.requests where count::text <> '1'",
     lambda v: v == 0),

    ("Staging keeps every raw row (raw minus staging)", "stop",
     "select (select count(*) from raw.requests) - (select count(*) from staging.requests)",
     lambda v: v == 0),

    ("Only Closed and Open statuses", "stop",
     "select count(*) from staging.requests where request_status not in ('Closed', 'Open')",
     lambda v: v == 0),

    ("No request closed before it was created", "stop",
     "select count(*) from staging.requests where dq_negative_duration",
     lambda v: v == 0),

    ("Marts count every request once (staging minus monthly_volume)", "stop",
     "select (select count(*) from staging.requests) - (select sum(requests) from marts.monthly_volume)",
     lambda v: v == 0),

    ("Large categories sit near 25% late in 2023-2025 (categories more than 5 points off)", "stop",
     """select count(*) from (
            select service_category
            from staging.requests
            where is_late is not null and is_training_period
            group by service_category
            having count(*) >= 500
               and abs(100.0 * avg(is_late::int) - 25) > 5
        ) off_target""",
     lambda v: v == 0),

    ("Every open row in the model data is labelled late", "stop",
     "select count(*) from marts.model_training where is_open and is_late = 0",
     lambda v: v == 0),

    ("Missing referral type stays under 2% of requests", "warn",
     "select 100.0 * avg(dq_missing_referral_type::int) from staging.requests",
     lambda v: v < 2),

    ("Data is fresh (days since the newest request)", "warn",
     "select extract(epoch from (now() - max(created_at))) / 86400 from staging.requests",
     lambda v: v <= 7),
]


def run():
    """Run every check. Returns the number of failed 'stop' checks."""
    engine = get_engine()
    stops = warnings = 0
    with engine.connect() as conn:
        for name, severity, sql, rule in CHECKS:
            value = conn.execute(text(sql)).scalar()
            value = float(value) if value is not None else None
            passed = value is not None and rule(value)
            shown = "none" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")
            label = "PASS" if passed else ("FAIL" if severity == "stop" else "WARN")
            print(f"  [{label}] {name}: {shown}")
            if not passed:
                if severity == "stop":
                    stops += 1
                else:
                    warnings += 1
    print(f"Checks: {len(CHECKS) - stops - warnings} passed, "
          f"{warnings} warnings, {stops} failed")
    return stops


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
