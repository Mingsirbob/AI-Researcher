import numpy as np
import pandas as pd
import app.current_shadow as current_shadow_module

from app.current_shadow import (
    align_forward_adjusted_fields,
    code_to_qlib_instrument,
    current_signal_rows,
    rolling_oos_evaluation,
    write_qlib_provider,
)


def test_rolling_oos_evaluation_uses_time_windows_and_passes_stable_signal():
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2018-01-02", periods=210)
    instruments = [f"SH{600000 + index:06d}" for index in range(30)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=("datetime", "instrument")
    )
    scores = rng.normal(size=len(index))
    labels = scores * 0.08 + rng.normal(scale=0.92, size=len(index))

    result = rolling_oos_evaluation(
        pd.DataFrame({"score": scores}, index=index),
        pd.DataFrame({"LABEL0": labels}, index=index),
        window_size=21,
    )

    assert result["status"] == "passed"
    assert result["metrics"]["window_count"] == 10
    assert result["metrics"]["mean_rank_ic"] > 0.02
    assert all("start_date" in item and "end_date" in item for item in result["windows"])


def test_current_signal_rows_rank_single_cross_section():
    index = pd.MultiIndex.from_tuples(
        [
            ("2026-07-20", "SH600000"),
            ("2026-07-20", "SZ000001"),
        ],
        names=("datetime", "instrument"),
    )
    rows = current_signal_rows(pd.Series([0.1, 0.3], index=index))

    assert code_to_qlib_instrument("600000.SH") == "SH600000"
    assert [(item["security_code"], item["cross_section_rank"]) for item in rows] == [
        ("600000.SH", 2),
        ("000001.SZ", 1),
    ]


def test_write_qlib_provider_creates_calendar_instruments_and_binary_features(tmp_path, monkeypatch):
    dates = pd.bdate_range("2026-03-02", periods=85)
    records = []
    for code, base in (("600000.SH", 10.0), ("000001.SZ", 20.0)):
        for offset, trading_date in enumerate(dates):
            close = base + offset * 0.01
            records.append(
                {
                    "time": trading_date.strftime("%Y-%m-%d"),
                    "thscode": code,
                    "open": close - 0.02,
                    "high": close + 0.05,
                    "low": close - 0.05,
                    "close": close,
                    "vwap": close,
                    "volume": 1_000_000 + offset,
                }
            )
    target = tmp_path / "provider"
    original_replace = current_shadow_module.os.replace
    attempts = 0

    def transient_windows_lock(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated transient scanner lock")
        return original_replace(source, destination)

    monkeypatch.setattr(current_shadow_module.os, "replace", transient_windows_lock)

    result = write_qlib_provider(pd.DataFrame(records), target)

    assert result == {"reused": False, "calendar_days": 85, "instruments": 2}
    assert attempts == 3
    assert (target / "calendars" / "day.txt").read_text().splitlines()[-1] == dates[-1].strftime("%Y-%m-%d")
    assert "sh600000" in (target / "instruments" / "csi300.txt").read_text()
    values = np.fromfile(target / "features" / "sh600000" / "close.day.bin", dtype="<f4")
    assert values[0] == 0
    assert len(values) == 86


def test_align_forward_adjusted_fields_scales_raw_vwap_to_adjusted_prices():
    adjusted = pd.DataFrame(
        [
            {
                "time": "2026-07-20",
                "thscode": "600000.SH",
                "open": 20.0,
                "high": 22.0,
                "low": 19.0,
                "close": 21.0,
                "vwap": 10.2,
                "volume": None,
            }
        ]
    )
    raw = pd.DataFrame(
        [
            {
                "time": "2026-07-20",
                "thscode": "600000.SH",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "vwap": 10.2,
                "volume": 1000.0,
            }
        ]
    )

    frame, summary = align_forward_adjusted_fields(adjusted, raw)

    assert frame.iloc[0]["vwap"] == 20.4
    assert frame.iloc[0]["volume"] == 1000.0
    assert summary["scaled_coverage"] == 1.0
