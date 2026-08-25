# -*- coding: utf-8 -*-
"""
lumibot_ma_rsi_strategy.py
-----------------------------
Lumibot으로 구현한 MA크로스+RSI 전략.
Lumibot은 백테스트부터 실전 배포(브로커 연동)까지 같은 코드로 처리 가능한 게 장점입니다.
CCXT 기반 브로커 연동을 지원해서 바이낸스와도 연결할 수 있습니다.

설치:
    pip install lumibot

바이낸스 연동 시 필요:
    - lumibot의 CcxtBacktesting / Ccxt 브로커 클래스 사용
    - 환경변수로 API 키 관리 권장
"""

from lumibot.strategies.strategy import Strategy
from lumibot.brokers import Ccxt
from lumibot.backtesting import CcxtBacktesting
from lumibot.traders import Trader
import pandas as pd
import numpy as np
import os


class MaRsiLumibotStrategy(Strategy):
    parameters = {
        "symbol": "BTC/USDT",
        "fast_period": 20,
        "slow_period": 50,
        "rsi_period": 14,
        "rsi_buy_threshold": 50,
        "rsi_sell_threshold": 70,
        "stop_loss_pct": 0.05,
        "quantity_pct": 0.95,  # 매수 시 가용 현금의 95% 사용
    }

    def initialize(self):
        self.sleeptime = "1H"
        self.entry_price = None

    def calculate_rsi(self, prices: pd.Series, period: int) -> float:
        delta = prices.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if not rsi.empty else 50.0

    def on_trading_iteration(self):
        symbol = self.parameters["symbol"]
        fast_period = self.parameters["fast_period"]
        slow_period = self.parameters["slow_period"]

        bars = self.get_historical_prices(symbol, slow_period + 20, "hour")
        if bars is None or bars.df.empty:
            return

        df = bars.df
        closes = df["close"]

        fast_ma = closes.rolling(fast_period).mean().iloc[-1]
        slow_ma = closes.rolling(slow_period).mean().iloc[-1]
        rsi_value = self.calculate_rsi(closes, self.parameters["rsi_period"])

        current_price = self.get_last_price(symbol)
        position = self.get_position(symbol)

        if position is None or position.quantity == 0:
            # 진입 조건
            if fast_ma > slow_ma and rsi_value > self.parameters["rsi_buy_threshold"]:
                cash = self.get_cash()
                quantity = (cash * self.parameters["quantity_pct"]) / current_price
                order = self.create_order(symbol, quantity, "buy")
                self.submit_order(order)
                self.entry_price = current_price
                self.log_message(f"매수 진입: {current_price}, 수량: {quantity}")

        else:
            # 손절 체크
            if self.entry_price and current_price <= self.entry_price * (1 - self.parameters["stop_loss_pct"]):
                order = self.create_order(symbol, position.quantity, "sell")
                self.submit_order(order)
                self.log_message(f"손절 청산: {current_price}")
                return

            # 청산 조건
            if fast_ma < slow_ma or rsi_value > self.parameters["rsi_sell_threshold"]:
                order = self.create_order(symbol, position.quantity, "sell")
                self.submit_order(order)
                self.log_message(f"청산: {current_price}")


if __name__ == "__main__":
    # ---- 백테스트 실행 예시 ----
    backtesting_start = pd.Timestamp("2023-01-01")
    backtesting_end = pd.Timestamp("2024-12-31")

    MaRsiLumibotStrategy.backtest(
        CcxtBacktesting,
        backtesting_start,
        backtesting_end,
        benchmark_asset="BTC/USDT",
    )

    # ---- 실전/드라이런 배포 예시 (주석 해제 후 사용) ----
    # broker = Ccxt({
    #     "exchange_id": "binance",
    #     "apiKey": os.environ.get("BINANCE_API_KEY"),
    #     "secret": os.environ.get("BINANCE_API_SECRET"),
    #     "sandbox": True,  # 테스트넷 - 실전 전환 시 False
    # })
    # strategy = MaRsiLumibotStrategy(broker=broker)
    # trader = Trader()
    # trader.add_strategy(strategy)
    # trader.run_all()
