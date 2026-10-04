"""Model fit/predict for M5. Contract: ADR 0014 §3, §4.

Public API:

    fit_predict(train_df, test_df, feature_cols, settings) -> np.ndarray

    Given a training frame (features + label) and a test frame
    (features only), fit a logistic regression on the training rows
    using exponential time-decay sample weights, then return predicted
    P(y=1) for each test row.

    Pure: does not mutate either input. Deterministic given the same
    inputs and settings.

Feature handling:

    - NaN in features: median-imputed (fit on train, applied to test).
    - All-NaN columns: kept (imputer emits 0.0 after scaling).
    - Constant columns: kept; coefficient will be 0.
    - Non-numeric columns: rejected up front.

Label handling:

    - y is cast to int64. Boolean labels are accepted.
    - If the training window has fewer than 2 classes, the function
      returns a constant prediction equal to the base rate (no fit).
      This can happen in degenerate windows; the runner logs a warning.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from backtest.config import BacktestSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------


def compute_time_decay_weights(
    trade_dates: pd.Series | np.ndarray,
    half_life_days: int,
) -> np.ndarray:
    """Exponential weight: w = exp(-lambda * age), age in trading days.

    ``age`` is measured as the number of distinct trade dates between
    the row and the newest date in the input. The newest row has
    weight 1.0; the row ``half_life_days`` trading days older has
    weight 0.5.
    """
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive: {half_life_days}")

    dates = np.asarray(trade_dates)
    if dates.size == 0:
        return np.zeros(0, dtype=float)

    unique = np.sort(np.unique(dates))
    pos = {d: i for i, d in enumerate(unique)}
    idx = np.array([pos[d] for d in dates], dtype=int)
    age = (unique.size - 1) - idx  # 0 = newest
    lam = np.log(2.0) / float(half_life_days)
    return np.exp(-lam * age)


def build_estimator(settings: BacktestSettings) -> Pipeline:
    """Build the sklearn Pipeline used per fold.

    Pipeline: SimpleImputer(median) -> StandardScaler -> LogisticRegression.
    """
    if settings.model_type != "logistic":
        raise ValueError(
            f"model_type {settings.model_type!r} not supported in v1 (only 'logistic')."
        )
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scaler", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    C=settings.model_C,
                    solver=settings.model_solver,
                    max_iter=settings.model_max_iter,
                    random_state=settings.seed,
                ),
            ),
        ]
    )


# ---------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------


def _validate(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    settings: BacktestSettings,
) -> None:
    required_train = set(feature_cols) | {settings.label_column}
    missing_train = [c for c in required_train if c not in train_df.columns]
    if missing_train:
        raise ValueError(f"train_df missing required columns: {sorted(missing_train)}")
    missing_test = [c for c in feature_cols if c not in test_df.columns]
    if missing_test:
        raise ValueError(f"test_df missing feature columns: {sorted(missing_test)}")


def _to_xy(
    df: pd.DataFrame,
    feature_cols: list[str],
) -> np.ndarray:
    """Extract X as float64 numpy, raising on non-numeric columns."""
    try:
        return df[feature_cols].to_numpy(dtype=np.float64)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Non-numeric value in feature columns: {e}") from e


# ---------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------


def fit_predict(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    settings: BacktestSettings | None = None,
) -> np.ndarray:
    """Fit on train_df, return P(y=1) for each row of test_df."""
    if settings is None:
        settings = BacktestSettings()

    _validate(train_df, test_df, feature_cols, settings)

    if train_df.empty:
        raise ValueError("train_df is empty")
    if test_df.empty:
        return np.zeros(0, dtype=float)

    x_train = _to_xy(train_df, feature_cols)
    x_test = _to_xy(test_df, feature_cols)

    y_train = train_df[settings.label_column].to_numpy()
    y_train = y_train.astype(np.int64, copy=False)

    n_classes = len(np.unique(y_train))
    if n_classes < 2:
        base_rate = float(np.clip(y_train.mean(), 0.0, 1.0))
        logger.warning(
            "fit_predict: train window has %d class(es); "
            "returning constant p=%.4f for %d test rows.",
            n_classes,
            base_rate,
            len(test_df),
        )
        return np.full(len(test_df), base_rate, dtype=float)

    # Sample weights
    if settings.use_time_decay:
        w = compute_time_decay_weights(
            train_df["trade_date"],
            settings.time_decay_half_life_days,
        )
    else:
        w = np.ones(len(train_df), dtype=float)

    estimator = build_estimator(settings)
    estimator.fit(x_train, y_train, clf__sample_weight=w)

    probs = estimator.predict_proba(x_test)[:, 1]
    # Numerical safety: clip to [0, 1] (predict_proba already does,
    # but guard against solver edge cases).
    return np.clip(probs, 0.0, 1.0).astype(float)


__all__ = [
    "build_estimator",
    "compute_time_decay_weights",
    "fit_predict",
]
