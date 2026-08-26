# -*- coding: utf-8 -*-
"""
MeanReversionStrategyV3 (로컬 실험 전용, 배포 안 함)
-----------------------------------------------------
V2(EMA 이분법 필터)를 두 가지 방향으로 개선해볼 수 있는지 테스트하기 위한 버전.
  - ema_buffer_pct: EMA 컷오프에 여유폭을 줌 (0.01 = 종가가 EMA보다 1% 낮아도 롱 허용)
  - require_slope: EMA 자체가 방향성 있게 움직이고 있는지 추가 확인
    (롱은 EMA가 slope_lookback 캔들 전보다 올라있어야, 숏은 내려있어야)
둘 다 기본은 꺼져 있고(V2와 동일 동작), 클래스 속성으로 켜서 비교 테스트함.
"""

from MeanReversionStrategy import MeanReversionStrategy
from pandas import DataFrame
import talib.abstract as ta


class MeanReversionStrategyV3(MeanReversionStrategy):

    startup_candle_count = 220
    trend_ema_period = 100

    ema_buffer_pct = 0.01       # 0.0 = V2와 동일(여유폭 없음), 0.01 = 1% 여유
    require_slope = False      # True면 EMA 기울기 방향까지 확인
    slope_lookback = 10        # 몇 캔들 전과 비교해서 기울기를 볼지

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        dataframe["trend_ema"] = ta.EMA(dataframe, timeperiod=self.trend_ema_period)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)

        long_ok = dataframe["close"] >= dataframe["trend_ema"] * (1 - self.ema_buffer_pct)
        short_ok = dataframe["close"] <= dataframe["trend_ema"] * (1 + self.ema_buffer_pct)

        if self.require_slope:
            ema_rising = dataframe["trend_ema"] > dataframe["trend_ema"].shift(self.slope_lookback)
            ema_falling = dataframe["trend_ema"] < dataframe["trend_ema"].shift(self.slope_lookback)
            long_ok = long_ok & ema_rising
            short_ok = short_ok & ema_falling

        dataframe.loc[~long_ok, "enter_long"] = 0
        dataframe.loc[~short_ok, "enter_short"] = 0
        return dataframe
