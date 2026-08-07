from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal


Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class ChinaAConfig:
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_rate: float = 0.001
    lot_size: int = 100
    max_volume_participation: float = 0.10

    def __post_init__(self) -> None:
        for name, value in (
            ("commission_rate", self.commission_rate),
            ("minimum_commission", self.minimum_commission),
            ("stamp_duty_rate", self.stamp_duty_rate),
            ("transfer_fee_rate", self.transfer_fee_rate),
            ("slippage_rate", self.slippage_rate),
            ("max_volume_participation", self.max_volume_participation),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是非负有限数")
        if self.lot_size < 1:
            raise ValueError("lot_size 必须为正整数")
        if self.max_volume_participation > 1:
            raise ValueError("max_volume_participation 不能超过 1")


class ChinaAMarketRules:
    def __init__(self, config: ChinaAConfig | None = None):
        self.config = config or ChinaAConfig()

    def fee(self, side: Side, quantity: int, execution_price: float) -> float:
        notional = quantity * execution_price
        if notional <= 0:
            return 0.0
        commission = max(notional * self.config.commission_rate, self.config.minimum_commission)
        transfer = notional * self.config.transfer_fee_rate
        stamp = notional * self.config.stamp_duty_rate if side == "sell" else 0.0
        return commission + transfer + stamp

    def execution_price(self, side: Side, market_price: float) -> float:
        direction = 1 if side == "buy" else -1
        return market_price * (1 + direction * self.config.slippage_rate)

    def round_buy_quantity(self, quantity: float) -> int:
        return max(int(quantity // self.config.lot_size) * self.config.lot_size, 0)

    def round_sell_quantity(self, quantity: float, *, liquidating: bool) -> int:
        if liquidating:
            return max(int(quantity), 0)
        return max(int(quantity // self.config.lot_size) * self.config.lot_size, 0)

    def volume_cap(self, volume: Any) -> int | None:
        value = _finite(volume)
        if value is None or value <= 0:
            return None
        return self.round_buy_quantity(value * self.config.max_volume_participation)

    def tradability_reason(
        self,
        *,
        security_code: str,
        side: Side,
        row: dict[str, Any],
        previous_close: float | None,
        trading_date: str,
        is_st: bool = False,
    ) -> str | None:
        open_price = _finite(row.get("open"))
        volume = _finite(row.get("volume"))
        if open_price is None or open_price <= 0 or volume is None or volume <= 0:
            return "suspended_or_missing_open"
        if previous_close is None or previous_close <= 0:
            return None
        limit = self.price_limit(security_code, trading_date=trading_date, is_st=is_st)
        tolerance = 1e-6
        if side == "buy" and open_price >= previous_close * (1 + limit) - tolerance:
            return "limit_up"
        if side == "sell" and open_price <= previous_close * (1 - limit) + tolerance:
            return "limit_down"
        return None

    @staticmethod
    def price_limit(security_code: str, *, trading_date: str, is_st: bool = False) -> float:
        if is_st:
            return 0.05
        code = security_code.split(".")[0].replace("_", "")
        if security_code.endswith((".BJ", "_BJ")) or code.startswith(("4", "8", "9")):
            return 0.30
        if code.startswith(("688", "689")):
            return 0.20
        if code.startswith(("300", "301")):
            return 0.20 if date.fromisoformat(trading_date) >= date(2020, 8, 24) else 0.10
        return 0.10


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
