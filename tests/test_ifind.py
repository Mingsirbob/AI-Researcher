import pandas as pd
from types import SimpleNamespace

from app.core.config import Settings
from app.integrations.ifind import IFindError, IFindService
from app.api.handlers import enrich_with_ifind, public_ifind_context
from app.core.runtime_events import RunContext, RuntimeEventStore, bind_run_context


class Result:
    errorcode = 0
    errmsg = ""
    data = pd.DataFrame(
        [
            {
                "reportDate": "2026-07-01",
                "reportTitle": "关于年度报告的公告",
                "pdfURL": "https://example.com/report.pdf",
            }
        ]
    )


def test_ifind_status_does_not_expose_credentials(tmp_path):
    service = IFindService(
        Settings(
            state_db=tmp_path / "state.db",
            ifind_username="researcher",
            ifind_password="secret-value",
        )
    )
    status = service.status()
    assert status["configured"] is True
    assert "secret-value" not in str(status)
    assert "researcher" not in str(status)


def test_ifind_records_convert_dataframe():
    records = IFindService._records(Result())
    assert records[0]["reportTitle"] == "关于年度报告的公告"


def test_ifind_security_profiles_use_one_batch_call(tmp_path):
    calls = []
    service = IFindService(Settings(state_db=tmp_path / "state.db"))
    service._ensure_login = lambda: SimpleNamespace(
        THS_BD=lambda codes, indicator, params, output: calls.append(
            (codes, indicator, params, output)
        )
        or SimpleNamespace(
            errorcode=0,
            errmsg="",
            data=pd.DataFrame(
                [
                    {"thscode": "600183.SH", "ths_stock_short_name_stock": "生益科技"},
                    {"thscode": "300604.SZ", "ths_stock_short_name_stock": "长川科技"},
                ]
            ),
        )
    )

    profiles = service.get_security_profiles(["600183.SH", "300604.SZ"])

    assert calls[0][0] == "600183.SH,300604.SZ"
    assert [item["name"] for item in profiles] == ["生益科技", "长川科技"]


def test_ifind_realtime_quotes_use_ths_rq_and_normalize_contract(tmp_path):
    calls = []
    service = IFindService(Settings(state_db=tmp_path / "state.db"))
    service._ensure_login = lambda: SimpleNamespace(
        THS_RQ=lambda codes, indicators, params, output: calls.append(
            (codes, indicators, params, output)
        )
        or SimpleNamespace(
            errorcode=0,
            errmsg="",
            data=pd.DataFrame([{
                "time": "2026-07-23 10:01:02", "thscode": "000001.SZ",
                "open": 10.1, "latest": 10.2, "high": 10.3, "low": 10.0,
                "volume": 1000, "amount": 10200, "preClose": 9.9,
            }]),
        )
    )

    quotes = service.get_realtime_quotes(["000001.SZ"])

    assert calls[0][0] == "000001.SZ"
    assert calls[0][1] == "open;latest;high;low;volume;amount;preClose"
    assert quotes[0]["source"] == "iFinD THS_RQ"
    assert quotes[0]["quote_time"] == "2026-07-23 10:01:02+08:00"
    assert quotes[0]["previous_close"] == 9.9


def test_ifind_announcements_become_evidence():
    analysis = {"security": {"code": "300750.SZ", "name": "300750.SZ"}, "evidence": []}
    context = {
        "security": {"code": "300750.SZ", "name": "宁德时代"},
        "announcements": [
            {
                "date": "2026-07-01",
                "published_at": "2026-07-01 18:00:00",
                "title": "关于年度报告的公告",
                "url": "https://example.com/report.pdf",
                "sequence": "1",
            }
        ],
    }
    enrich_with_ifind(analysis, context)
    assert analysis["security"]["name"] == "宁德时代"
    assert analysis["evidence"][0]["category"] == "external_document"
    assert "不代表已解析" in analysis["evidence"][0]["method"]


def test_public_context_does_not_expose_download_token():
    context = {
        "announcements": [
            {
                "title": "公告",
                "url": "https://ft.10jqka.com.cn/file?token=secret-token",
                "sequence": "1",
            }
        ]
    }
    public = public_ifind_context(context)
    assert "secret-token" not in str(public)
    assert public["announcements"][0]["has_source_url"] is True


def test_ifind_retries_transient_upstream_errors(tmp_path):
    calls = []
    settings = Settings(
        state_db=tmp_path / "state.db",
        ifind_max_attempts=3,
        ifind_backoff_seconds=0.001,
        ifind_circuit_failure_threshold=3,
    )
    service = IFindService(settings)

    def query():
        calls.append(1)
        if len(calls) < 3:
            return SimpleNamespace(errorcode=123, errmsg="temporary upstream failure", data=None)
        return Result()

    records = service._call("THS_BD", query)
    assert len(calls) == 3
    assert records[0]["reportTitle"] == "关于年度报告的公告"
    assert service.circuit.status()["state"] == "closed"


def test_ifind_does_not_retry_authentication_error(tmp_path):
    calls = []
    service = IFindService(
        Settings(
            state_db=tmp_path / "state.db",
            ifind_max_attempts=3,
            ifind_backoff_seconds=0.001,
        )
    )

    def query():
        calls.append(1)
        return SimpleNamespace(errorcode=-2, errmsg="账号或密码错误", data=None)

    try:
        service._call("THS_BD", query)
    except IFindError as exc:
        assert exc.error_code == -2
    else:
        raise AssertionError("认证错误必须失败")
    assert len(calls) == 1


def test_ifind_runtime_events_share_call_and_step_ids(tmp_path):
    settings = Settings(
        state_db=tmp_path / "state.db",
        ifind_max_attempts=3,
        ifind_backoff_seconds=0.001,
        ifind_circuit_failure_threshold=3,
    )
    service = IFindService(settings)
    event_store = RuntimeEventStore(settings.state_db)
    event_store.prepare_run(
        root_run_id="run-ifind",
        task_key="test",
        contract_version="v1",
        contract_hash="hash-ifind",
        input_payload={},
    )
    calls = []

    def query():
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(errorcode=123, errmsg="temporary upstream failure", data=None)
        return Result()

    context = RunContext(event_store, "run-ifind", step_run_id="step-ifind")
    with bind_run_context(context):
        records = service._call("THS_BD", query)

    assert records
    events = [event for event in event_store.events("run-ifind") if event["call_id"]]
    assert [event["event_type"] for event in events] == [
        "tool_started", "tool_retrying", "tool_completed"
    ]
    assert len({event["call_id"] for event in events}) == 1
    assert {event["step_run_id"] for event in events} == {"step-ifind"}
    with service.observer.connect() as conn:
        observed = conn.execute(
            "SELECT root_run_id, step_run_id, runtime_call_id FROM ifind_call_events "
            "WHERE operation='THS_BD' ORDER BY attempt"
        ).fetchall()
    assert {row["root_run_id"] for row in observed} == {"run-ifind"}
    assert {row["step_run_id"] for row in observed} == {"step-ifind"}
    assert {row["runtime_call_id"] for row in observed} == {events[0]["call_id"]}
