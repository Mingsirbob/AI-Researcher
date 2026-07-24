import hashlib
import json
import math
import statistics

import pytest

from app.primitives import (
    annualized_volatility,
    canonical_hash,
    code_to_qlib_instrument,
    code_to_table,
    maximum_drawdown,
    normalize_code,
    period_return,
    qlib_instrument_to_code,
    table_to_code,
)


def test_canonical_hash_preserves_compact_and_legacy_json_contracts():
    payload = {"中文": [2, 1], "alpha": True}
    compact = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    legacy = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert canonical_hash(payload) == hashlib.sha256(compact.encode("utf-8")).hexdigest()
    assert canonical_hash(payload, compact=False) == hashlib.sha256(
        legacy.encode("utf-8")
    ).hexdigest()


def test_market_metrics_make_window_requirements_explicit():
    prices = [100.0, 101.0, 99.0, 102.0]
    returns = [prices[index] / prices[index - 1] - 1 for index in range(1, 4)]

    assert period_return(prices, 3) == pytest.approx(0.02)
    assert annualized_volatility(
        prices, periods=60, require_full_window=False
    ) == pytest.approx(statistics.stdev(returns) * math.sqrt(252))
    assert annualized_volatility(prices, periods=60) is None
    assert maximum_drawdown(prices, periods=4) == pytest.approx(99.0 / 101.0 - 1)
    assert maximum_drawdown(prices, periods=250) is None
    assert maximum_drawdown(
        prices, periods=250, require_full_window=False
    ) == pytest.approx(99.0 / 101.0 - 1)


def test_security_code_conversions_have_one_round_trip_contract():
    assert normalize_code("stock.600000_sh") == "600000.SH"
    assert code_to_table("600000") == "stock_600000_SH"
    assert table_to_code("stock_600000_SH") == "600000.SH"
    assert code_to_qlib_instrument("600000.SH") == "SH600000"
    assert qlib_instrument_to_code("sh600000") == "600000.SH"
