#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ccxt_binance_toolkit.py
-------------------------
Freqtrade 밖에서 CCXT로 바이낸스를 직접 다루는 유틸리티 스크립트.
- 잔고 조회
- 실시간 시세 조회
- OHLCV 데이터 직접 받아오기
- 시장가/지정가 주문 함수 (기본은 안전하게 dry-run처럼 print만 하도록 되어 있음)

설치:
    pip install ccxt

사용 전 필수:
    - API_KEY / API_SECRET을 환경변수로 설정 (코드에 직접 쓰지 말 것)
      export BINANCE_API_KEY="..."
      export BINANCE_API_SECRET="..."
"""

import os
import ccxt
import pandas as pd
from datetime import datetime


def get_exchange():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    if not api_key or not api_secret:
        print("[경고] 환경변수 BINANCE_API_KEY / BINANCE_API_SECRET 이 설정되지 않았습니다. "
              "공개 데이터(시세, OHLCV)만 조회 가능합니다.")

    config = {
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    # apiKey/secret을 빈 문자열로라도 넘기면 ccxt가 "인증된 사용자"로 판단해서
    # market 정보 로딩에 인증이 필요한 엔드포인트를 타 버려 공개 조회까지 막힘.
    # 실제 값이 있을 때만 넣어야 공개 데이터(시세/OHLCV) 조회가 정상 동작함.
    if api_key and api_secret:
        config["apiKey"] = api_key
        config["secret"] = api_secret

    exchange = ccxt.binance(config)
    return exchange


def fetch_balance(exchange):
    """전체 잔고 중 0보다 큰 자산만 출력"""
    balance = exchange.fetch_balance()
    non_zero = {k: v for k, v in balance["total"].items() if v and v > 0}
    print("\n[잔고]")
    for asset, amount in non_zero.items():
        print(f"  {asset}: {amount}")
    return non_zero


def fetch_ticker(exchange, symbol="BTC/USDT"):
    """실시간 시세 조회"""
    ticker = exchange.fetch_ticker(symbol)
    print(f"\n[{symbol} 시세] 현재가: {ticker['last']} / 24h 변동: {ticker['percentage']}%")
    return ticker


def fetch_ohlcv_df(exchange, symbol="BTC/USDT", timeframe="1h", limit=200):
    """OHLCV 데이터를 pandas DataFrame으로 반환 (VectorBT/Backtrader 등에 바로 활용 가능)"""
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def place_market_order(exchange, symbol, side, amount, dry_run=True):
    """
    시장가 주문 실행 함수.
    기본값은 dry_run=True 로, 실제 주문을 넣지 않고 내용만 출력합니다.
    실제 주문을 넣으려면 반드시 dry_run=False를 명시적으로 지정해야 함.
    """
    print(f"\n[주문 요청] {side.upper()} {amount} {symbol} (dry_run={dry_run})")

    if dry_run:
        print("  -> dry_run 모드이므로 실제 주문은 전송되지 않았습니다.")
        return None

    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다.")

    order = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount)
    print(f"  -> 주문 체결: {order}")
    return order


def monitor_multiple_pairs(exchange, symbols):
    """여러 페어 동시 모니터링 (Freqtrade 밖에서 커스텀 감시 로직 짤 때 활용)"""
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 멀티 페어 모니터링")
    for symbol in symbols:
        try:
            ticker = exchange.fetch_ticker(symbol)
            print(f"  {symbol}: {ticker['last']} ({ticker['percentage']}%)")
        except Exception as e:
            print(f"  {symbol}: 조회 실패 - {e}")


if __name__ == "__main__":
    ex = get_exchange()

    fetch_ticker(ex, "BTC/USDT")
    df = fetch_ohlcv_df(ex, "BTC/USDT", timeframe="1h", limit=50)
    print("\n[최근 OHLCV 5개]")
    print(df.tail())

    monitor_multiple_pairs(ex, ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"])

    # 잔고 조회는 API 키가 있어야 동작함
    try:
        fetch_balance(ex)
    except Exception as e:
        print(f"\n[잔고 조회 실패] API 키를 확인하세요: {e}")

    # 주문 예시 (기본 dry_run=True 라 실제 전송 안 됨)
    place_market_order(ex, "BTC/USDT", "buy", 0.001, dry_run=True)
