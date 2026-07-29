from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np


def neutralize_factor(
    rows: list[dict],
    *,
    factor_key: str,
    industry_key: str = "industry_l1",
    market_cap_key: str = "market_cap",
) -> dict:
    """Remove industry and log-market-cap exposures with deterministic OLS."""
    valid = []
    excluded = []
    for row in rows:
        try:
            value = float(row[factor_key])
            market_cap = float(row[market_cap_key])
        except (KeyError, TypeError, ValueError):
            excluded.append({**row, "neutralization_reason": "missing_numeric_input"})
            continue
        industry = str(row.get(industry_key) or "unknown")
        if not math.isfinite(value) or not math.isfinite(market_cap) or market_cap <= 0:
            excluded.append({**row, "neutralization_reason": "invalid_numeric_input"})
            continue
        valid.append(({**row}, value, math.log(market_cap), industry))
    industries = sorted({item[3] for item in valid})
    if len(valid) < max(3, len(industries) + 1):
        return {"items": [], "excluded": [*excluded, *[{**item[0], "neutralization_reason": "insufficient_cross_section"} for item in valid]], "method": "industry-dummy + log-market-cap OLS", "industry_count": len(industries)}
    baseline = industries[0]
    x = []
    y = []
    for _, value, log_cap, industry in valid:
        x.append([1.0, log_cap, *[1.0 if industry == name else 0.0 for name in industries[1:]]])
        y.append(value)
    matrix = np.asarray(x, dtype=float)
    target = np.asarray(y, dtype=float)
    coefficients, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    residuals = target - matrix @ coefficients
    std = float(residuals.std(ddof=0))
    normalized = residuals / std if std > 1e-12 else np.zeros_like(residuals)
    items = [{**row, "neutralized_value": float(residual), "neutralized_zscore": float(zscore)} for (row, *_), residual, zscore in zip(valid, residuals, normalized)]
    return {"items": items, "excluded": excluded, "method": "industry-dummy + log-market-cap OLS", "industry_count": len(industries), "baseline_industry": baseline, "coefficients": coefficients.tolist()}


def universe_membership(db_path: Path, *, as_of: str, universe: str | None = None) -> dict:
    if not db_path.exists():
        return {"as_of": as_of, "universe": universe, "items": [], "source": db_path.name}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_universe_membership'").fetchone()
        if not exists:
            return {"as_of": as_of, "universe": universe, "items": [], "source": db_path.name}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(price_universe_membership)")}
        if "effective_from" in columns:
            conditions = ["effective_from<=?", "(effective_to IS NULL OR effective_to>=?)"]
            params: list[object] = [as_of, as_of]
            universe_column = "universe"
        else:
            conditions = ["universe_as_of<=?"]
            params = [as_of]
            universe_column = "universe_name"
        if universe:
            conditions.append(f"{universe_column}=?")
            params.append(universe)
        rows = conn.execute(f"SELECT * FROM price_universe_membership WHERE {' AND '.join(conditions)} ORDER BY {universe_column}, security_code", params).fetchall()
    return {"as_of": as_of, "universe": universe, "items": [dict(row) for row in rows], "source": db_path.name, "history_contract": "effective_range" if "effective_from" in columns else "snapshot_as_of"}
