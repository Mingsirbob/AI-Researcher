import sqlite3

from app.quant_research import neutralize_factor, universe_membership


def test_neutralize_factor_removes_industry_and_size_exposure():
    rows = []
    for index in range(12):
        industry = "A" if index < 6 else "B"
        cap = 100 + index * 20
        value = (3 if industry == "B" else 0) + 2 * __import__("math").log(cap) + (index % 3 - 1) * 0.1
        rows.append({"code": str(index), "industry_l1": industry, "market_cap": cap, "factor": value})
    result = neutralize_factor(rows, factor_key="factor")
    assert len(result["items"]) == 12
    assert abs(sum(item["neutralized_value"] for item in result["items"])) < 1e-10
    assert {item["code"] for item in result["items"]} == {str(index) for index in range(12)}
    assert result["method"] == "industry-dummy + log-market-cap OLS"


def test_neutralize_factor_quarantines_invalid_rows():
    result = neutralize_factor(
        [{"factor": 1, "market_cap": 0, "industry_l1": "A"}],
        factor_key="factor",
    )
    assert result["items"] == []
    assert result["excluded"][0]["neutralization_reason"] == "invalid_numeric_input"


def test_universe_membership_supports_snapshot_contract(tmp_path):
    path = tmp_path / "prices.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE price_universe_membership (security_code TEXT PRIMARY KEY, security_name TEXT NOT NULL, universe_name TEXT NOT NULL, universe_as_of TEXT NOT NULL, universe_source TEXT NOT NULL)")
        conn.execute("INSERT INTO price_universe_membership VALUES ('000001.SZ','平安银行','CSI300','2026-07-01','test')")
    before = universe_membership(path, as_of="2026-06-30", universe="CSI300")
    current = universe_membership(path, as_of="2026-07-01", universe="CSI300")
    assert before["items"] == []
    assert current["items"][0]["security_code"] == "000001.SZ"
    assert current["history_contract"] == "snapshot_as_of"
