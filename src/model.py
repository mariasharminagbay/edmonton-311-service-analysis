"""
model.py - Predict which 311 referrals will be late, then score open requests.

Reads marts.model_training (built by sql/03_marts.sql) and writes:
    models/late_model.joblib      the chosen model (ignored by Git)
    models/metrics.json           test results for both training periods
    marts.open_request_risk       a risk score for every open referral
                                  that is not yet past its target

Design decisions (see the guide's "Decisions to revisit"):
    * Split by time: train on earlier years, test on 2026. A random split
      would mix years and hide the 2025-2026 slowdown.
    * Two training periods are compared: 2023-2024 (stable years) and
      2023-2025 (includes the year the slowdown began).
    * Only "mature" requests are used: created longer ago than their
      category's target, so every one has had the full chance to be late.
    * is_open and days_before_data_end are never features: every open row
      is late by construction, so they would give the answer away.
    * created_year is not a feature either: the test year is never seen
      in training, so the model must not lean on the year.

Usage (from the project folder):
    python src/model.py
"""
import json
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import PROJECT_ROOT, get_engine  # noqa: E402

MODELS_DIR = PROJECT_ROOT / "models"

CATEGORICAL = ["service_category", "service_description", "service_area",
               "interaction_channel", "ward", "neighbourhood"]
NUMERIC = ["created_month_num", "created_weekday", "created_hour"]
FEATURES = CATEGORICAL + NUMERIC
MAX_LEVELS = 250      # the model handles at most 255 values per text column
TOP_SHARE = 0.20      # "if crews checked the riskiest 20% first..."

TRAINING_PERIODS = {"2023-2024": 2024, "2023-2025": 2025}


# ---------- data ----------

def load_mature(engine):
    sql = """
        select *
        from marts.model_training
        where days_before_data_end > target_days
    """
    return pd.read_sql(sql, engine)


def fit_levels(train):
    """The most common values of each text column; the rest become 'Other'."""
    return {col: train[col].value_counts().index[:MAX_LEVELS].tolist()
            for col in CATEGORICAL}


def prepare(df, levels):
    """Feature table with fixed categories, so training and scoring line up."""
    X = df[FEATURES].copy()
    for col in CATEGORICAL:
        cats = levels[col] + ["Other"]
        X[col] = (X[col].where(X[col].isin(levels[col]), "Other")
                        .astype(pd.CategoricalDtype(categories=cats)))
    for col in NUMERIC:
        X[col] = X[col].astype("int16")
    return X


# ---------- model ----------

def train(X, y):
    model = HistGradientBoostingClassifier(
        categorical_features="from_dtype",
        max_iter=300,
        learning_rate=0.1,
        max_leaf_nodes=63,
        early_stopping=True,          # stops when a held-back 10% stops improving
        validation_fraction=0.1,
        random_state=42,
    )
    return model.fit(X, y)


def evaluate(model, X, y):
    """Scores on the 2026 test set, each compared with what guessing gives."""
    p = model.predict_proba(X)[:, 1]
    top = np.argsort(-p)[: int(len(p) * TOP_SHARE)]
    late = y.to_numpy()
    return {
        "test_rows": int(len(y)),
        "late_rate": round(float(late.mean()), 3),               # guessing scores this precision
        "roc_auc": round(float(roc_auc_score(late, p)), 3),     # guessing = 0.5
        "avg_precision": round(float(average_precision_score(late, p)), 3),
        "precision_top20": round(float(late[top].mean()), 3),   # share late among the riskiest 20%
        "recall_top20": round(float(late[top].sum() / late.sum()), 3),  # guessing = 0.20
    }


def importance(model, X, y, n=50_000):
    """Which features matter, measured by how much shuffling each one hurts."""
    sample = X.sample(min(n, len(X)), random_state=42)
    result = permutation_importance(model, sample, y.loc[sample.index],
                                    scoring="roc_auc", n_repeats=3,
                                    random_state=42, n_jobs=-1)
    return (pd.Series(result.importances_mean, index=FEATURES)
              .sort_values(ascending=False).round(4).to_dict())


# ---------- scoring open requests ----------

def score_open(engine, model, levels):
    sql = """
        select row_id, service_category, service_description, service_area,
               interaction_channel,
               coalesce(ward, 'Unknown')          as ward,
               coalesce(neighbourhood, 'Unknown') as neighbourhood,
               extract(month from created_at)::int as created_month_num,
               extract(dow   from created_at)::int as created_weekday,
               extract(hour  from created_at)::int as created_hour,
               created_at, age_days, target_days
        from staging.requests
        where is_open
          and referral_type = 'Referral'
          and not is_instant_category
          and not is_overdue
    """
    df = pd.read_sql(sql, engine)
    df["risk_score"] = model.predict_proba(prepare(df, levels))[:, 1].round(3)
    df["days_until_target"] = (df["target_days"] - df["age_days"]).round(1)
    out = df[["row_id", "created_at", "service_category", "ward",
              "age_days", "target_days", "days_until_target", "risk_score"]]
    out.to_sql("open_request_risk", engine, schema="marts",
               if_exists="replace", index=False, chunksize=5_000, method="multi")
    return len(out)


# ---------- main ----------

def main():
    started = time.time()
    engine = get_engine()
    MODELS_DIR.mkdir(exist_ok=True)

    print("Loading mature requests from marts.model_training...")
    df = load_mature(engine)
    test = df[~df["is_training_period"]]
    print(f"  {len(df):,} rows; 2026 test set {len(test):,} rows, "
          f"{test['is_late'].mean():.1%} late")

    results, fitted = {}, {}
    for name, last_year in TRAINING_PERIODS.items():
        train_df = df[df["is_training_period"] & (df["created_year"] <= last_year)]
        levels = fit_levels(train_df)
        print(f"\nTraining on {name}: {len(train_df):,} rows, "
              f"{train_df['is_late'].mean():.1%} late")
        model = train(prepare(train_df, levels), train_df["is_late"])
        results[name] = {"train_rows": int(len(train_df)),
                         "train_late_rate": round(float(train_df["is_late"].mean()), 3),
                         **evaluate(model, prepare(test, levels), test["is_late"])}
        fitted[name] = (model, levels)
        for k, v in results[name].items():
            print(f"  {k:>16}: {v}")

    best = max(results, key=lambda k: results[k]["avg_precision"])
    model, levels = fitted[best]
    print(f"\nChosen: trained on {best} (higher average precision on 2026)")

    print("Measuring feature importance (a minute or two)...")
    imp = importance(model, prepare(test, levels), test["is_late"])
    for feature, value in imp.items():
        print(f"  {feature:>20}: {value}")

    joblib.dump({"model": model, "levels": levels, "features": FEATURES,
                 "trained_on": best}, MODELS_DIR / "late_model.joblib")
    with open(MODELS_DIR / "metrics.json", "w") as f:
        json.dump({"chosen": best, "results": results,
                   "permutation_importance_roc_auc": imp}, f, indent=2)

    n = score_open(engine, model, levels)
    print(f"\nScored {n:,} open requests into marts.open_request_risk")
    print(f"Saved models/late_model.joblib and models/metrics.json "
          f"({(time.time() - started) / 60:.1f} min)")


if __name__ == "__main__":
    main()
