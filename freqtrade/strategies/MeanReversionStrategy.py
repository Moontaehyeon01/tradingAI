# -*- coding: utf-8 -*-
"""
MeanReversionStrategy
-----------------------
포트폴리오용 2번째 전략 (추세추종형 MultiConfluenceStrategy와 상관관계 낮음).

MultiConfluenceStrategy는 추세장(ADX 높음)에서만 진입하는 반면,
이 전략은 반대로 횡보장(ADX 낮음)에서 볼린저밴드 하단 터치 + RSI 과매도
반등을 노리는 평균회귀 전략입니다.

두 전략을 동시에 소액씩 운용하면:
  - 추세장 → MultiConfluenceStrategy가 수익 담당
  - 횡보장 → MeanReversionStrategy가 수익 담당
  - 전체 포트폴리오의 국면별 변동성을 낮추는 효과

롱 진입 조건:
  1. ADX < threshold (횡보장 확인 - 추세장 아님)
  2. 종가가 볼린저밴드 하단 터치 또는 이탈 (과매도 구간)
  3. RSI < oversold 임계값

  (※ 원래는 "RSI가 직전 대비 상승 전환"하는 정확히 그 캔들만 허용했으나,
   walk-forward 검증 중 이 조건이 너무 좁아서 2023H2~2024 상승장 다수 구간에서
   거래가 0건이었음. RSI가 과매도 구간에 있기만 하면 진입하도록 완화함 —
   포지션은 한 개만 열리므로(freqtrade가 페어당 중복 진입 방지) 매 캔들 재진입
   위험은 없음.)

숏 진입 조건 (롱 조건을 대칭 반전):
  1. ADX < threshold (횡보장)
  2. 종가가 볼린저밴드 상단 터치/이탈 (과매수 구간)
  3. RSI > overbought 임계값

청산 조건:
  - 롱: 종가가 볼린저밴드 중심선 도달 OR RSI가 rsi_exit 이상
  - 숏: 종가가 볼린저밴드 중심선 도달 OR RSI가 rsi_exit_short 이하

리스크 관리:
  - ATR 기반 동적 손절 (추세추종 전략과 동일 로직)
  - 평균회귀는 추세 반전 시 손실이 커질 수 있으므로 손절폭을 더 타이트하게 설정
  - 레버리지: config의 "leverage" 값 사용 (기본 1배). 선물 config에서만 의미 있음.

사용법:
1. user_data/strategies/ 에 복사
2. 별도 config (예: config_meanreversion.json) 에서 strategy 지정 후
   MultiConfluenceStrategy와 각각 별도 봇 인스턴스로 동시 구동
   (자금은 각 전략에 나눠서 배분 - 예: 전체 자금의 50%씩)
3. 선물(숏 포함)로 쓰려면 freqtrade/config_meanreversion_futures.json 사용
"""

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade
from pandas import DataFrame
import talib.abstract as ta
import numpy as np
from datetime import datetime


class MeanReversionStrategy(IStrategy):

    timeframe = "1h"
    startup_candle_count = 100

    stoploss = -0.10  # 추세추종 전략보다 타이트하게 (평균회귀는 역추세 베팅이라 더 보수적으로)

    trailing_stop = False  # 평균회귀는 목표가(중심선) 도달 시 바로 청산하는 게 기본 로직

    use_exit_signal = True
    exit_profit_only = False

    # ------------------------------------------------------------------
    # 하이퍼옵트 대상 파라미터
    # ------------------------------------------------------------------
    bb_period = IntParameter(15, 25, default=20, space="buy")
    # 하단 1.2까지 낮춰서 밴드가 좁아질 여지를 줌 -> 하단 터치 빈도 증가
    # (walk-forward 검증 중 2023H2~2024 상승장에서 원래 범위(1.8~3.0)로는
    #  90일 구간 다수에서 거래가 0건이라 하이퍼옵트가 아예 결과를 못 찾는 문제 확인)
    bb_std = DecimalParameter(1.2, 3.0, default=2.2, space="buy")

    rsi_period = IntParameter(10, 20, default=14, space="buy")
    # 과매도 기준을 45까지 완화 (기존 25~40) -> 진입 조건 충족 빈도 증가
    rsi_oversold = IntParameter(25, 45, default=35, space="buy")
    rsi_exit = IntParameter(55, 70, default=60, space="sell")  # 롱 청산용

    rsi_overbought = IntParameter(55, 75, default=65, space="buy")  # 숏 진입용 (rsi_oversold 대칭)
    rsi_exit_short = IntParameter(30, 45, default=40, space="sell")  # 숏 청산용 (rsi_exit 대칭)

    adx_period = IntParameter(10, 20, default=14, space="buy")
    # 횡보장 판정 기준을 35까지 완화 (기존 18~28) -> 약한 추세장도 포함해 진입 기회 확대
    adx_regime_threshold = IntParameter(18, 35, default=22, space="buy")  # 이 이하면 "횡보장"

    atr_period = IntParameter(10, 20, default=14, space="buy")
    atr_stoploss_multiplier = DecimalParameter(1.2, 3.0, default=1.8, space="sell")

    # spot config에서 can_short=True인 채로 로드하면 freqtrade가 아예 에러를 내며 거부함
    # ("Short strategies cannot run in spot markets") -> trading_mode를 보고 동적으로 결정
    @property
    def can_short(self) -> bool:
        return self.config.get("trading_mode") == "futures"

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        bollinger = ta.BBANDS(
            dataframe, timeperiod=self.bb_period.value, nbdevup=self.bb_std.value, nbdevdn=self.bb_std.value
        )
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_mid"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]

        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=self.adx_period.value)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_period.value)

        return dataframe

    # ------------------------------------------------------------------
    # 진입 조건 (횡보장 + 과매도/과매수 반등, 롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe.loc[
            (
                # 1) 국면 필터: 횡보장에서만 (추세장에서는 진입 안 함)
                (dataframe["adx"] < self.adx_regime_threshold.value)

                # 2) 볼린저밴드 하단 터치/이탈
                & (dataframe["close"] <= dataframe["bb_lower"])

                # 3) RSI 과매도 구간 (정확한 "상승 전환 캔들"까지는 요구하지 않음 —
                #    페어당 중복 진입은 freqtrade가 막아주므로 매 캔들 재진입 걱정 없음)
                & (dataframe["rsi"] < self.rsi_oversold.value)

                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # can_short=False(spot config)일 때는 freqtrade가 enter_short 컬럼을 무시함
        dataframe.loc[
            (
                (dataframe["adx"] < self.adx_regime_threshold.value)
                & (dataframe["close"] >= dataframe["bb_upper"])
                & (dataframe["rsi"] > self.rsi_overbought.value)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # 청산 조건 (중심선 회귀 완료 또는 RSI 충분히 회복, 롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe.loc[
            (dataframe["close"] >= dataframe["bb_mid"])
            | (dataframe["rsi"] > self.rsi_exit.value),
            "exit_long",
        ] = 1

        dataframe.loc[
            (dataframe["close"] <= dataframe["bb_mid"])
            | (dataframe["rsi"] < self.rsi_exit_short.value),
            "exit_short",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # 레버리지 (선물 config에서만 의미 있음, spot에서는 호출 안 됨)
    # ------------------------------------------------------------------
    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str,
        side: str,
        **kwargs,
    ) -> float:
        desired_leverage = self.config.get("leverage", 1)
        return max(1.0, min(float(desired_leverage), max_leverage))

    # ------------------------------------------------------------------
    # ATR 기반 동적 손절 (타이트한 버전)
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

        atr_stop_distance = (atr * self.atr_stoploss_multiplier.value) / current_rate
        return max(-atr_stop_distance, self.stoploss)

    # ------------------------------------------------------------------
    # 포지션 사이징: 평균회귀는 역추세 베팅이라 기본적으로 추세추종보다 보수적으로
    # (변동성 클수록 더 크게 축소)
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

        volatility_ratio = atr / current_rate
        base_volatility = 0.02
        adjustment = base_volatility / max(volatility_ratio, 0.001)
        # 평균회귀는 추세추종보다 보수적 범위 (0.2배~1.2배)
        adjustment = min(max(adjustment, 0.2), 1.2)

        adjusted_stake = proposed_stake * adjustment
        return max(min(adjusted_stake, max_stake), min_stake)
