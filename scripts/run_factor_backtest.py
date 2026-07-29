from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_access import StockRepository
from app.factor_backtest import FactorBacktestService
from app.factor_evaluation import FactorEvaluationService
from app.factor_lab import FactorLabService
from app.research_store import ResearchStore
from app.quant_store import QuantStore


def main() -> None:
    parser = argparse.ArgumentParser(description="运行单因子 Top-N 策略回测")
    parser.add_argument("--evaluation-id")
    parser.add_argument("--factor-id", required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--rebalance-step", type=int, default=20)
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--commission-rate", type=float, default=0.0003)
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    parser.add_argument("--slippage-rate", type=float, default=0.001)
    parser.add_argument("--full", action="store_true", help="输出逐日净值和全部调仓记录")
    args = parser.parse_args()

    repository = StockRepository(settings.stock_qfq_db)
    research_store = ResearchStore(settings.state_db, settings.document_root)
    store = QuantStore(settings.quant_db, research_store)
    factor_lab = FactorLabService(
        store, repository, adjustment="CPS:2", universe="CSI300 current"
    )
    evaluation = FactorEvaluationService(store, repository, factor_lab)
    service = FactorBacktestService(store, repository, factor_lab, evaluation)
    evaluation_id = args.evaluation_id
    if not evaluation_id:
        latest = evaluation.latest()
        if latest is None:
            raise SystemExit("请先运行 M11.2 因子评价")
        evaluation_id = latest["run"]["evaluation_id"]
    result = service.run(
        evaluation_id=evaluation_id,
        factor_id=args.factor_id,
        start_date=args.start_date,
        end_date=args.end_date,
        top_n=args.top_n,
        rebalance_step=args.rebalance_step,
        initial_capital=args.initial_capital,
        commission_rate=args.commission_rate,
        stamp_duty_rate=args.stamp_duty_rate,
        slippage_rate=args.slippage_rate,
    )
    output = result if args.full else {
        "run": result["run"],
        "nav_point_count": len(result["nav"]),
        "rebalance_count": len(result["rebalances"]),
        "reused": result["reused"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
