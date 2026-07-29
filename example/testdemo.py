"""Minimal iFinD SDK smoke examples without embedded credentials."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from iFinDPy import THS_BD, THS_ReportQuery, THS_iFinDLogin, THS_iFinDLogout


def login() -> tuple[int, bool]:
    if not settings.ifind_username or not settings.ifind_password:
        raise RuntimeError("请在项目 .env 中配置 IFIND_USER 和 IFIND_PASSWORD")
    result = int(THS_iFinDLogin(settings.ifind_username, settings.ifind_password))
    if result not in {0, -201}:
        raise RuntimeError(f"iFinD 登录失败（{result}）")
    return result, result == 0


def basic_data_demo() -> None:
    result = THS_BD("300750.SZ", "ths_stock_short_name_stock", "")
    if result.errorcode != 0:
        raise RuntimeError(f"基础数据查询失败：{result.errmsg}")
    print(result.data)


def report_query_demo() -> None:
    today = date.today().isoformat()
    result = THS_ReportQuery(
        "300750.SZ",
        f"beginrDate:{today};endrDate:{today}",
        "reportDate:Y,thscode:Y,secName:Y,ctime:Y,reportTitle:Y,pdfURL:Y,seq:Y",
    )
    if result.errorcode != 0:
        raise RuntimeError(f"公告查询失败：{result.errmsg}")
    print(result.data)


def main() -> int:
    parser = argparse.ArgumentParser(description="iFinD SDK 本机环境冒烟测试")
    parser.add_argument("--basic", action="store_true", help="查询证券简称")
    parser.add_argument("--reports", action="store_true", help="查询当日公告")
    args = parser.parse_args()

    code, owns_login = login()
    print(f"iFinD 登录成功（{code}）")
    try:
        if args.basic:
            basic_data_demo()
        if args.reports:
            report_query_demo()
    finally:
        if owns_login:
            THS_iFinDLogout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
