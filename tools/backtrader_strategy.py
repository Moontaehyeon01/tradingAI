#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtrader_strategy.py
------------------------
Backtrader로 동일한 MA크로스+RSI 전략을 백테스트.
Freqtrade/VectorBT 결과와 교차검증(cross-check) 용도로 사용하면 좋습니다.
서로 다른 엔진에서 비슷한 결과가 나와야 신뢰도가 높아집니다.

설치:
    pip install backtrader ccxt pandas

실행:
    python backtrader_strategy.py
"""

import matplotlib
matplotlib.use("Agg")  # 차트를 파일로만 저장 (GUI 백엔드는 헤드리스 환경에서 멈출 수 있음)

import backtrader as bt
import ccxt
import pandas as pd
from datetime import datetime


class MaRsiStrategy(bt.Strategy):
    params = (
        ("fast_period", 20),
        ("slow_period", 50),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50),
        ("rsi_sell_threshold", 70),
        ("stop_loss_pct", 0.05),  # 5% 손절
    )

    def __init__(self):
        self.fast_ma = bt.indicators.SMA(self.data.close, period=self.p.fast_period)
        self.slow_ma = bt.indicators.SMA(self.data.close, period=self.p.slow_period)
        self.crossover = bt.indicators.CrossOver(self.fast_ma, self.slow_ma)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.p.rsi_period)

        self.order = None
        self.buy_price = None

    def log(self, txt):
        dt = self.datas[0].datetime.date(0)
        print(f"{dt.isoformat()} - {txt}")

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                self.buy_price = order.executed.price
                self.log(f"매수 체결: 가격 {order.executed.price:.2f}")
            else:
                self.log(f"매도 체결: 가격 {order.executed.price:.2f}")
        self.order = None

    def next(self):
        if self.order:
            return

        if not self.position:
            # 골든크로스 + RSI 조건
            if self.crossover > 0 and self.rsi[0] > self.p.rsi_buy_threshold:
                self.order = self.buy()
        else:
            # 손절 체크
            if self.buy_price and self.data.close[0] <= self.buy_price * (1 - self.p.stop_loss_pct):
                self.log(f"손절 매도 (진입가 대비 -{self.p.stop_loss_pct*100:.0f}%)")
                self.order = self.sell()
                return

            # 데드크로스 또는 RSI 과매수 청산
            if self.crossover < 0 or self.rsi[0] > self.p.rsi_sell_threshold:
                self.order = self.sell()


def fetch_ohlcv_df(symbol="BTC/USDT", timeframe="1h", limit=1000):
    exchange = ccxt.binance({"enableRateLimit": True})
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def run_backtest(save_chart=False):
    df = fetch_ohlcv_df("BTC/USDT", "1h", limit=1000)

    data = bt.feeds.PandasData(dataname=df)

    cerebro = bt.Cerebro()
    cerebro.adddata(data)
    cerebro.addstrategy(MaRsiStrategy)

    cerebro.broker.setcash(1000.0)
    cerebro.broker.setcommission(commission=0.001)  # 0.1% 수수료
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95)

    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    start_value = cerebro.broker.getvalue()
    print(f"시작 자본: {start_value:.2f}")

    results = cerebro.run()
    strat = results[0]

    end_value = cerebro.broker.getvalue()
    print(f"\n종료 자본: {end_value:.2f}")
    print(f"총 수익률: {(end_value / start_value - 1) * 100:.2f}%")

    try:
        print(f"샤프비율: {strat.analyzers.sharpe.get_analysis().get('sharperatio')}")
    except Exception:
        pass

    dd = strat.analyzers.drawdown.get_analysis()
    print(f"최대낙폭(MDD): {dd.max.drawdown:.2f}%")

    trades = strat.analyzers.trades.get_analysis()
    total_trades = trades.get("total", {}).get("total", 0)
    won_trades = trades.get("won", {}).get("total", 0)
    if total_trades > 0:
        print(f"총 거래횟수: {total_trades}, 승률: {won_trades/total_trades*100:.2f}%")

    # 차트 저장 (기본 비활성화: backtrader의 cerebro.plot()이 환경에 따라
    # 멈추는 경우가 있어 --chart 플래그를 줄 때만 시도)
    if save_chart:
        try:
            cerebro.plot(style="candlestick", savefig=dict(fname="backtrader_result.png"))
            print("\n차트 저장됨: backtrader_result.png")
        except Exception as e:
            print(f"\n차트 생성 스킵: {e}")


if __name__ == "__main__":
    import sys
    run_backtest(save_chart="--chart" in sys.argv)
