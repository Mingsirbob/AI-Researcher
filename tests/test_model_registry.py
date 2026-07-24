from datetime import datetime, timezone

import pandas as pd
import pytest

from app.model_registry import prepare_shadow_signals, qlib_instrument_to_code
from app.research_store import ResearchStore


def test_prepare_shadow_signals_ranks_each_historical_cross_section():
    index = pd.MultiIndex.from_tuples(
        [
            ("2020-07-30", "SH600000"),
            ("2020-07-30", "SZ000001"),
            ("2020-07-31", "SH600000"),
            ("2020-07-31", "SZ000001"),
        ],
        names=("datetime", "instrument"),
    )
    predictions = pd.DataFrame({"score": [0.1, 0.3, -0.2, 0.2]}, index=index)
    labels = pd.DataFrame({"LABEL0": [0.01, -0.02, 0.03, 0.04]}, index=index)

    summary, rows = prepare_shadow_signals(predictions, labels)

    assert summary == {
        "start_date": "2020-07-30",
        "end_date": "2020-07-31",
        "row_count": 4,
        "instrument_count": 2,
        "trading_days": 2,
    }
    assert qlib_instrument_to_code("SH600000") == "600000.SH"
    assert qlib_instrument_to_code("SZ000001") == "000001.SZ"
    latest = [item for item in rows if item["trading_date"] == "2020-07-31"]
    assert [(item["security_code"], item["cross_section_rank"]) for item in latest] == [
        ("600000.SH", 2),
        ("000001.SZ", 1),
    ]


def test_prepare_shadow_signals_rejects_index_mismatch():
    pred_index = pd.MultiIndex.from_tuples(
        [("2020-07-31", "SH600000")], names=("datetime", "instrument")
    )
    label_index = pd.MultiIndex.from_tuples(
        [("2020-07-31", "SZ000001")], names=("datetime", "instrument")
    )
    with pytest.raises(ValueError, match="索引不一致"):
        prepare_shadow_signals(
            pd.DataFrame({"score": [0.1]}, index=pred_index),
            pd.DataFrame({"LABEL0": [0.01]}, index=label_index),
        )


def test_model_run_registration_is_immutable_and_queryable(tmp_path):
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    imported_at = datetime.now(timezone.utc).isoformat()
    model = {
        "model_run_id": "run-1",
        "experiment_id": "experiment-1",
        "framework": "Qlib 0.9.7 / MLflow",
        "model_class": "LGBModel",
        "feature_set": "Alpha158",
        "universe": "csi300",
        "train_start": "2008-01-01",
        "train_end": "2014-12-31",
        "valid_start": "2015-01-01",
        "valid_end": "2016-12-31",
        "test_start": "2020-07-31",
        "test_end": "2020-07-31",
        "status": "accepted_historical_only",
        "intended_use": "shadow_evaluation_only",
        "source_path": "external/run-1",
        "source_fingerprint": "fingerprint-1",
        "config": {"prediction_trading_days": 1},
        "metrics": {"Rank IC": 0.05},
        "limitations": ["historical only"],
        "imported_at": imported_at,
    }
    artifacts = [
        {
            "artifact_type": "model",
            "file_path": "model.pkl",
            "sha256": "a" * 64,
            "byte_size": 10,
        }
    ]
    snapshot = {
        "snapshot_id": "snapshot-1",
        "start_date": "2020-07-31",
        "end_date": "2020-07-31",
        "row_count": 1,
        "instrument_count": 1,
        "status": "historical_only",
        "source_fingerprint": "fingerprint-1",
        "imported_at": imported_at,
    }
    signals = [
        {
            "trading_date": "2020-07-31",
            "security_code": "600000.SH",
            "source_instrument": "SH600000",
            "score": 0.1,
            "realized_label": 0.01,
            "cross_section_rank": 1,
            "cross_section_size": 1,
            "percentile": 100.0,
        }
    ]

    first = store.register_model_run(
        model_run=model, artifacts=artifacts, snapshot=snapshot, signals=signals
    )
    repeated = store.register_model_run(
        model_run=model, artifacts=artifacts, snapshot=snapshot, signals=signals
    )
    result = store.list_shadow_signals("run-1")

    assert first["reused"] is False
    assert repeated["reused"] is True
    assert result["total"] == 1
    assert result["items"][0]["security_code"] == "600000.SH"
    changed = {**model, "source_fingerprint": "different"}
    with pytest.raises(ValueError, match="拒绝覆盖"):
        store.register_model_run(
            model_run=changed, artifacts=artifacts, snapshot=snapshot, signals=signals
        )
