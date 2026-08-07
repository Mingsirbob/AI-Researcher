import sqlite3
from datetime import date
from types import SimpleNamespace

import pandas as pd

from app.core.config import Settings
from app.data.ifind import IFindDataError, IFindDataLayer
from app.api.handlers import enrich_with_ifind, public_ifind_context


def result(rows):
    return SimpleNamespace(errorcode=0, errmsg="", data=pd.DataFrame(rows))


def layer_with_sdk(tmp_path, sdk):
    layer = IFindDataLayer(
        Settings(
            stock_db=tmp_path / "stock.db",
            state_db=tmp_path / "state.db",
            ifind_username="researcher",
            ifind_password="secret-value",
            ifind_timeout_seconds=1,
        )
    )
    layer._sdk = sdk
    return layer


def test_ifind_status_does_not_expose_credentials(tmp_path):
    layer = IFindDataLayer(
        Settings(
            state_db=tmp_path / "state.db",
            ifind_username="researcher",
            ifind_password="secret-value",
        )
    )
    status = layer.status()
    assert status["configured"] is True
    assert "secret-value" not in str(status)
    assert "researcher" not in str(status)


def test_single_layer_reuses_one_login_and_owns_logout(tmp_path):
    calls = []
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda user, password: calls.append("login") or 0,
        THS_iFinDLogout=lambda: calls.append("logout") or 0,
    )
    layer = layer_with_sdk(tmp_path, sdk)

    layer.start()
    layer.start()
    layer.close()

    assert calls == ["login", "logout"]


def test_daily_prices_pass_adjustment_per_call(tmp_path):
    calls = []
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: 0,
        THS_iFinDLogout=lambda: 0,
        THS_HD=lambda *args: calls.append(args) or result([
            {
                "time": "2026-07-30", "thscode": "000001.SZ",
                "open": 10, "high": 11, "low": 9, "close": 10.5,
                "vwap": 10.2, "volume": 1000,
            }
        ]),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    frame = layer.get_daily_prices(
        ["000001.SZ"], date(2026, 7, 30), date(2026, 7, 30), adjustment="forward"
    )

    assert calls[0][2] == "CPS:2"
    assert frame.iloc[0]["close"] == 10.5


def test_index_members_use_ths_dr_and_save_snapshot(tmp_path):
    calls = []
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: 0,
        THS_iFinDLogout=lambda: 0,
        THS_DR=lambda *args: calls.append(args) or result([
            {"p03473_f001": "000001.SZ", "p03473_f002": "平安银行", "p03473_f003": 0.5},
            {"p03473_f001": "600000.SH", "p03473_f002": "浦发银行", "p03473_f003": 0.4},
        ]),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    members = layer.get_index_members("000300.CSI", date(2026, 7, 30))

    assert calls[0][0] == "p03473"
    assert calls[0][1] == "iv_date=20260730;iv_zsdm=000300.CSI"
    assert members["security_code"].tolist() == ["000001.SZ", "600000.SH"]
    with sqlite3.connect(tmp_path / "stock.db") as conn:
        saved = conn.execute(
            "SELECT COUNT(*) FROM universe_member WHERE universe_code='000300.CSI'"
        ).fetchone()[0]
    assert saved == 2


def test_universe_query_uses_wcquery_and_normalizes_members(tmp_path):
    calls = []
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: 0,
        THS_iFinDLogout=lambda: 0,
        THS_WCQuery=lambda *args: calls.append(args) or result([
            {"股票代码": "600000.SH", "股票简称": "浦发银行"},
            {"股票代码": "000001.SZ", "股票简称": "平安银行"},
            {"股票代码": "920002.BJ", "股票简称": "万达轴承"},
        ]),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    members = layer.get_universe_by_query("沪深300成分股")

    assert calls == [("沪深300成分股", "stock")]
    assert members["security_code"].tolist() == ["000001.SZ", "600000.SH", "920002.BJ"]
    assert members["weight"].isna().all()


def test_company_data_uses_bd_and_report_query_in_one_session(tmp_path):
    calls = []
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: calls.append("login") or 0,
        THS_iFinDLogout=lambda: 0,
        THS_BD=lambda *_: result([
            {"thscode": "600183.SH", "ths_stock_short_name_stock": "生益科技"}
        ]),
        THS_ReportQuery=lambda *_: result([
            {
                "reportDate": "2026-07-01", "ctime": "2026-07-01 18:00:00",
                "reportTitle": "关于年度报告的公告", "pdfURL": "https://example.com/a.pdf",
                "seq": "1",
            }
        ]),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    company = layer.get_company_data(["600183.SH"], as_of=date(2026, 7, 30))

    assert calls == ["login"]
    assert company["profiles"][0]["name"] == "生益科技"
    assert company["announcements"]["600183.SH"][0]["sequence"] == "1"


def test_realtime_quotes_normalize_contract(tmp_path):
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: 0,
        THS_iFinDLogout=lambda: 0,
        THS_RQ=lambda *_: result([{
            "time": "2026-07-23 10:01:02", "thscode": "000001.SZ",
            "open": 10.1, "latest": 10.2, "high": 10.3, "low": 10.0,
            "volume": 1000, "amount": 10200, "preClose": 9.9,
        }]),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    quote = layer.get_realtime_quotes(["000001.SZ"])[0]

    assert quote["source"] == "iFinD THS_RQ"
    assert quote["quote_time"] == "2026-07-23 10:01:02+08:00"
    assert quote["previous_close"] == 9.9


def test_ifind_error_keeps_vendor_code(tmp_path):
    sdk = SimpleNamespace(
        THS_iFinDLogin=lambda *_: 0,
        THS_iFinDLogout=lambda: 0,
        THS_BD=lambda *_: SimpleNamespace(errorcode=-4301, errmsg="quota exceeded", data=None),
    )
    layer = layer_with_sdk(tmp_path, sdk)

    try:
        layer.get_company_data(
            ["600183.SH"], as_of=date(2026, 7, 30), include_announcements=False
        )
    except IFindDataError as exc:
        assert exc.error_code == -4301
    else:
        raise AssertionError("非零错误码必须失败")


def test_ifind_announcements_become_evidence():
    analysis = {"security": {"code": "300750.SZ", "name": "300750.SZ"}, "evidence": []}
    context = {
        "security": {"code": "300750.SZ", "name": "宁德时代"},
        "announcements": [{
            "date": "2026-07-01", "published_at": "2026-07-01 18:00:00",
            "title": "关于年度报告的公告", "url": "https://example.com/report.pdf",
            "sequence": "1",
        }],
    }
    enrich_with_ifind(analysis, context)
    assert analysis["security"]["name"] == "宁德时代"
    assert analysis["evidence"][0]["category"] == "external_document"


def test_public_context_does_not_expose_download_token():
    context = {"announcements": [{
        "title": "公告", "url": "https://ft.10jqka.com.cn/file?token=secret-token", "sequence": "1"
    }]}
    public = public_ifind_context(context)
    assert "secret-token" not in str(public)
    assert public["announcements"][0]["has_source_url"] is True
