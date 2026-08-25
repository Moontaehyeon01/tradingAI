#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finrl_train_agent.py
-----------------------
FinRL로 강화학습 기반 트레이딩 에이전트를 학습.

주의: 이건 지금까지 만든 규칙 기반 전략(MA/RSI/MACD)과 완전히 다른 접근입니다.
강화학습 에이전트는 "언제 사고 팔지"를 데이터로부터 스스로 학습합니다.
학습이 잘 됐는지 검증하기 훨씬 어렵고, 블랙박스 성격이 강해서
지금 단계(백테스트도 막 끝낸 단계)에서 바로 실전 투입은 권장하지 않습니다.
연구/실험 목적으로 먼저 다뤄보는 걸 추천합니다.

설치:
    pip install finrl stable-baselines3 ccxt pandas numpy gymnasium

실행:
    python finrl_train_agent.py
"""

import ccxt
import pandas as pd
import numpy as np
from finrl.meta.env_stock_trading.env_stocktrading import StockTradingEnv
from finrl.agents.stablebaselines3.models import DRLAgent
from stable_baselines3.common.vec_env import DummyVecEnv


def fetch_and_prepare_data(symbol="BTC/USDT", timeframe="1h", limit=1000):
    exchange = ccxt.binance({"enableRateLimit": True})
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df["tic"] = symbol.replace("/", "")

    # FinRL StockTradingEnv가 기대하는 컬럼 형태로 정리
    df = df[["date", "open", "high", "low", "close", "volume", "tic"]]
    df = df.sort_values("date").reset_index(drop=True)

    # 간단한 기술적 지표 추가 (FinRL 환경의 tech_indicator_list와 매칭)
    df["sma_20"] = df["close"].rolling(20).mean()
    df["sma_50"] = df["close"].rolling(50).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    df = df.dropna().reset_index(drop=True)
    return df


def build_env(df):
    tech_indicators = ["sma_20", "sma_50", "rsi_14"]
    stock_dim = 1
    state_space = 1 + 2 * stock_dim + len(tech_indicators) * stock_dim

    env_kwargs = {
        "hmax": 1,  # 1회 최대 매매 수량 (심볼 1개 기준 정규화된 단위)
        "initial_amount": 1000,
        "num_stock_shares": [0],
        "buy_cost_pct": [0.001],
        "sell_cost_pct": [0.001],
        "reward_scaling": 1e-2,
        "state_space": state_space,
        "action_space": stock_dim,
        "tech_indicator_list": tech_indicators,
        "stock_dim": stock_dim,
    }

    env = StockTradingEnv(df=df, **env_kwargs)
    return env


def train_agent(env, total_timesteps=20000):
    vec_env = DummyVecEnv([lambda: env])

    agent = DRLAgent(env=vec_env)
    model = agent.get_model("ppo")

    print(f"\n학습 시작 (PPO, total_timesteps={total_timesteps})...")
    trained_model = agent.train_model(
        model=model,
        tb_log_name="ppo_crypto",
        total_timesteps=total_timesteps,
    )

    trained_model.save("finrl_ppo_crypto_model")
    print("학습 완료. 모델 저장: finrl_ppo_crypto_model.zip")
    return trained_model


def backtest_agent(model, df):
    """학습된 모델로 같은 기간(또는 별도 out-of-sample 기간)에서 시뮬레이션"""
    env = build_env(df)
    vec_env = DummyVecEnv([lambda: env])

    obs = vec_env.reset()
    done = False
    total_reward = 0

    while not done:
        action, _ = model.predict(obs)
        obs, reward, done, info = vec_env.step(action)
        total_reward += reward[0]

    print(f"\n[백테스트 결과] 누적 리워드: {total_reward:.4f}")
    print("주의: 리워드는 절대 수익률과 스케일이 다릅니다. env_kwargs의 reward_scaling 참고.")


if __name__ == "__main__":
    print("데이터 수집 중...")
    df = fetch_and_prepare_data("BTC/USDT", "1h", limit=1000)
    print(f"데이터 {len(df)}행 준비 완료")

    # 학습/검증 데이터 분리 (과최적화 방지 - 반드시 out-of-sample로 검증)
    split_idx = int(len(df) * 0.8)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    test_df = df.iloc[split_idx:].reset_index(drop=True)

    train_env = build_env(train_df)
    model = train_agent(train_env, total_timesteps=20000)

    print("\n학습 구간 성과:")
    backtest_agent(model, train_df)

    print("\n검증(out-of-sample) 구간 성과:")
    backtest_agent(model, test_df)
