# -*- coding: utf-8 -*-
"""
BoxBreakoutStrategy
-------------------
박스권(횡보 구간) 돌파 추종 전략.

설계 근거 (2026-08-27 분석, 1시간봉 BTC/ETH/SOL/XRP 2020-10 ~ 2026-08):
  - 직전 24봉의 고가/저가로 박스를 정의하고, 박스 폭이 3% 이하일 때만
    '횡보 중'으로 인정한다. 종가가 박스를 벗어나면 그 방향으로 진입.
  - 신호는 전부 '과거 봉'만으로 만들어진다. (프랙탈/다이버전스/추세선과 달리
    스윙포인트 확정 지연으로 인한 룩어헤드가 구조적으로 없음)

  - 대조군 검정 통과: 같은 익절/손절 브라켓을 '무작위 진입'에 걸면 -0.063%,
    박스 돌파에 걸면 +0.269% (차이 p=0.001, 1338건). 브라켓 구조가 아니라
    돌파 신호 자체가 수익에 기여함을 확인.

  - 실제 수익 구조는 '익절'이 아니라 '손절 + 시간청산'이다.
    freqtrade 백테스트(레버리지 3배, 2020-10~2026-08, 1725건):
      max_hold(2일 시간청산)  632건  평균 +10.02%  승률 87.0%   +11,327 USDT
      손절(가격 1.5%)       1093건  평균  -4.83%  승률  0.0%    -9,400 USDT
    즉 "짧게 자르고, 이긴 건 이틀간 달리게 둔다"가 핵심이다.
    고정 익절선은 도달률이 1.1%뿐이라 실질적으로 무의미했다.

검증 요약:
  - 대조군: 무작위 진입 -0.063% vs 박스돌파 +0.269% (차이 p=0.001)
  - 파라미터 견고성(N x 폭 20조합): IS 15/20 플러스, OOS 20/20 플러스
  - 페어별: BTC/ETH/SOL/XRP 4개 전부 플러스
  - 레버리지 단조성: 1배 +49% / 2배 +114% / 3배 +193% / 5배 +359%
  - 연도별: 4/6 플러스 (2023 -18.3%, 2024 -1.2%가 손실 연도)
  - Profit factor 1.20, Sharpe 0.89

한계 (반드시 인지할 것):
  - OOS(2024-26)가 IS(2020-24)보다 일관되게 훨씬 좋다. 최근 구간에 수익이
    쏠려 있으며, 앞으로의 성과는 OOS보다 IS(3배 기준 연 4.5%)에 가까울 수 있다.
  - 승률이 25~41%로 낮다. 연속 손절이 정상이며 심리적으로 견디기 어렵다.
  - 2023년은 3배 기준 -18.3%, 최대낙폭 38%였다.

주의 - freqtrade 단위 규칙:
  stoploss / minimal_roi 값은 '가격 이동폭'이 아니라 '레버리지 적용 후 계좌
  손익률' 단위다 (adjust_stop_loss: new_loss = price * (1 - |stoploss/leverage|)).
  이 전략은 가격 기준 손절폭을 stop_price_pct 로 두고, custom_stoploss에서
  stoploss_from_absolute()로 정확히 변환한다.
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


class BoxBreakoutStrategy(IStrategy):

    timeframe = "1h"
    startup_candle_count = 120

    can_short = True

    # ------------------------------------------------------------------
    # 청산 설정
    # ------------------------------------------------------------------
    # 가격 기준 손절폭. 분석상 1.5%가 최적 (1.0~2.0% 구간은 완만한 고원).
    stop_price_pct = DecimalParameter(0.010, 0.030, default=0.015, decimals=3, space="sell")

    # 최대 보유 시간(봉). 48봉=2일이 가장 안정적이었음.
    max_hold_candles = IntParameter(24, 96, default=48, space="sell")

    # 안전망 stoploss. custom_stoploss가 항상 이보다 타이트하게 조이므로
    # 실제로는 거의 쓰이지 않지만, freqtrade는 진입 시 이 값으로 손절선을
    # 먼저 잡고 custom_stoploss는 '조이기'만 가능하므로 반드시 느슨해야 한다.
    # (레버리지 5배 기준 -0.50 = 가격 10% 이동)
    stoploss = -0.50
    use_custom_stoploss = True

    trailing_stop = False

    # 주의: freqtrade는 custom_exit()을 use_exit_signal 플래그 안에서만 호출한다
    # (interface.py: `if self.use_exit_signal:` -> custom_exit).
    # False로 두면 아래 custom_exit의 시간청산이 통째로 죽는다 — 반드시 True.
    # populate_exit_trend가 exit 컬럼을 세팅하지 않으므로 신호청산은 발생하지 않고,
    # 실제로는 custom_exit(시간청산)만 동작한다.
    use_exit_signal = True
    exit_profit_only = False

    # ROI 미사용. minimal_roi 값도 '레버리지 적용 후' 단위라서 값을 고정하면
    # 레버리지마다 실제 익절선이 달라져 비교가 오염된다(1배=가격100%, 5배=가격20%).
    # 분석상 익절 도달률이 1.1%로 무의미했으므로 아예 걸지 않고 시간청산에 맡긴다.
    minimal_roi = {"0": 100.0}

    # ------------------------------------------------------------------
    # 박스 정의 파라미터
    # ------------------------------------------------------------------
    # 견고성 탐색 결과 N=12~32 / 폭 3~4% 구간이 안정적으로 플러스였다.
    # (N=48은 IS에서 마이너스가 잦아 실질 상한은 32 정도로 본다)
    # 기본값 24/3%는 '최고점'이 아니라 그 구간의 중앙값 쪽이다 — 일부러
    # 최고 성적 조합을 기본값으로 잡지 않았다(선택 편향 방지).
    box_period = IntParameter(12, 32, default=24, space="buy")
    box_max_width = DecimalParameter(0.020, 0.050, default=0.030, decimals=3, space="buy")

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return max(1.0, min(float(self.config.get("leverage", 1)), max_leverage))

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = self.box_period.value
        # shift(1): 현재 봉을 박스 계산에서 제외 -> 룩어헤드 차단
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
        # 신호 기반 청산은 쓰지 않는다 (청산은 custom_stoploss + custom_exit 담당)
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """
        진입가 기준 고정 손절가를 freqtrade가 요구하는 '현재가 대비 비율'로 변환.
        stoploss_from_absolute()가 레버리지 나눗셈과 기준가 차이를 모두 처리한다.
        """
        pct = self.stop_price_pct.value
        if trade.is_short:
            stop_price = trade.open_rate * (1 + pct)
        else:
            stop_price = trade.open_rate * (1 - pct)
        return stoploss_from_absolute(
            stop_price, current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage or 1.0,
        )

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """최대 보유 시간 도달 시 시간청산. 이 전략 수익의 대부분이 여기서 나온다."""
        held = current_time - trade.open_date_utc
        if held >= timedelta(hours=self.max_hold_candles.value):
            return "max_hold"
        return None
