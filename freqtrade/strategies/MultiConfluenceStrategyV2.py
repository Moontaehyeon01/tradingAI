# -*- coding: utf-8 -*-
"""
MultiConfluenceStrategyV2 (로컬 실험 전용, 배포 안 함)
--------------------------------------------------------
MeanReversionStrategyV2와 같은 아이디어를 추세추종 전략에도 적용:
MACD 골든/데드크로스는 "단기 반전 신호"라 큰 추세와 반대 방향(예: 큰 하락장
중의 일시적 반등)에서도 신호가 뜰 수 있음. 장기 EMA 방향 필터를 추가해서
큰 흐름과 같은 방향의 신호만 받아들이도록 제한.
"""

from MultiConfluenceStrategy import MultiConfluenceStrategy
from pandas import DataFrame
import talib.abstract as ta


class MultiConfluenceStrategyV2(MultiConfluenceStrategy):

    trend_ema_period = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["trend_ema"] = ta.EMA(dataframe, timeperiod=self.trend_ema_period)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        dataframe.loc[dataframe["close"] < dataframe["trend_ema"], "enter_long"] = 0
        dataframe.loc[dataframe["close"] > dataframe["trend_ema"], "enter_short"] = 0
        return dataframe
