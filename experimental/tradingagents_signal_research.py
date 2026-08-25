#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tradingagents_signal_research.py
-----------------------------------
TradingAgents는 여러 LLM 에이전트(애널리스트/리서처/트레이더/리스크매니저 역할)가
토론을 거쳐 매매 신호를 만들어내는 리서치 프레임워크입니다.
지금까지 만든 규칙 기반 전략과는 결이 완전히 다르고, 실행할 때마다
LLM API를 호출하므로 비용이 발생하고 응답이 결정적(deterministic)이지 않습니다.

포지셔닝: 이건 "그대로 자동매매에 연결하는 용도"가 아니라,
정성적 리서치 보조(뉴스/펀더멘털 해석)로 참고하고, 실제 매수/매도 실행은
지금까지 만든 Freqtrade 규칙 기반 전략이 담당하는 하이브리드 구조를 권장합니다.

설치:
    git clone https://github.com/TauricResearch/TradingAgents.git
    cd TradingAgents
    pip install -r requirements.txt

필요:
    - OPENAI_API_KEY (또는 지원하는 다른 LLM provider 키) 환경변수 설정
    - 이 스크립트는 TradingAgents 리포를 클론한 폴더 안에서 실행하거나
      해당 경로를 PYTHONPATH에 추가해야 동작합니다.
"""

import os
from datetime import datetime

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
except ImportError:
    raise ImportError(
        "TradingAgents 패키지를 찾을 수 없습니다.\n"
        "먼저 https://github.com/TauricResearch/TradingAgents 를 클론하고,\n"
        "해당 폴더 안에서 이 스크립트를 실행하거나 PYTHONPATH에 추가하세요."
    )


def get_trading_signal(ticker: str, analysis_date: str):
    """
    지정 종목/날짜에 대해 TradingAgents의 멀티에이전트 토론을 실행하고
    최종 매매 의견(BUY/SELL/HOLD)과 근거를 반환합니다.
    """
    config = DEFAULT_CONFIG.copy()

    # 사용할 LLM 모델 지정 (비용/속도 고려해서 조정)
    config["deep_think_llm"] = "gpt-4o"       # 심층 분석용 (리서처, 리스크매니저)
    config["quick_think_llm"] = "gpt-4o-mini"  # 빠른 분석용 (초기 애널리스트)
    config["max_debate_rounds"] = 2            # 에이전트 간 토론 라운드 수
    config["online_tools"] = True              # 실시간 뉴스/데이터 도구 사용

    graph = TradingAgentsGraph(debug=True, config=config)

    print(f"\n[{ticker}] {analysis_date} 기준 멀티에이전트 분석 시작...")
    final_state, decision = graph.propagate(ticker, analysis_date)

    print(f"\n[최종 의견] {decision}")
    return final_state, decision


def hybrid_signal_check(ticker: str = "BTC-USD"):
    """
    실전 활용 예시: 매일 1회 정도만 실행해서
    Freqtrade 봇의 자동매매와 별개로 '정성적 참고 신호'로만 사용.
    (LLM 호출 비용 때문에 매 캔들마다 실행하는 건 비현실적)
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if not os.environ.get("OPENAI_API_KEY"):
        print("[경고] OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")
        return None

    final_state, decision = get_trading_signal(ticker, today)

    print("\n" + "=" * 60)
    print("주의: 이 결과는 참고용입니다.")
    print("실제 매매 실행은 Freqtrade의 규칙 기반 전략(백테스트로 검증된)이")
    print("담당하도록 하고, 이 신호는 재량 판단 보조 자료로만 활용하세요.")
    print("=" * 60)

    return decision


if __name__ == "__main__":
    hybrid_signal_check("BTC-USD")
