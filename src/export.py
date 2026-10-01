"""
export.py - Write the dashboard's data files to data/exports/.

Tableau Public cannot connect to PostgreSQL, so the dashboard reads CSV
files. Every file comes from a marts table: small summaries, not the
3.4 million request rows, so the dashboard stays fast.

Files (data/exports/ is ignored by Git; Tableau Public keeps its own copy):
    monthly_volume.csv            R1, R2  volume, channel, first-contact resolution
    monthly_performance.csv       trend   late rate by month and category
    request_type_performance.csv  R3      turnaround and late rate by category
    backlog_age.csv               R4      open requests by age
    ward_summary.csv              R5      ward comparison
    weekday_performance.csv               late rate by weekday
    open_request_risk.csv                 model risk score for open requests

Usage (from the project folder):
    python src/export.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PROJECT_ROOT, get_engine  # noqa: E402

EXPORT_DIR = PROJECT_ROOT / "data" / "exports"

EXPORTS = {
    "monthly_volume": """
        select * from marts.monthly_volume
        order by created_month, service_category, interaction_channel""",
    "monthly_performance": """
        select *,
               round(mature_referrals::numeric / nullif(referrals, 0), 3) as coverage
        from marts.monthly_performance
        order by created_month, service_category""",
    "request_type_performance": """
        select * from marts.request_type_performance
        order by requests desc""",
    "backlog_age": """
        select * from marts.backlog_age
        order by service_category, age_bucket""",
    "ward_summary": """
        select * from marts.ward_summary
        order by ward""",
    "weekday_performance": """
        select * from marts.weekday_performance
        order by is_training_period desc, weekday_num""",
    "open_request_risk": """
        select * from marts.open_request_risk
        order by risk_score desc""",
}


def run():
    engine = get_engine()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for name, sql in EXPORTS.items():
        df = pd.read_sql(sql, engine)
        path = EXPORT_DIR / f"{name}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        print(f"  {name}.csv: {len(df):,} rows, {path.stat().st_size / 1024:,.0f} KB")


if __name__ == "__main__":
    run()
