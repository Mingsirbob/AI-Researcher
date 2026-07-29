from app.research.analysis import analyze_stock
from app.market.repository import normalize_code


def _rows(count: int = 300) -> list[dict]:
    return [
        {
            "time": f"2025-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "open": 10 + index * 0.02,
            "high": 10.2 + index * 0.02,
            "low": 9.8 + index * 0.02,
            "close": 10 + index * 0.02,
            "vwap": 10 + index * 0.02,
            "volume": 1_000_000 + index * 1000,
        }
        for index in range(count)
    ]


def test_normalize_code_infers_exchange():
    assert normalize_code("600519") == "600519.SH"
    assert normalize_code("300750") == "300750.SZ"
    assert normalize_code("830001") == "830001.BJ"


def test_analysis_produces_traceable_claims():
    result = analyze_stock("300750.SZ", "宁德时代", _rows())
    evidence_ids = {item["id"] for item in result["evidence"]}
    assert result["metrics"]["trend"] == "偏强"
    assert result["as_of"] == _rows()[-1]["time"]
    assert all(set(claim["evidence_ids"]).issubset(evidence_ids) for claim in result["claims"])
    assert result["claims"][0]["claim_type"] == "inference"


def test_suspension_does_not_break_volume_analysis():
    rows = _rows()
    rows[-1].update({"open": None, "high": None, "low": None, "volume": None})
    result = analyze_stock("300750.SZ", "宁德时代", rows)
    assert result["metrics"]["suspended"] is True
    assert result["metrics"]["volume_ratio_20d"] is None
