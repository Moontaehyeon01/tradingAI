# -*- coding: utf-8 -*-
"""
nautilus_ma_rsi_strategy.py
------------------------------
NautilusTrader로 구현한 MA크로스+RSI 전략.
NautilusTrader는 이벤트 기반 고성능 엔진이라 실전 저지연 매매에 적합합니다.
지금 단계에선 백테스트 엔진으로 먼저 검증하는 용도로 사용하세요.

설치:
    pip install nautilus_trader

참고: NautilusTrader는 버전업이 빠른 편이라 정확한 API는
      설치된 버전의 공식 문서를 함께 참고하는 걸 권장합니다.
"""

from decimal import Decimal
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.indicators.rsi import RelativeStrengthIndex
from nautilus_trader.indicators.average.sma import SimpleMovingAverage


class MaRsiStrategyConfig(StrategyConfig):
    instrument_id: str = "BTCUSDT-SPOT.BINANCE"
    bar_type: str = "BTCUSDT-SPOT.BINANCE-1-HOUR-LAST-EXTERNAL"
    fast_period: int = 20
    slow_period: int = 50
    rsi_period: int = 14
    rsi_buy_threshold: float = 50.0
    rsi_sell_threshold: float = 70.0
    trade_size: Decimal = Decimal("0.01")
    stop_loss_pct: float = 0.05


class MaRsiStrategy(Strategy):
    def __init__(self, config: MaRsiStrategyConfig):
        super().__init__(config)

        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)

        self.fast_ma = SimpleMovingAverage(config.fast_period)
        self.slow_ma = SimpleMovingAverage(config.slow_period)
        self.rsi = RelativeStrengthIndex(config.rsi_period)

        self.entry_price = None
        self.position_open = False

    def on_start(self):
        self.subscribe_bars(self.bar_type)
        self.log.info("MaRsiStrategy 시작됨 (NautilusTrader)")

    def on_bar(self, bar: Bar):
        close_price = float(bar.close)

        self.fast_ma.update_raw(close_price)
        self.slow_ma.update_raw(close_price)
        self.rsi.update_raw(close_price)

        if not (self.fast_ma.initialized and self.slow_ma.initialized and self.rsi.initialized):
            return

        fast_value = self.fast_ma.value
        slow_value = self.slow_ma.value
        rsi_value = self.rsi.value

        if not self.position_open:
            # 골든크로스 + RSI 조건 (단순화: 매 바마다 fast>slow 여부만 체크,
            # 실전에서는 직전값과 비교해서 "돌파 시점"만 잡도록 보완 권장)
            if fast_value > slow_value and rsi_value > self.config.rsi_buy_threshold:
                self.submit_market_order(OrderSide.BUY)
                self.entry_price = close_price
                self.position_open = True
                self.log.info(f"매수 진입: {close_price}")

        else:
            # 손절 체크
            if self.entry_price and close_price <= self.entry_price * (1 - self.config.stop_loss_pct):
                self.submit_market_order(OrderSide.SELL)
                self.position_open = False
                self.log.info(f"손절 청산: {close_price}")
                return

            # 데드크로스 또는 RSI 과매수 청산
            if fast_value < slow_value or rsi_value > self.config.rsi_sell_threshold:
                self.submit_market_order(OrderSide.SELL)
                self.position_open = False
                self.log.info(f"청산: {close_price}")

    def submit_market_order(self, side: OrderSide):
        order: MarketOrder = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(self.config.trade_size),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def on_stop(self):
        self.log.info("MaRsiStrategy 종료됨")
