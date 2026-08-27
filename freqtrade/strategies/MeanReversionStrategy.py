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

from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    DecimalParameter,
    stoploss_from_absolute,
)
from freqtrade.persistence import Trade
from pandas import DataFrame
import talib.abstract as ta
import numpy as np
from datetime import datetime


class MeanReversionStrategy(IStrategy):

    timeframe = "1h"
    startup_candle_count = 220

    # 장기 추세(EMA100) 방향 필터 + 1% 여유폭: 큰 흐름과 확실히 반대인 진입만 차단
    # (5년 워크포워드 검증: 52/57 구간 플러스, 마이너스 0개. 기존 MeanReversionStrategy가
    # 진짜 하락추세에서도 "횡보장"으로 오판해 눌림목을 계속 사서 손절을 반복하던 문제를
    # 해결하기 위해 추가함 — 자세한 배경은 이전 로컬 실험 MeanReversionStrategyV3 참고)
    trend_ema_period = 100
    trend_ema_buffer_pct = 0.01

    # ------------------------------------------------------------------
    # 손절 설정 — 단위 주의!
    #
    # freqtrade의 stoploss 값은 "가격 이동폭"이 아니라 "레버리지 적용 후 계좌
    # 손실률" 단위다 (adjust_stop_loss: new_loss = price * (1 - |stoploss/leverage|)).
    # config의 leverage=5 기준이므로  stoploss=-0.35  ->  가격 7% 이동.
    #
    # 또한 freqtrade는 진입 시점에 이 고정값으로 손절선을 먼저 잡고, 그 뒤
    # custom_stoploss는 "더 타이트하게"만 조일 수 있다(느슨하게는 못 함).
    # 따라서 이 값은 custom_stoploss가 쓸 수 있는 최대폭(STOP_MAX_PCT)보다
    # 반드시 느슨해야 한다. 그렇지 않으면 ATR 손절이 통째로 무력화된다.
    #   -> STOP_MAX_PCT(12%) * 5배 = 0.60  이므로 여유를 둬서 -0.70
    # ------------------------------------------------------------------
    stoploss = -0.70

    use_custom_stoploss = True

    # custom_stoploss가 실제로 쓰는 손절폭 (전부 "가격 이동폭" 기준, 레버리지 무관)
    STOP_MIN_PCT = 0.015   # 너무 타이트하면 노이즈에 털림
    STOP_MAX_PCT = 0.12    # ATR이 아무리 커도 이 이상은 안 벌림
    STOP_DEFAULT_PCT = 0.04  # ATR을 못 구했을 때 폴백

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
    # 탐색 범위 대폭 확대: 이 파라미터는 use_custom_stoploss=False였던 탓에
    # 지금까지 하이퍼옵트가 아무리 돌아도 결과에 전혀 영향을 못 준 "죽은 파라미터"였음.
    # 이제 실제로 작동하므로 처음부터 넓게 다시 탐색해야 함.
    atr_stoploss_multiplier = DecimalParameter(1.5, 8.0, default=3.5, space="sell")

    # ------------------------------------------------------------------
    # XRP 전용 하이퍼옵트 파라미터 (MultiConfluenceStrategy와 동일한 이유:
    # BTC/ETH/SOL 위주로 최적화된 파라미터는 XRP에 신호가 거의 안 나와서,
    # 같은 프로세스 안에서 페어가 XRP일 때만 별도 파라미터로 분기함.
    # 탐색 범위는 위 기본 파라미터와 동일 -> hyperopt 한 번으로 두 세트가
    # 같은 목적함수 기준으로 동시에 튜닝됨)
    # ------------------------------------------------------------------
    XRP_PAIR = "XRP/USDT:USDT"

    xrp_bb_period = IntParameter(15, 25, default=21, space="buy")
    xrp_bb_std = DecimalParameter(1.2, 3.0, default=1.565, space="buy")

    xrp_rsi_period = IntParameter(10, 20, default=17, space="buy")
    xrp_rsi_oversold = IntParameter(25, 45, default=35, space="buy")
    xrp_rsi_exit = IntParameter(55, 70, default=70, space="sell")

    xrp_rsi_overbought = IntParameter(55, 75, default=67, space="buy")
    xrp_rsi_exit_short = IntParameter(30, 45, default=38, space="sell")

    xrp_adx_period = IntParameter(10, 20, default=15, space="buy")
    xrp_adx_regime_threshold = IntParameter(18, 35, default=18, space="buy")

    xrp_atr_period = IntParameter(10, 20, default=20, space="buy")
    xrp_atr_stoploss_multiplier = DecimalParameter(1.5, 8.0, default=3.5, space="sell")

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

        bollinger = ta.BBANDS(
            dataframe, timeperiod=self._p("bb_period", pair), nbdevup=self._p("bb_std", pair), nbdevdn=self._p("bb_std", pair)
        )
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_mid"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]

        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self._p("rsi_period", pair))
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=self._p("adx_period", pair))
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self._p("atr_period", pair))

        # 장기 추세 방향 필터용 EMA
        dataframe["trend_ema"] = ta.EMA(dataframe, timeperiod=self.trend_ema_period)

        return dataframe

    # ------------------------------------------------------------------
    # 진입 조건 (횡보장 + 과매도/과매수 반등, 롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        adx_regime_threshold = self._p("adx_regime_threshold", pair)
        rsi_oversold = self._p("rsi_oversold", pair)
        rsi_overbought = self._p("rsi_overbought", pair)

        dataframe.loc[
            (
                # 1) 국면 필터: 횡보장에서만 (추세장에서는 진입 안 함)
                (dataframe["adx"] < adx_regime_threshold)

                # 2) 볼린저밴드 하단 터치/이탈
                & (dataframe["close"] <= dataframe["bb_lower"])

                # 3) RSI 과매도 구간 (정확한 "상승 전환 캔들"까지는 요구하지 않음 —
                #    페어당 중복 진입은 freqtrade가 막아주므로 매 캔들 재진입 걱정 없음)
                & (dataframe["rsi"] < rsi_oversold)

                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # can_short=False(spot config)일 때는 freqtrade가 enter_short 컬럼을 무시함
        dataframe.loc[
            (
                (dataframe["adx"] < adx_regime_threshold)
                & (dataframe["close"] >= dataframe["bb_upper"])
                & (dataframe["rsi"] > rsi_overbought)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        # 장기 추세와 확실히 반대 방향인 진입만 차단 (1% 여유폭 — EMA를 살짝 밑돌아도 롱 허용)
        long_ok = dataframe["close"] >= dataframe["trend_ema"] * (1 - self.trend_ema_buffer_pct)
        short_ok = dataframe["close"] <= dataframe["trend_ema"] * (1 + self.trend_ema_buffer_pct)
        dataframe.loc[~long_ok, "enter_long"] = 0
        dataframe.loc[~short_ok, "enter_short"] = 0

        return dataframe

    # ------------------------------------------------------------------
    # 청산 조건 (중심선 회귀 완료 또는 RSI 충분히 회복, 롱/숏 대칭)
    # ------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        dataframe.loc[
            (dataframe["close"] >= dataframe["bb_mid"])
            | (dataframe["rsi"] > self._p("rsi_exit", pair)),
            "exit_long",
        ] = 1

        dataframe.loc[
            (dataframe["close"] <= dataframe["bb_mid"])
            | (dataframe["rsi"] < self._p("rsi_exit_short", pair)),
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
    # ATR 기반 동적 손절
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
        """
        진입 시점 ATR로 "절대 손절가"를 고정하고, 그걸 freqtrade가 요구하는
        "현재가 대비 비율"로 변환해서 반환한다.

        ※ freqtrade 내부 동작 (persistence/trade_model.py: adjust_stop_loss)
             new_loss = current_price * (1 - |반환값 / leverage|)

           여기서 두 가지가 직관과 다르다:
           (1) 기준가가 '진입가'가 아니라 '현재가'다 (백테스트에서는 캔들 high).
               -> 상수를 그대로 반환하면 의도치 않게 '트레일링 스탑'이 된다.
                  이전 버전이 승률 20~40%, 보유시간 1~6시간으로 폭락했던 진짜 원인.
           (2) 반환값이 leverage로 나뉜다.
               -> '가격 이동폭'이 아니라 '레버리지 적용 후 계좌 손실률' 단위다.

           이 두 변환을 정확히 처리해주는 게 freqtrade 기본 제공 헬퍼
           stoploss_from_absolute() 이므로, 직접 계산하지 말고 이걸 쓴다.
        """
        # --- 진입 시점 캔들의 ATR로 손절폭 고정 ---
        # (매 캔들 최신 ATR로 재계산하면 진입 후 변동성이 급등했을 때 손절폭이
        #  같이 넓어져서, 정작 보호가 필요한 순간에 더 큰 손실을 허용하게 됨)
        stop_distance_pct = self.STOP_DEFAULT_PCT

        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if dataframe is not None and len(dataframe) > 0 and trade.open_rate:
            entry_candles = dataframe.loc[dataframe["date"] <= trade.open_date_utc]
            row = entry_candles.iloc[-1] if len(entry_candles) else dataframe.iloc[-1]
            atr = row["atr"]
            if atr and not np.isnan(atr):
                stop_distance_pct = (
                    atr * self._p("atr_stoploss_multiplier", pair)
                ) / trade.open_rate

        # 가격 기준(레버리지 무관) 손절폭을 상/하한으로 제한
        stop_distance_pct = min(max(stop_distance_pct, self.STOP_MIN_PCT), self.STOP_MAX_PCT)

        if trade.is_short:
            stop_price = trade.open_rate * (1 + stop_distance_pct)
        else:
            stop_price = trade.open_rate * (1 - stop_distance_pct)

        return stoploss_from_absolute(
            stop_price,
            current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage or 1.0,
        )

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
