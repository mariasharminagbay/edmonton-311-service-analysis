"""
transform.py - Run the SQL files that build staging and marts, in order.

Each SQL file manages its own transaction (begin ... commit), so it either
builds completely or changes nothing. The connection runs in autocommit
mode so those statements are in charge, not Python.

Usage (from the project folder):
    python src/transform.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PROJECT_ROOT, get_engine  # noqa: E402

SQL_FILES = ["sql/02_staging.sql", "sql/03_marts.sql"]


def run_sql_file(engine, path):
    sql = (PROJECT_ROOT / path).read_text(encoding="utf-8")
    raw = engine.raw_connection()
    dbapi = raw.driver_connection
    try:
        dbapi.autocommit = True
        with dbapi.cursor() as cur:
            cur.execute(sql)              # no parameters, so '%' in comments is safe
    finally:
        dbapi.autocommit = False          # hand the connection back in its normal state
        raw.close()


def run():
    engine = get_engine()
    for path in SQL_FILES:
        started = time.time()
        print(f"Running {path}...")
        run_sql_file(engine, path)
        print(f"  done in {(time.time() - started) / 60:.1f} min")


if __name__ == "__main__":
    run()
