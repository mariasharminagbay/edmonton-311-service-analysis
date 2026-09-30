"""
extract.py - Pull Edmonton 311 requests from the City's open data portal
and load them, unchanged, into PostgreSQL as raw.requests.

Dataset: 311 Requests (q7ua-agfg), https://data.edmonton.ca/d/q7ua-agfg

The full dataset is several million rows, so this script streams it:
each page of 50,000 rows is written to PostgreSQL and the raw CSV as soon
as it arrives, instead of holding everything in memory.

Usage (run from the project folder):
    python src/extract.py --test                 # first 1,000 rows only
    python src/extract.py                        # full dataset
    python src/extract.py --since 2024-01-01     # only requests created on/after a date
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"          # ignored by .gitignore
RAW_CSV = RAW_DIR / "requests_raw.csv"

DATASET_ID = "q7ua-agfg"
DATA_URL = f"https://data.edmonton.ca/resource/{DATASET_ID}.json"
META_URL = f"https://data.edmonton.ca/api/views/{DATASET_ID}.json"
PAGE_SIZE = 50_000
TEST_ROWS = 1_000

load_dotenv(PROJECT_ROOT / ".env")

HEADERS = {}
if os.getenv("SOCRATA_APP_TOKEN"):               # optional; reduces throttling
    HEADERS["X-App-Token"] = os.getenv("SOCRATA_APP_TOKEN")


# ---------- database ----------

def get_engine():
    """Build a SQLAlchemy engine from the settings in .env."""
    keys = ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME")
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing from .env: {', '.join(missing)}")
    url = URL.create(
        "postgresql+psycopg2",
        username=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
    )
    return create_engine(url)


def copy_insert(table, conn, keys, data_iter):
    """Fast insert for pandas.to_sql using PostgreSQL COPY."""
    buf = io.StringIO()
    csv.writer(buf).writerows(data_iter)
    buf.seek(0)
    cols = ", ".join(f'"{k}"' for k in keys)
    name = f'"{table.schema}"."{table.name}"' if table.schema else f'"{table.name}"'
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {name} ({cols}) FROM STDIN WITH CSV", buf)


# ---------- API ----------

def get(url, params=None):
    """GET with three attempts, returning parsed JSON."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=180)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as err:
            if attempt == 2:
                raise
            print(f"  Request failed ({err}); retrying in 15 seconds")
            time.sleep(15)


def get_columns():
    """Column names from the dataset's metadata, so every page has the same shape."""
    meta = get(META_URL)
    return [c["fieldName"] for c in meta["columns"]
            if not c["fieldName"].startswith(":")]


def count_rows(where):
    params = {"$select": "count(*)"}
    if where:
        params["$where"] = where
    result = get(DATA_URL, params)
    return int(list(result[0].values())[0])


def pages(where, max_rows):
    """Yield the dataset one page at a time."""
    offset = 0
    while True:
        limit = PAGE_SIZE if max_rows is None else min(PAGE_SIZE, max_rows - offset)
        if limit <= 0:
            return
        params = {"$limit": limit, "$offset": offset, "$order": ":id"}
        if where:
            params["$where"] = where
        rows = get(DATA_URL, params)
        if not rows:
            return
        yield rows
        offset += len(rows)
        if len(rows) < limit:
            return


def tidy(rows, columns):
    """Fixed column order; nested location objects stored as JSON text."""
    df = pd.DataFrame(rows).reindex(columns=columns)
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, (dict, list))).any():
            df[col] = df[col].map(
                lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
    return df


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Extract Edmonton 311 data")
    parser.add_argument("--test", action="store_true",
                        help=f"only download the first {TEST_ROWS:,} rows")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only requests created on or after this date")
    args = parser.parse_args()

    where = f"date_created >= '{args.since}T00:00:00'" if args.since else None

    engine = get_engine()
    with engine.connect() as conn:                 # fail fast on bad credentials
        conn.execute(text("select 1"))
    print("Connected to PostgreSQL.")

    columns = get_columns()
    expected = TEST_ROWS if args.test else count_rows(where)
    print(f"{len(columns)} columns; {expected:,} rows to download.")

    with engine.begin() as conn:
        conn.execute(text("create schema if not exists raw"))
        conn.execute(text("drop table if exists raw.requests"))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    done = 0

    for i, rows in enumerate(pages(where, TEST_ROWS if args.test else None)):
        df = tidy(rows, columns)
        df.to_csv(RAW_CSV, index=False, mode="w" if i == 0 else "a", header=(i == 0))
        df.to_sql("requests", engine, schema="raw", if_exists="append",
                  index=False, method=copy_insert)
        done += len(df)
        mins = (time.time() - started) / 60
        print(f"  {done:,} / {expected:,} rows loaded ({mins:.1f} min)")

    with engine.connect() as conn:
        loaded = conn.execute(text("select count(*) from raw.requests")).scalar()

    print(f"\nLoaded {loaded:,} rows into raw.requests")
    print(f"Raw copy saved to {RAW_CSV}")
    if loaded != expected:
        sys.exit(f"Row count mismatch: expected {expected:,}, loaded {loaded:,}. "
                 "The dataset may have refreshed mid-download; run it again.")


if __name__ == "__main__":
    main()
