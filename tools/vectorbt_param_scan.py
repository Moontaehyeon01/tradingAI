#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vectorbt_param_scan.py
------------------------
VectorBT로 MA크로스+RSI 전략의 파라미터를 대량으로 빠르게 스캔.
Freqtrade hyperopt보다 훨씬 빨라서, 여기서 먼저 유망한 파라미터 범위를
좁힌 뒤 그 범위만 Freqtrade hyperopt로 정밀 검증하는 용도로 씁니다.

설치:
    pip install vectorbt ccxt pandas numpy

실행:
    python vectorbt_param_scan.py
"""

import itertools
import ccxt
import pandas as pd
import numpy as np
import vectorbt as vbt


def fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=1000):
    exchange = ccxt.binance({"enableRateLimit": True})
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def run_param_scan(df, fast_windows, slow_windows, rsi_window=14, rsi_threshold=50):
    close = df["close"]

    # fast_windows(13개) x slow_windows(10개)처럼 서로 길이가 다른 두 파라미터를
    # 완전 교차(cross join)하려면 vectorbt의 자동 정렬(combine)에 맡기지 않고
    # itertools.product로 조합을 직접 만들어 같은 컬럼 구조로 명시적으로 브로드캐스트한다.
    # (fast/slow 길이가 다르면 vectorbt가 페어 매칭을 시도하다가 정렬 실패함)
    fast_ma_df = vbt.MA.run(close, window=fast_windows, short_name="fast").ma
    slow_ma_df = vbt.MA.run(close, window=slow_windows, short_name="slow").ma
    rsi = vbt.RSI.run(close, window=rsi_window).rsi

    combos = list(itertools.product(fast_windows, slow_windows))
    columns = pd.MultiIndex.from_tuples(combos, names=["fast_window", "slow_window"])

    fast_bc = pd.concat([fast_ma_df[f] for f, _ in combos], axis=1)
    fast_bc.columns = columns
    slow_bc = pd.concat([slow_ma_df[s] for _, s in combos], axis=1)
    slow_bc.columns = columns
    rsi_bc = pd.concat([rsi] * len(combos), axis=1)
    rsi_bc.columns = columns

    # 모든 fast/slow 조합에 대해 크로스 신호 생성
    entries = (
        (fast_bc > slow_bc)
        & (fast_bc.shift(1) <= slow_bc.shift(1))
        & (rsi_bc > rsi_threshold)
    )
    exits = (fast_bc < slow_bc) & (fast_bc.shift(1) >= slow_bc.shift(1))

    # 전체 조합에 대해 포트폴리오 시뮬레이션 (수수료 0.1% 반영)
    portfolio = vbt.Portfolio.from_signals(
        close, entries, exits,
        fees=0.001,
        freq="1h",
        init_cash=1000,
    )

    return portfolio


def summarize_results(portfolio):
    stats = portfolio.total_return()

    if isinstance(stats, pd.Series):
        results = stats.sort_values(ascending=False)
        print("\n[상위 10개 파라미터 조합 - 총 수익률 기준]")
        print(results.head(10))

        best_combo = results.index[0]
        print(f"\n[최고 조합] {best_combo}")
        print(f"  총 수익률: {results.iloc[0] * 100:.2f}%")

        best_pf = portfolio[best_combo]
        print(f"  최대낙폭(MDD): {best_pf.max_drawdown() * 100:.2f}%")
        print(f"  샤프비율: {best_pf.sharpe_ratio():.2f}")
        print(f"  승률: {best_pf.trades.win_rate() * 100:.2f}%")
        print(f"  총 거래횟수: {best_pf.trades.count()}")

        return best_combo
    else:
        print(f"총 수익률: {stats * 100:.2f}%")
        return None


if __name__ == "__main__":
    print("바이낸스 BTC/USDT 1시간봉 데이터 수집 중...")
    df = fetch_ohlcv("BTC/USDT", "1h", limit=1000)
    print(f"데이터 {len(df)}개 확보 (기간: {df.index[0]} ~ {df.index[-1]})")

    # 넓은 범위로 빠르게 스캔 (수백 개 조합을 수 초~수십 초 내 처리)
    fast_windows = np.arange(5, 30, 2)     # 5,7,9,...,29
    slow_windows = np.arange(30, 80, 5)    # 30,35,...,75

    print(f"\n파라미터 스캔 시작: fast {list(fast_windows)} x slow {list(slow_windows)}")
    portfolio = run_param_scan(df, fast_windows, slow_windows)

    best_combo = summarize_results(portfolio)

    print("\n[다음 단계]")
    print("여기서 찾은 최고 조합 주변 범위를 MultiConfluenceStrategy.py의")
    print("IntParameter 범위로 좁혀서 Freqtrade hyperopt로 정밀 검증하세요.")
