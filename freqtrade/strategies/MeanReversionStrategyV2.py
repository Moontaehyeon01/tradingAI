# -*- coding: utf-8 -*-
"""
MeanReversionStrategyV2 (로컬 실험 전용, 배포 안 함)
-----------------------------------------------------
2026년 상반기(BTC 고점 대비 -19.9%) 백테스트에서 기존 MeanReversionStrategy가
큰 손실(-47.71%)을 낸 원인을 분석한 결과: "횡보장"으로 오판하고 진짜
하락추세에서 눌림목을 계속 사들이다 손절을 반복한 게 핵심 원인이었음
(ADX가 낮다고 항상 진짜 횡보장은 아님 - 완만한 하락추세도 ADX가 낮게 나올 수 있음).

이 버전은 장기 이동평균(EMA) 방향 필터를 추가함:
  - 롱 진입: 기존 조건 + 종가가 장기 EMA 위 (큰 흐름이 상승/중립일 때만 눌림목 매수)
  - 숏 진입: 기존 조건 + 종가가 장기 EMA 아래 (큰 흐름이 하락/중립일 때만 반등 매도)
  -> 대세와 반대로 "떨어지는 칼날 잡기"를 하지 않도록 방지
"""

from MeanReversionStrategy import MeanReversionStrategy
from pandas import DataFrame
import talib.abstract as ta


class MeanReversionStrategyV2(MeanReversionStrategy):

    # EMA 기간을 100으로 고정했더니 57개 구간 중 17개가 거래 0건이라 너무 보수적이었음
    # -> 하이퍼옵트로 구간마다 최적 기간을 찾도록 변경 (짧을수록 더 자주 거래 허용)
    startup_candle_count = 220
    trend_ema_period = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["trend_ema"] = ta.EMA(dataframe, timeperiod=self.trend_ema_period)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        # 큰 흐름과 반대 방향인 진입은 취소 (하락추세에서 롱 금지, 상승추세에서 숏 금지)
        dataframe.loc[dataframe["close"] < dataframe["trend_ema"], "enter_long"] = 0
        dataframe.loc[dataframe["close"] > dataframe["trend_ema"], "enter_short"] = 0
        return dataframe
