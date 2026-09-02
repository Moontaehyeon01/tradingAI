# -*- coding: utf-8 -*-
"""
BoxBreakoutV2Strategy
---------------------
박스권 돌파 + 박스 반대편 손절 + 고정 익절.

BoxBreakoutStrategy(v1)와의 차이:

  v1                                  v2 (이 파일)
  ----------------------------------  ----------------------------------
  손절: 진입가 대비 고정 1.5%          손절: 박스의 반대쪽 경계
                                       (롱 -> 박스 하단, 숏 -> 박스 상단)
  익절: 없음 (48시간 시간청산이 주력)   익절: 레버리지 적용 수익률 20%에 전량
  페어: 고정 4개                       페어: 거래대금 상위 20개 자동 선정

손절을 박스 경계에 두는 이유
  돌파가 진짜라면 가격이 박스 안으로 되돌아오지 않는다는 것이 이 매매의 전제다.
  박스 안으로 되돌아와 반대편 경계까지 갔다면 돌파 판단 자체가 틀린 것이므로,
  그 지점이 자연스러운 손절 자리가 된다. 고정 %와 달리 박스가 좁으면 손절도
  타이트해지고 넓으면 여유가 생겨서, 손절폭이 그 종목의 변동성에 자동으로 맞는다.

  대신 v1보다 손절폭이 넓다. 박스 상단 돌파 직후 진입하면 박스 하단까지는
  박스 폭(최대 3%)만큼 떨어져 있다. v1의 1.5% 대비 약 2배다.

익절 20%의 의미 (단위 주의)
  freqtrade의 minimal_roi 는 '가격 이동폭'이 아니라 '레버리지 적용 후 계좌
  수익률' 단위다. 따라서 minimal_roi = 0.20 은
      레버리지 5배 -> 가격 4% 상승 시 익절
      레버리지 3배 -> 가격 6.7% 상승 시 익절
  즉 레버리지를 바꾸면 실제 익절 가격대도 같이 바뀐다.

시간 청산
  사용자 사양에는 없어서 기본 비활성(max_hold_candles = 0)이다.
  다만 v1에서 수익의 대부분이 48시간 시간청산에서 나왔으므로(632건, 평균
  +10.02%, 승률 87%), 비교 검증을 위해 파라미터로는 남겨두었다.
  0보다 크게 설정하면 그 시간 경과 시 청산한다.
"""

from datetime import datetime, timedelta

import numpy as np
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import (
    DecimalParameter,
    IntParameter,
    IStrategy,
    stoploss_from_absolute,
)


class BoxBreakoutV2Strategy(IStrategy):

    timeframe = "1h"
    startup_candle_count = 120
    can_short = True

    # ------------------------------------------------------------------
    # 박스 정의
    # ------------------------------------------------------------------
    box_period = IntParameter(12, 32, default=24, space="buy")
    box_max_width = DecimalParameter(0.020, 0.050, default=0.030, decimals=3, space="buy")

    # ------------------------------------------------------------------
    # 청산
    # ------------------------------------------------------------------
    # 익절: 레버리지 적용 후 계좌 수익률 20% -> 전량 청산
    minimal_roi = {"0": 0.20}

    # 손절은 custom_stoploss 가 박스 경계로 잡는다.
    # 아래 고정값은 안전망이다. freqtrade는 진입 시 이 값으로 손절선을 먼저 잡고
    # custom_stoploss 는 '더 조이기'만 가능하므로, 박스 경계보다 반드시 느슨해야
    # 한다. 박스 최대 폭 5% + 여유 -> 레버리지 5배 기준 -0.50(가격 10%).
    stoploss = -0.50
    use_custom_stoploss = True

    # 박스 경계 손절이 너무 벌어지지 않도록 하는 상한(가격 기준).
    # 박스 폭이 이보다 크면 이 값으로 자른다.
    max_stop_pct = DecimalParameter(0.020, 0.080, default=0.050, decimals=3, space="sell")

    # 0이면 시간 청산 없음. 0보다 크면 그 시간(봉 수) 뒤 청산.
    # 1시간봉 기준 캔들 수 = 시간. 0이면 시간청산 비활성.
    # 상한을 240(10일)까지 열어둔다 - 5일(120)을 쓰려면 기존 상한 96으로는 안 된다.
    max_hold_candles = IntParameter(0, 240, default=0, space="sell")

    trailing_stop = False
    # custom_exit(시간청산)을 쓰려면 반드시 True 여야 한다.
    # freqtrade는 custom_exit 을 use_exit_signal 안에서만 호출한다.
    use_exit_signal = True
    exit_profit_only = False

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return max(1.0, min(float(self.config.get("leverage", 1)), max_leverage))

    # ------------------------------------------------------------------
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = self.box_period.value
        # shift(1): 현재 봉을 박스 계산에서 제외 -> 룩어헤드 차단.
        # 현재 봉을 넣으면 돌파하는 순간 박스도 같이 넓어져 판정이 순환한다.
        dataframe["box_high"] = dataframe["high"].shift(1).rolling(n).max()
        dataframe["box_low"] = dataframe["low"].shift(1).rolling(n).min()
        dataframe["box_width"] = (
            (dataframe["box_high"] - dataframe["box_low"]) / dataframe["box_low"]
        )
        dataframe["is_box"] = dataframe["box_width"] <= self.box_max_width.value
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            dataframe["is_box"] & (dataframe["close"] > dataframe["box_high"]),
            ["enter_long", "enter_tag"],
        ] = (1, "box_break_up")

        dataframe.loc[
            dataframe["is_box"] & (dataframe["close"] < dataframe["box_low"]),
            ["enter_short", "enter_tag"],
        ] = (1, "box_break_down")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 신호 기반 청산은 쓰지 않는다. 청산은 익절(ROI) / 손절 / (선택)시간청산.
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    # ------------------------------------------------------------------
    def _entry_box(self, pair: str, trade: Trade):
        """진입 시점 봉의 박스 상·하단을 돌려준다. 못 찾으면 (None, None)."""
        df, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if df is None or len(df) == 0:
            return None, None
        rows = df.loc[df["date"] <= trade.open_date_utc]
        row = rows.iloc[-1] if len(rows) else df.iloc[-1]
        hi, lo = row.get("box_high"), row.get("box_low")
        if hi is None or lo is None or np.isnan(hi) or np.isnan(lo):
            return None, None
        return float(hi), float(lo)

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """
        손절가 = 진입 시점 박스의 반대쪽 경계.
          롱  -> 박스 하단
          숏  -> 박스 상단

        freqtrade가 요구하는 반환값은 '현재가 대비 비율'이고 leverage로 나뉘므로,
        절대 가격을 그대로 넘길 수 있는 stoploss_from_absolute() 를 쓴다.
        (직접 계산하면 기준가·레버리지 두 군데서 틀리기 쉽다)
        """
        box_high, box_low = self._entry_box(pair, trade)
        cap = self.max_stop_pct.value
        entry = trade.open_rate

        if box_high is None:
            # 박스를 못 읽으면 상한폭으로 폴백
            stop_price = entry * (1 + cap) if trade.is_short else entry * (1 - cap)
        elif trade.is_short:
            # 숏: 박스 상단. 단 상한폭을 넘지 않게 제한
            stop_price = min(box_high, entry * (1 + cap))
        else:
            # 롱: 박스 하단. 단 상한폭을 넘지 않게 제한
            stop_price = max(box_low, entry * (1 - cap))

        return stoploss_from_absolute(
            stop_price, current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage or 1.0,
        )

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """선택적 시간 청산. max_hold_candles=0 이면 비활성."""
        limit = self.max_hold_candles.value
        if limit and (current_time - trade.open_date_utc) >= timedelta(hours=limit):
            return "max_hold"
        return None
