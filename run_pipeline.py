"""
run_pipeline.py - Run the whole Edmonton 311 pipeline with one command.

Steps, in order:
    1. Extract   download the data and load raw.requests   (about 36 min)
    2. Transform run sql/02_staging.sql and sql/03_marts.sql (about 7 min)
    3. Checks    stop here if the data looks wrong
    4. Model     train, test and score open requests       (about 4 min)

Usage (from the project folder):
    python run_pipeline.py                  # everything
    python run_pipeline.py --skip-download  # rebuild from data already loaded

Each run adds a summary to pipeline.log (ignored by Git).
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import checks      # noqa: E402
import extract     # noqa: E402
import model       # noqa: E402
import transform   # noqa: E402

LOG = ROOT / "pipeline.log"


def log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def step(name, func):
    log(f"START {name}")
    started = time.time()
    result = func()
    log(f"END   {name} ({(time.time() - started) / 60:.1f} min)")
    return result


def main():
    parser = argparse.ArgumentParser(description="Run the Edmonton 311 pipeline")
    parser.add_argument("--skip-download", action="store_true",
                        help="skip the download and rebuild from raw.requests as it is")
    args = parser.parse_args()

    started = time.time()
    log("=" * 50)
    log("Pipeline run" + (" (download skipped)" if args.skip_download else ""))

    if not args.skip_download:
        step("extract", extract.run)
    step("transform", transform.run)

    failed = step("checks", checks.run)
    if failed:
        log(f"STOPPED: {failed} check(s) failed, so the model was not retrained")
        sys.exit(1)

    step("model", model.main)
    log(f"Pipeline finished in {(time.time() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
