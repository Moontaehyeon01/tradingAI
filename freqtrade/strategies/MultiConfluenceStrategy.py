# -*- coding: utf-8 -*-
"""
MultiConfluenceStrategy
------------------------
MaRsiAdxStrategy의 업그레이드 버전.

롱 진입 조건 (모두 동시 충족 시에만 진입 = 컨플루언스):
  1. 추세 지표: MACD 골든크로스 (MACD line이 Signal line 상향 돌파)
  2. 모멘텀 지표: Stochastic %K가 50 상향 돌파 (과매도 탈출 + 상승 모멘텀)
  3. 변동성 지표: 종가가 볼린저밴드 중심선(SMA20) 위에 위치
  4. 국면 필터: ADX > threshold (추세장에서만 진입, 횡보장 제외)

숏 진입 조건 (롱 조건을 그대로 대칭 반전):
  1. MACD 데드크로스
  2. Stochastic %K가 50 하향 돌파
  3. 종가가 볼린저밴드 중심선 아래
  4. ADX > threshold (추세장에서만)

청산 조건:
  - 롱: MACD 데드크로스 OR 종가가 볼린저밴드 중심선 아래로 하락 OR Stochastic 과매수 이탈
  - 숏: MACD 골든크로스 OR 종가가 볼린저밴드 중심선 위로 상승 OR Stochastic 과매도 이탈

리스크 관리:
  - ATR 기반 동적 손절 (custom_stoploss, 롱/숏 공통 — freqtrade가 방향에 맞춰 해석)
  - ATR 기반 포지션 사이징 (custom_stake_amount)
  - 레버리지: config의 "leverage" 값을 사용 (기본 1배 = 레버리지 없음). 선물(futures)
    config에서만 의미가 있으며, spot config에서는 leverage() 자체가 호출되지 않음.

사용법:
1. user_data/strategies/ 에 복사
2. config에서 "strategy": "MultiConfluenceStrategy" 로 지정
3. 반드시 백테스트 → hyperopt(walk-forward) → dry_run 순서로 검증
4. 선물(숏 포지션 포함)로 쓰려면 config에 "trading_mode": "futures",
   "margin_mode": "isolated" 를 설정한 futures 전용 config를 사용할 것
   (freqtrade/config_trend_futures.json 참고)
"""

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade
from pandas import DataFrame
import talib.abstract as ta
import numpy as np
from datetime import datetime


class MultiConfluenceStrategy(IStrategy):

    timeframe = "1h"
    startup_candle_count = 220

    # 장기 추세(EMA100) 방향 필터: 큰 흐름과 반대 방향인 진입은 차단
    # (5년 워크포워드 검증 결과 57/57 구간 전부 플러스로 확인된 개선 사항 — 자세한 배경은
    # 이전 로컬 실험 MultiConfluenceStrategyV2 참고. 검증 끝나서 본 파일에 병합함)
    trend_ema_period = 100

    stoploss = -0.15  # ATR 커스텀 stoploss의 안전망(하한선) 역할

    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.04
    trailing_only_offset_is_reached = True

    use_exit_signal = True
    exit_profit_only = False

    # ------------------------------------------------------------------
    # 하이퍼옵트 대상 파라미터
    # ------------------------------------------------------------------
    macd_fast = IntParameter(8, 16, default=12, space="buy")
    macd_slow = IntParameter(20, 30, default=26, space="buy")
    macd_signal = IntParameter(7, 12, default=9, space="buy")

    stoch_k_period = IntParameter(10, 20, default=14, space="buy")
    stoch_d_period = IntParameter(3, 6, default=3, space="buy")
    stoch_threshold = IntParameter(45, 55, default=50, space="buy")
    stoch_overbought = IntParameter(75, 90, default=80, space="sell")  # 롱 청산용
    stoch_oversold = IntParameter(10, 25, default=20, space="sell")  # 숏 청산용 (stoch_overbought 대칭)

    bb_period = IntParameter(15, 25, default=20, space="buy")
    bb_std = DecimalParameter(1.5, 2.5, default=2.0, space="buy")

    adx_period = IntParameter(10, 20, default=14, space="buy")
    adx_threshold = IntParameter(20, 30, default=25, space="buy")

    atr_period = IntParameter(10, 20, default=14, space="buy")
    atr_stoploss_multiplier = DecimalParameter(1.5, 4.0, default=2.5, space="sell")

    # ------------------------------------------------------------------
    # XRP 전용 하이퍼옵트 파라미터
    # ------------------------------------------------------------------
    # 이 전략의 기본 파라미터(위)는 BTC/ETH/SOL 위주로 최적화되면 XRP에는
    # 신호가 거의/전혀 안 나옴. 그렇다고 XRP만 따로 봇 인스턴스를 두면 계좌
    # 잔고를 봇끼리 못 나눠 쓰게 되므로, 같은 프로세스 안에서 페어가 XRP일 때만
    # 아래 xrp_* 파라미터를 쓰도록 분기함. 탐색 범위는 위 기본 파라미터와 동일하게
    # 맞춰서, hyperopt 한 번 돌리면 두 세트가 동시에(같은 목적함수로) 튜닝됨.
    XRP_PAIR = "XRP/USDT:USDT"

    xrp_macd_fast = IntParameter(8, 16, default=14, space="buy")
    xrp_macd_slow = IntParameter(20, 30, default=29, space="buy")
    xrp_macd_signal = IntParameter(7, 12, default=7, space="buy")

    xrp_stoch_k_period = IntParameter(10, 20, default=19, space="buy")
    xrp_stoch_d_period = IntParameter(3, 6, default=3, space="buy")
    xrp_stoch_threshold = IntParameter(45, 55, default=54, space="buy")
    xrp_stoch_overbought = IntParameter(75, 90, default=77, space="sell")
    xrp_stoch_oversold = IntParameter(10, 25, default=10, space="sell")

    xrp_bb_period = IntParameter(15, 25, default=18, space="buy")
    xrp_bb_std = DecimalParameter(1.5, 2.5, default=1.911, space="buy")

    xrp_adx_period = IntParameter(10, 20, default=11, space="buy")
    xrp_adx_threshold = IntParameter(20, 30, default=30, space="buy")

    xrp_atr_period = IntParameter(10, 20, default=19, space="buy")
    xrp_atr_stoploss_multiplier = DecimalParameter(1.5, 4.0, default=3.519, space="sell")

    def _p(self, name: str, pair: str):
        """파라미터 값을 페어에 맞게 반환 (XRP면 xrp_<name> 하이퍼옵트 파라미터, 아니면 기본값)"""
        if pair == self.XRP_PAIR:
            return getattr(self, f"xrp_{name}").value
        return getattr(self, name).value

    # spot config에서 can_short=True인 채로 로드하면 freqtrade가 아예 에러를 내며 거부함
    # ("Short strategies cannot run in spot markets") -> trading_mode를 보고 동적으로 결정
    @property
    def can_short(self) -> bool:
        return self.config.get("trading_mode") == "futures"

    # ------------------------------------------------------------------
    # 지표 계산
    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # MACD
        macd = ta.MACD(
            dataframe,
            fastperiod=self._p("macd_fast", pair),
            slowperiod=self._p("macd_slow", pair),
            signalperiod=self._p("macd_signal", pair),
        )
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]

        # Stochastic
        stoch = ta.STOCH(
            dataframe,
            fastk_period=self._p("stoch_k_period", pair),
            slowk_period=self._p("stoch_d_period", pair),
            slowd_period=self._p("stoch_d_period", pair),
        )
        dataframe["slowk"] = stoch["slowk"]
        dataframe["slowd"] = stoch["slowd"]

        # Bollinger Bands
        bollinger = ta.BBANDS(
            dataframe, timeperiod=self._p("bb_period", pair), nbdevup=self._p("bb_std", pair), nbdevdn=self._p("bb_std", pair)
        )
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_mid"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]

        # ADX (국면 필터)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=self._p("adx_period", pair))

        # ATR (리스크 관리용)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self._p("atr_period", pair))

        # 장기 추세 방향 필터용 EMA
        dataframe["trend_ema"] = ta.EMA(dataframe, timeperiod=self.trend_ema_period)

        return dataframe

    # ------------------------------------------------------------------
    # 진입 조건 (컨플루언스: 4개 조건 동시 충족, 롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        stoch_threshold = self._p("stoch_threshold", pair)
        adx_threshold = self._p("adx_threshold", pair)

        dataframe.loc[
            (
                # 1) MACD 골든크로스
                (dataframe["macd"] > dataframe["macdsignal"])
                & (dataframe["macd"].shift(1) <= dataframe["macdsignal"].shift(1))

                # 2) Stochastic 상승 모멘텀 (50 상향 돌파)
                & (dataframe["slowk"] > stoch_threshold)
                & (dataframe["slowk"].shift(1) <= stoch_threshold)

                # 3) 볼린저밴드 중심선 위 (상승 추세 구간)
                & (dataframe["close"] > dataframe["bb_mid"])

                # 4) 국면 필터: 추세장에서만
                & (dataframe["adx"] > adx_threshold)

                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # can_short=False(spot config)일 때는 freqtrade가 enter_short 컬럼을 무시함
        dataframe.loc[
            (
                # 1) MACD 데드크로스
                (dataframe["macd"] < dataframe["macdsignal"])
                & (dataframe["macd"].shift(1) >= dataframe["macdsignal"].shift(1))

                # 2) Stochastic 하락 모멘텀 (50 하향 돌파)
                & (dataframe["slowk"] < stoch_threshold)
                & (dataframe["slowk"].shift(1) >= stoch_threshold)

                # 3) 볼린저밴드 중심선 아래 (하락 추세 구간)
                & (dataframe["close"] < dataframe["bb_mid"])

                # 4) 국면 필터: 추세장에서만
                & (dataframe["adx"] > adx_threshold)

                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        # 장기 추세와 반대 방향인 진입은 취소 (하락추세에서 롱 금지, 상승추세에서 숏 금지)
        dataframe.loc[dataframe["close"] < dataframe["trend_ema"], "enter_long"] = 0
        dataframe.loc[dataframe["close"] > dataframe["trend_ema"], "enter_short"] = 0

        return dataframe

    # ------------------------------------------------------------------
    # 청산 조건 (롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        stoch_overbought = self._p("stoch_overbought", pair)
        stoch_oversold = self._p("stoch_oversold", pair)

        dataframe.loc[
            (
                # MACD 데드크로스
                (dataframe["macd"] < dataframe["macdsignal"])
                & (dataframe["macd"].shift(1) >= dataframe["macdsignal"].shift(1))
            )
            # 중심선 이탈
            | (dataframe["close"] < dataframe["bb_mid"])
            # 과매수 구간 이탈 (Stochastic 하락 전환)
            | (
                (dataframe["slowk"] < dataframe["slowd"])
                & (dataframe["slowk"].shift(1) >= stoch_overbought)
            ),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                # MACD 골든크로스
                (dataframe["macd"] > dataframe["macdsignal"])
                & (dataframe["macd"].shift(1) <= dataframe["macdsignal"].shift(1))
            )
            # 중심선 회복
            | (dataframe["close"] > dataframe["bb_mid"])
            # 과매도 구간 이탈 (Stochastic 상승 전환)
            | (
                (dataframe["slowk"] > dataframe["slowd"])
                & (dataframe["slowk"].shift(1) <= stoch_oversold)
            ),
            "exit_short",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # 레버리지 (선물 config에서만 의미 있음, spot에서는 호출 안 됨)
    # config에 "leverage" 키가 없으면 기본 1배(레버리지 없음)
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
    # ATR 기반 동적 손절 (MaRsiAdxStrategy와 동일 로직)
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

        atr_stop_distance = (atr * self._p("atr_stoploss_multiplier", pair)) / current_rate
        return max(-atr_stop_distance, self.stoploss)

    # ------------------------------------------------------------------
    # ATR 기반 포지션 사이징 (MaRsiAdxStrategy와 동일 로직)
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
        adjustment = min(max(adjustment, 0.3), 1.5)

        adjusted_stake = proposed_stake * adjustment
        return max(min(adjusted_stake, max_stake), min_stake)
