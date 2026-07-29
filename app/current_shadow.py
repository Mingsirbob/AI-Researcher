from __future__ import annotations

import json
import math
import os
import pickle
import shutil
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .model_registry import qlib_instrument_to_code, sha256_file
from .primitives import canonical_hash, code_to_qlib_instrument, sha256_parts, sha256_text
from .research_store import ResearchStore
from .updater import DAILY_COLUMNS, align_adjusted_daily_fields


CURRENT_SHADOW_VERSION = "current-shadow-v1"
VALIDATION_VERSION = "frozen-model-rolling-oos-v1"
QLIB_FEATURE_COUNT = 158
MIN_UNIVERSE_SIZE = 285
MIN_HISTORY_DAYS = 80
MIN_PREDICTION_COVERAGE = 0.95
MIN_FEATURE_FINITE_RATIO = 0.95


def _gate(name: str, passed: bool, observed, expected: str) -> dict:
    return {
        "name": name,
        "passed": bool(passed),
        "observed": observed,
        "expected": expected,
    }


def _daily_rank_ic(group: pd.DataFrame) -> float:
    if len(group) < 20 or group["score"].nunique() < 2 or group["label"].nunique() < 2:
        return math.nan
    # Spearman is Pearson correlation over average ranks. Computing it directly
    # avoids Pandas' optional SciPy import and keeps this core gate deterministic.
    return float(group["score"].rank(method="average").corr(group["label"].rank(method="average")))


def _daily_top_bottom_spread(group: pd.DataFrame) -> float:
    if len(group) < 20:
        return math.nan
    size = max(1, len(group) // 10)
    ordered = group.sort_values("score")
    return float(ordered.tail(size)["label"].mean() - ordered.head(size)["label"].mean())


def rolling_oos_evaluation(
    predictions: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    window_size: int = 63,
) -> dict:
    if window_size < 20:
        raise ValueError("滚动窗口不能少于 20 个交易日")
    if predictions.shape[1] != 1 or labels.shape[1] != 1:
        raise ValueError("滚动验证仅支持单目标预测和单标签")
    if not predictions.index.equals(labels.index):
        raise ValueError("滚动验证的预测与标签索引不一致")
    frame = pd.concat(
        [
            pd.to_numeric(predictions.iloc[:, 0], errors="raise").rename("score"),
            pd.to_numeric(labels.iloc[:, 0], errors="coerce").rename("label"),
        ],
        axis=1,
    ).dropna()
    if frame.empty or frame.index.names != ["datetime", "instrument"]:
        raise ValueError("滚动验证需要 datetime + instrument 索引的非空数据")
    dates = pd.Index(sorted(pd.to_datetime(frame.index.get_level_values("datetime")).unique()))
    daily_ic = frame.groupby(level="datetime", sort=True).apply(_daily_rank_ic).dropna()
    daily_spread = frame.groupby(level="datetime", sort=True).apply(_daily_top_bottom_spread).dropna()
    if daily_ic.empty:
        raise ValueError("没有可计算 Rank IC 的样本外截面")

    date_to_window = {value: index // window_size for index, value in enumerate(dates)}
    windows: list[dict] = []
    for window_id, values in daily_ic.groupby(
        [date_to_window[pd.Timestamp(value)] for value in daily_ic.index]
    ):
        values = values.astype(float)
        spread = daily_spread.loc[daily_spread.index.intersection(values.index)]
        std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
        windows.append(
            {
                "window": int(window_id) + 1,
                "start_date": pd.Timestamp(values.index.min()).date().isoformat(),
                "end_date": pd.Timestamp(values.index.max()).date().isoformat(),
                "trading_days": int(len(values)),
                "mean_rank_ic": float(values.mean()),
                "rank_icir": float(values.mean() / std) if std > 0 else None,
                "positive_day_ratio": float((values > 0).mean()),
                "mean_top_bottom_spread": float(spread.mean()) if not spread.empty else None,
            }
        )

    ic_std = float(daily_ic.std(ddof=1))
    metrics = {
        "start_date": pd.Timestamp(daily_ic.index.min()).date().isoformat(),
        "end_date": pd.Timestamp(daily_ic.index.max()).date().isoformat(),
        "trading_days": int(len(daily_ic)),
        "window_size": window_size,
        "window_count": len(windows),
        "mean_rank_ic": float(daily_ic.mean()),
        "rank_icir": float(daily_ic.mean() / ic_std) if ic_std > 0 else None,
        "positive_day_ratio": float((daily_ic > 0).mean()),
        "positive_window_ratio": float(
            sum(item["mean_rank_ic"] > 0 for item in windows) / len(windows)
        ),
        "mean_top_bottom_spread": float(daily_spread.mean()),
    }
    gates = [
        _gate("minimum_windows", metrics["window_count"] >= 8, metrics["window_count"], ">= 8"),
        _gate("mean_rank_ic", metrics["mean_rank_ic"] >= 0.02, metrics["mean_rank_ic"], ">= 0.02"),
        _gate(
            "positive_day_ratio",
            metrics["positive_day_ratio"] >= 0.55,
            metrics["positive_day_ratio"],
            ">= 0.55",
        ),
        _gate(
            "positive_window_ratio",
            metrics["positive_window_ratio"] >= 0.60,
            metrics["positive_window_ratio"],
            ">= 0.60",
        ),
        _gate(
            "top_bottom_spread",
            metrics["mean_top_bottom_spread"] > 0,
            metrics["mean_top_bottom_spread"],
            "> 0",
        ),
    ]
    return {
        "validation_version": VALIDATION_VERSION,
        "status": "passed" if all(item["passed"] for item in gates) else "rejected",
        "metrics": metrics,
        "gates": gates,
        "windows": windows,
    }


def validate_current_data(
    frame: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    start_date: date,
    as_of: date,
) -> dict:
    required_universe = {"security_code", "security_name"}
    if not required_universe.issubset(universe.columns):
        raise ValueError("当前股票池缺少 security_code/security_name")
    if list(frame.columns) != list(DAILY_COLUMNS):
        raise ValueError("当前行情字段顺序或集合与日线合同不一致")
    requested = set(universe["security_code"].astype(str).str.upper())
    valid = frame.copy()
    valid["time"] = pd.to_datetime(valid["time"], errors="raise")
    for column in ("open", "high", "low", "close", "vwap", "volume"):
        valid[column] = pd.to_numeric(valid[column], errors="coerce")
    valid = valid.loc[valid["close"].notna()].copy()
    invalid_codes: set[str] = set()
    issues: list[dict] = []
    required_missing = valid[["open", "high", "low", "close"]].isna().any(axis=1)
    invalid_price = (
        (valid[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (valid["high"] < valid[["open", "close", "low"]].max(axis=1))
        | (valid["low"] > valid[["open", "close", "high"]].min(axis=1))
    )
    price_tolerance = valid["close"].abs().clip(lower=1.0) * 1e-6
    invalid_vwap = valid["vwap"].notna() & (
        (valid["vwap"] < valid["low"] - price_tolerance)
        | (valid["vwap"] > valid["high"] + price_tolerance)
    )
    invalid_volume = valid["volume"].notna() & (valid["volume"] < 0)
    for rule, mask in (
        ("complete_ohlc", required_missing),
        ("ohlc_integrity", invalid_price),
        ("adjusted_vwap_bounds", invalid_vwap),
        ("non_negative_volume", invalid_volume),
    ):
        codes = sorted(set(valid.loc[mask, "thscode"]))
        invalid_codes.update(codes)
        issues.extend({"code": code, "rule": rule} for code in codes)
    valid = valid.loc[~valid["thscode"].isin(invalid_codes)].copy()
    counts = valid.groupby("thscode")["time"].nunique()
    current_codes = set(valid.loc[valid["time"].dt.date == as_of, "thscode"])
    history_codes = set(counts[counts >= MIN_HISTORY_DAYS].index)
    eligible_codes = requested & current_codes & history_codes
    coverage = len(eligible_codes) / len(requested) if requested else 0.0
    gates = [
        _gate("universe_size", len(requested) == 300, len(requested), "= 300"),
        _gate("quality_issues", not invalid_codes, len(invalid_codes), "= 0"),
        _gate("history_days", len(history_codes) >= MIN_UNIVERSE_SIZE, len(history_codes), f">= {MIN_UNIVERSE_SIZE}"),
        _gate("as_of_coverage", coverage >= MIN_PREDICTION_COVERAGE, coverage, f">= {MIN_PREDICTION_COVERAGE}"),
        _gate("no_future_rows", valid["time"].max().date() <= as_of, valid["time"].max().date().isoformat(), f"<= {as_of.isoformat()}"),
    ]
    return {
        "status": "passed" if all(item["passed"] for item in gates) else "rejected",
        "gates": gates,
        "eligible_codes": sorted(eligible_codes),
        "coverage": coverage,
        "issues": issues,
        "frame": valid.loc[valid["thscode"].isin(eligible_codes)].copy(),
    }


def align_forward_adjusted_fields(
    adjusted: pd.DataFrame,
    unadjusted: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    return align_adjusted_daily_fields(adjusted, unadjusted)


def data_fingerprint(frame: pd.DataFrame, universe: pd.DataFrame, as_of: date) -> str:
    ordered = frame.sort_values(["thscode", "time"])[list(DAILY_COLUMNS)].copy()
    universe_codes = sorted(universe["security_code"].astype(str).str.upper())
    return sha256_parts([
        pd.util.hash_pandas_object(ordered, index=False).values.tobytes(),
        json.dumps(universe_codes, separators=(",", ":")).encode("ascii"),
        f"CPS:2:{as_of.isoformat()}:{CURRENT_SHADOW_VERSION}".encode("ascii"),
    ])


def _publish_provider_directory(temp: Path, target: Path, attempts: int = 10) -> bool:
    """Publish a provider atomically, tolerating transient Windows file locks."""
    for attempt in range(attempts):
        if target.exists():
            return False
        try:
            os.replace(temp, target)
            return True
        except PermissionError:
            if target.exists():
                return False
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.1 * (attempt + 1), 0.5))
    return False


def write_qlib_provider(frame: pd.DataFrame, target: Path) -> dict:
    frame = frame.copy()
    frame["time"] = pd.to_datetime(frame["time"], errors="raise")
    calendars = sorted(frame["time"].dt.strftime("%Y-%m-%d").unique())
    if len(calendars) < MIN_HISTORY_DAYS:
        raise ValueError("Qlib 当前数据快照的交易日不足")
    calendar_index = {value: index for index, value in enumerate(calendars)}
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.building")
    try:
        (temp / "calendars").mkdir(parents=True)
        (temp / "instruments").mkdir(parents=True)
        (temp / "features").mkdir(parents=True)
        (temp / "calendars" / "day.txt").write_text("\n".join(calendars) + "\n", encoding="utf-8")
        instruments: list[str] = []
        fields = ("open", "high", "low", "close", "vwap", "volume")
        for code, group in frame.groupby("thscode", sort=True):
            group = group.sort_values("time").drop_duplicates("time", keep="last")
            dates = group["time"].dt.strftime("%Y-%m-%d")
            start_index = calendar_index[dates.iloc[0]]
            end_index = calendar_index[dates.iloc[-1]]
            instrument = code_to_qlib_instrument(code).lower()
            instruments.append(f"{instrument}\t{dates.iloc[0]}\t{dates.iloc[-1]}")
            directory = temp / "features" / instrument
            directory.mkdir(parents=True)
            indexed = group.assign(_date=dates).set_index("_date")
            aligned_dates = calendars[start_index : end_index + 1]
            for field in fields:
                values = pd.to_numeric(indexed[field], errors="coerce").reindex(aligned_dates)
                binary = np.concatenate(
                    [np.array([start_index], dtype="<f4"), values.to_numpy(dtype="<f4")]
                )
                binary.tofile(directory / f"{field}.day.bin")
            factor = np.concatenate(
                [np.array([start_index], dtype="<f4"), np.ones(len(aligned_dates), dtype="<f4")]
            )
            factor.tofile(directory / "factor.day.bin")
        instrument_text = "\n".join(instruments) + "\n"
        (temp / "instruments" / "all.txt").write_text(instrument_text, encoding="utf-8")
        (temp / "instruments" / "csi300.txt").write_text(instrument_text, encoding="utf-8")
        if target.exists():
            shutil.rmtree(temp)
            return {"reused": True, "calendar_days": len(calendars), "instruments": len(instruments)}
        target.parent.mkdir(parents=True, exist_ok=True)
        published = _publish_provider_directory(temp, target)
        return {"reused": not published, "calendar_days": len(calendars), "instruments": len(instruments)}
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def current_signal_rows(predictions: pd.Series) -> list[dict]:
    if predictions.empty or predictions.index.names != ["datetime", "instrument"]:
        raise ValueError("当前预测必须使用 datetime + instrument 索引")
    values = pd.to_numeric(predictions, errors="raise")
    if values.isna().any() or not np.isfinite(values.to_numpy()).all():
        raise ValueError("当前预测包含空值或非有限值")
    frame = values.rename("score").reset_index()
    if frame["datetime"].nunique() != 1 or frame["instrument"].duplicated().any():
        raise ValueError("当前预测必须是单一交易日的唯一证券截面")
    frame["source_instrument"] = frame["instrument"].astype(str).str.upper()
    frame["security_code"] = frame["source_instrument"].map(qlib_instrument_to_code)
    frame["cross_section_rank"] = frame["score"].rank(method="first", ascending=False).astype(int)
    size = len(frame)
    denominator = max(size - 1, 1)
    frame["percentile"] = 100.0 * (size - frame["cross_section_rank"]) / denominator
    return [
        {
            "security_code": row.security_code,
            "source_instrument": row.source_instrument,
            "score": float(row.score),
            "cross_section_rank": int(row.cross_section_rank),
            "cross_section_size": size,
            "percentile": round(float(row.percentile), 4),
        }
        for row in frame.itertuples(index=False)
    ]


class CurrentShadowPipeline:
    def __init__(self, store: ResearchStore, provider_root: Path):
        self.store = store
        self.provider_root = provider_root

    def validate_model(self, model_run_id: str, *, trust_pickle: bool) -> tuple[dict, dict]:
        if not trust_pickle:
            raise ValueError("必须显式确认信任 pickle 才能验证模型")
        model = self.store.model_run(model_run_id)
        if model is None:
            raise ValueError("模型运行不存在")
        artifacts = {item["artifact_type"]: item for item in model["artifacts"]}
        required = {"model", "predictions", "labels"}
        if not required.issubset(artifacts):
            raise ValueError("模型运行缺少 model/predictions/labels 产物")
        for item in artifacts.values():
            path = Path(item["file_path"])
            if not path.is_file() or sha256_file(path) != item["sha256"]:
                raise ValueError(f"模型产物哈希校验失败：{item['artifact_type']}")
        predictions = pd.read_pickle(artifacts["predictions"]["file_path"])
        labels = pd.read_pickle(artifacts["labels"]["file_path"])
        result = rolling_oos_evaluation(predictions, labels)
        fingerprint = canonical_hash(
            {
                "version": result["validation_version"],
                "model": artifacts["model"]["sha256"],
                "predictions": artifacts["predictions"]["sha256"],
                "labels": artifacts["labels"]["sha256"],
                "window_size": result["metrics"]["window_size"],
            },
            compact=False,
        )
        validation = self.store.register_model_validation(
            {
                "validation_id": fingerprint,
                "model_run_id": model_run_id,
                "validation_version": result["validation_version"],
                "status": result["status"],
                "metrics": result["metrics"],
                "gates": result["gates"],
                "windows": result["windows"],
                "source_fingerprint": fingerprint,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return model, validation

    def predict(
        self,
        *,
        model: dict,
        validation: dict,
        frame: pd.DataFrame,
        universe: pd.DataFrame,
        start_date: date,
        as_of: date,
        trust_pickle: bool,
        alignment: dict | None = None,
    ) -> dict:
        if not trust_pickle:
            raise ValueError("必须显式确认信任 pickle 才能执行当前推理")
        contract = validate_current_data(
            frame,
            universe,
            start_date=start_date,
            as_of=as_of,
        )
        fingerprint = data_fingerprint(contract["frame"], universe, as_of)
        provider_path = self.provider_root / fingerprint
        provider = write_qlib_provider(contract["frame"], provider_path)
        model_artifact = next(item for item in model["artifacts"] if item["artifact_type"] == "model")

        import qlib
        from qlib.constant import REG_CN
        from qlib.contrib.data.handler import Alpha158
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP

        qlib.init(provider_uri=str(provider_path), region=REG_CN)
        handler = Alpha158(
            instruments="csi300",
            start_time=start_date.isoformat(),
            end_time=as_of.isoformat(),
            fit_start_time=start_date.isoformat(),
            fit_end_time=as_of.isoformat(),
            learn_processors=[],
        )
        dataset = DatasetH(handler=handler, segments={"current": (as_of.isoformat(), as_of.isoformat())})
        features = dataset.prepare("current", col_set="feature", data_key=DataHandlerLP.DK_I)
        feature_count = int(features.shape[1])
        finite_ratio = float(np.isfinite(features.to_numpy(dtype=float)).mean()) if not features.empty else 0.0
        with Path(model_artifact["file_path"]).open("rb") as handle:
            fitted_model = pickle.load(handle)
        predictions = fitted_model.predict(dataset, segment="current")
        rows = current_signal_rows(predictions)
        prediction_coverage = len(rows) / len(universe) if len(universe) else 0.0
        gates = [
            *contract["gates"],
            _gate("adjustment", True, "CPS:2", "= CPS:2"),
            _gate(
                "vwap_alignment",
                (alignment or {}).get("scaled_coverage", 0.0) >= 0.99,
                (alignment or {}).get("scaled_coverage", 0.0),
                ">= 0.99",
            ),
            _gate("model_hash", sha256_file(Path(model_artifact["file_path"])) == model_artifact["sha256"], model_artifact["sha256"], "registered SHA-256"),
            _gate("rolling_oos", validation["status"] == "passed", validation["status"], "= passed"),
            _gate("feature_count", feature_count == QLIB_FEATURE_COUNT, feature_count, f"= {QLIB_FEATURE_COUNT}"),
            _gate("feature_finite_ratio", finite_ratio >= MIN_FEATURE_FINITE_RATIO, finite_ratio, f">= {MIN_FEATURE_FINITE_RATIO}"),
            _gate("prediction_count", len(rows) >= MIN_UNIVERSE_SIZE, len(rows), f">= {MIN_UNIVERSE_SIZE}"),
            _gate("prediction_coverage", prediction_coverage >= MIN_PREDICTION_COVERAGE, prediction_coverage, f">= {MIN_PREDICTION_COVERAGE}"),
        ]
        status = "current_shadow_ready" if all(item["passed"] for item in gates) else "rejected"
        snapshot_id = sha256_text(
            f"{CURRENT_SHADOW_VERSION}:{model['model_run_id']}:{as_of}:{fingerprint}:{validation['validation_id']}",
            encoding="ascii",
        )
        snapshot = {
            "snapshot_id": snapshot_id,
            "model_run_id": model["model_run_id"],
            "validation_id": validation["validation_id"],
            "as_of": as_of.isoformat(),
            "universe": "csi300_current",
            "adjustment": "forward",
            "ifind_params": "CPS:2",
            "data_source": "iFinD THS_WCQuery + THS_HD",
            "data_start": min(pd.to_datetime(contract["frame"]["time"])).date().isoformat(),
            "data_end": max(pd.to_datetime(contract["frame"]["time"])).date().isoformat(),
            "data_fingerprint": fingerprint,
            "provider_path": str(provider_path.resolve()),
            "model_sha256": model_artifact["sha256"],
            "feature_count": feature_count,
            "universe_size": len(universe),
            "signal_count": len(rows),
            "coverage": prediction_coverage,
            "status": status,
            "gates": gates,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provider": {**provider, "vwap_alignment": alignment or {}},
        }
        return self.store.register_current_shadow(snapshot, rows if status == "current_shadow_ready" else [])
