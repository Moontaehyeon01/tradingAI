# -*- coding: utf-8 -*-
"""
MaRsiAdxStrategy
-----------------
Phase 2~3용 커스텀 전략.
- 이동평균(20/50) 크로스 + RSI 필터 → 기본 신호
- ADX로 추세장/횡보장 구분 → 횡보장에서는 진입 안 함 (Regime Filter)
- ATR 기반 동적 손절 & 트레일링 스탑
- ATR 기반 포지션 사이징 (custom_stake_amount)

사용법:
1. 이 파일을 freqtrade user_data/strategies/ 폴더에 복사
2. config.json에서 "strategy": "MaRsiAdxStrategy" 로 지정
3. 먼저 백테스트로 검증 후 dry_run으로 전환
"""

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade
from pandas import DataFrame
import talib.abstract as ta
import numpy as np
from datetime import datetime


class MaRsiAdxStrategy(IStrategy):

    # ------------------------------------------------------------------
    # 기본 설정
    # ------------------------------------------------------------------
    timeframe = "1h"
    startup_candle_count = 100

    # 기본 stoploss (ATR 기반 커스텀 stoploss를 쓰므로 넉넉하게 잡아둠, 안전망 역할)
    stoploss = -0.15

    # 트레일링 스탑 사용
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.04
    trailing_only_offset_is_reached = True

    use_exit_signal = True
    exit_profit_only = False

    # ------------------------------------------------------------------
    # 하이퍼옵트 대상 파라미터 (walk-forward 최적화 시 이 값들을 탐색)
    # ------------------------------------------------------------------
    ma_fast_period = IntParameter(10, 30, default=20, space="buy")
    ma_slow_period = IntParameter(40, 70, default=50, space="buy")
    rsi_period = IntParameter(10, 20, default=14, space="buy")
    rsi_buy_threshold = IntParameter(45, 60, default=50, space="buy")
    rsi_sell_threshold = IntParameter(65, 80, default=70, space="sell")
    adx_period = IntParameter(10, 20, default=14, space="buy")
    adx_threshold = IntParameter(20, 30, default=25, space="buy")  # 이 이상이면 "추세장"으로 판단
    atr_period = IntParameter(10, 20, default=14, space="buy")
    atr_stoploss_multiplier = DecimalParameter(1.5, 4.0, default=2.5, space="sell")

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # 이동평균
        dataframe["ma_fast"] = ta.SMA(dataframe, timeperiod=self.ma_fast_period.value)
        dataframe["ma_slow"] = ta.SMA(dataframe, timeperiod=self.ma_slow_period.value)

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)

        # ADX (추세 강도 - 국면 판단용)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=self.adx_period.value)

        # ATR (변동성 - 손절/사이징용)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_period.value)

        return dataframe

    # ------------------------------------------------------------------
    # 진입 조건
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe.loc[
            (
                # MA 골든크로스 (직전 캔들엔 fast < slow, 현재는 fast > slow)
                (dataframe["ma_fast"] > dataframe["ma_slow"])
                & (dataframe["ma_fast"].shift(1) <= dataframe["ma_slow"].shift(1))

                # RSI 필터: 과매수 구간 아님 + 최소 모멘텀 확인
                & (dataframe["rsi"] > self.rsi_buy_threshold.value)
                & (dataframe["rsi"] < self.rsi_sell_threshold.value)

                # ADX 필터: 추세장일 때만 진입 (횡보장 필터링)
                & (dataframe["adx"] > self.adx_threshold.value)

                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # 청산 조건
    # ------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe.loc[
            (
                (dataframe["ma_fast"] < dataframe["ma_slow"])
                & (dataframe["ma_fast"].shift(1) >= dataframe["ma_slow"].shift(1))
            )
            | (dataframe["rsi"] > self.rsi_sell_threshold.value),
            "exit_long",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # ATR 기반 동적 손절
    # 진입가 대비 ATR * multiplier 만큼 아래를 손절선으로 설정
    # ------------------------------------------------------------------
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> float:

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return self.stoploss

        last_candle = dataframe.iloc[-1]
        atr = last_candle["atr"]

        if atr == 0 or np.isnan(atr):
            return self.stoploss

        # ATR 기반 손절폭을 현재가 대비 비율(음수)로 변환
        atr_stop_distance = (atr * self.atr_stoploss_multiplier.value) / current_rate

        # 기본 stoploss보다 타이트한 쪽(더 안전한 쪽)을 선택
        return max(-atr_stop_distance, self.stoploss)

    # ------------------------------------------------------------------
    # ATR 기반 포지션 사이징
    # 변동성이 클수록(ATR 큼) 포지션 축소, 변동성 작을수록 확대
    # ------------------------------------------------------------------
    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float,
        max_stake: float,
        leverage: float,
        entry_tag: str,
        side: str,
        **kwargs,
    ) -> float:

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return proposed_stake

        last_candle = dataframe.iloc[-1]
        atr = last_candle["atr"]

        if atr == 0 or np.isnan(atr) or current_rate == 0:
            return proposed_stake

        # 변동성 비율 (ATR / 현재가) — 값이 클수록 변동성 큰 구간
        volatility_ratio = atr / current_rate

        # 기준 변동성(예: 2%) 대비 현재 변동성에 반비례해서 사이즈 조정
        base_volatility = 0.02
        adjustment = base_volatility / max(volatility_ratio, 0.001)
        adjustment = min(max(adjustment, 0.3), 1.5)  # 0.3배~1.5배 범위로 제한

        adjusted_stake = proposed_stake * adjustment

        return max(min(adjusted_stake, max_stake), min_stake)
