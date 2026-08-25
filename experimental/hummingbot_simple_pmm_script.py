# -*- coding: utf-8 -*-
"""
simple_pmm_script.py (Hummingbot Script Strategy)
----------------------------------------------------
Hummingbot은 Freqtrade와 달리 "마켓메이킹/차익거래" 전용 봇이라
추세추종형 MA+RSI 전략과는 목적이 다릅니다. 여기서는 Hummingbot의
정석 용도인 Pure Market Making(PMM) 전략을 구현합니다.

동작 방식:
  - 현재가 기준 위/아래로 스프레드(%)를 두고 매수/매도 지정가 주문을 계속 걸어둠
  - 체결되면 다시 새 주문을 걸어서 스프레드 차익을 반복적으로 취함
  - 변동성 큰 구간에서는 스프레드를 넓혀 리스크 축소 (동적 스프레드)

설치 및 실행 (Hummingbot 자체 설치 필요, pip 단독 설치 아님):
  1. https://hummingbot.org 가이드대로 Hummingbot 설치 (Docker 권장)
  2. 이 파일을 hummingbot/scripts/ 폴더에 복사
  3. Hummingbot CLI에서:
       start --script simple_pmm_script.py

주의: 마켓메이킹은 양방향 주문을 계속 걸어두는 방식이라
     추세가 강하게 한쪽으로 쏠리면 손실이 누적될 수 있습니다.
     반드시 소액/테스트넷으로 먼저 검증하세요.
"""

from decimal import Decimal
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class SimplePMMScript(ScriptStrategyBase):
    # ------------------------------------------------------------------
    # 설정값
    # ------------------------------------------------------------------
    bid_spread = Decimal("0.001")   # 매수 스프레드 0.1%
    ask_spread = Decimal("0.001")   # 매도 스프레드 0.1%
    order_refresh_time = 30         # 주문 갱신 주기(초)
    order_amount = Decimal("0.01")  # 주문당 수량 (BTC 기준 예시)

    trading_pair = "BTC-USDT"
    exchange = "binance"

    markets = {exchange: {trading_pair}}

    create_timestamp = 0

    def on_tick(self):
        if self.create_timestamp <= self.current_timestamp:
            self.cancel_all_orders()
            proposal = self.create_proposal()
            self.place_orders(proposal)
            self.create_timestamp = self.order_refresh_time + self.current_timestamp

    def create_proposal(self):
        ref_price = self.connectors[self.exchange].get_mid_price(self.trading_pair)

        # 최근 변동성에 따라 스프레드 동적 조정 (변동성 클수록 스프레드 확대)
        volatility_adjustment = self.get_volatility_adjustment()

        buy_price = ref_price * (Decimal("1") - self.bid_spread * volatility_adjustment)
        sell_price = ref_price * (Decimal("1") + self.ask_spread * volatility_adjustment)

        buy_order = self.create_order(TradeType.BUY, buy_price)
        sell_order = self.create_order(TradeType.SELL, sell_price)

        return [buy_order, sell_order]

    def get_volatility_adjustment(self) -> Decimal:
        """
        간단한 변동성 배수 계산 (1.0 = 기본, 값 클수록 스프레드 확대).
        실전에서는 ATR 등을 붙여서 더 정교하게 계산 가능.
        """
        candles_df = self.connectors[self.exchange].get_price_by_type(
            self.trading_pair, "mid"
        )
        # 단순화된 예시 - 실제로는 캔들 데이터로 표준편차 계산 권장
        return Decimal("1.0")

    def create_order(self, side: TradeType, price: Decimal):
        return {
            "trading_pair": self.trading_pair,
            "side": side,
            "amount": self.order_amount,
            "price": price,
            "order_type": OrderType.LIMIT,
        }

    def place_orders(self, proposal):
        for order in proposal:
            if order["side"] == TradeType.BUY:
                self.buy(
                    connector_name=self.exchange,
                    trading_pair=order["trading_pair"],
                    amount=order["amount"],
                    order_type=order["order_type"],
                    price=order["price"],
                )
            else:
                self.sell(
                    connector_name=self.exchange,
                    trading_pair=order["trading_pair"],
                    amount=order["amount"],
                    order_type=order["order_type"],
                    price=order["price"],
                )

    def cancel_all_orders(self):
        for order in self.get_active_orders(connector_name=self.exchange):
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)

    def format_status(self) -> str:
        """Hummingbot CLI의 'status' 명령 실행 시 표시되는 요약 정보"""
        if not self.ready_to_trade:
            return "봇 준비 중..."

        lines = []
        ref_price = self.connectors[self.exchange].get_mid_price(self.trading_pair)
        lines.append(f"현재 중간가: {ref_price}")
        lines.append(f"매수 스프레드: {self.bid_spread*100}% / 매도 스프레드: {self.ask_spread*100}%")

        active_orders = self.get_active_orders(connector_name=self.exchange)
        lines.append(f"활성 주문 수: {len(active_orders)}")

        return "\n".join(lines)
