# -*- coding: utf-8 -*-
"""
XSectMomentumStrategy — 횡단면 모멘텀 (시장중립 롱숏)
=====================================================

지금까지의 전략들과 근본적으로 다른 점: **방향을 예측하지 않는다.**
매 시점 감시 페어 전체를 최근 수익률로 줄 세워, 상위를 롱 / 하위를 숏 한다.
시장 전체가 오르든 내리든 상대 강도만 남기 때문에 베타가 상쇄된다.

  2026-09-03 검증 (19개 페어, 2019-09 ~ 2026-09, 편도 0.10%, 펀딩 반영)
    BTC 상관 -0.045 / 베타 -0.053   -> 실제로 시장중립
    고원 평균 연 +54.3% (중앙값 +50.8%, 최소 +35.2%), 평균 t +1.67
    학습/검증 분리에서 검증구간 28/28 칸 양수
    대형코인 9개만으로도 유지 (생존편향 20%만 축소)
    편도 0.20% 비용에서도 t=2.3
    펀딩 순기여 -2.7% ~ +1.6% (롱 지불과 숏 수취가 상계됨)

*** 반드시 알아야 할 한계 ***

  학습↔검증 순위상관 = -0.082.
  즉 "어떤 룩백/보유기간이 가장 좋은가"는 다음 구간에 재현되지 않는다.
  최고 칸(+83%)이 아니라 **고원 평균 +54%, 나쁘면 +35%** 를 기대치로 볼 것.
  파라미터를 튜닝해서 숫자를 올리는 행위는 의미가 없다.

  MDD 62.9%. 시장중립이라고 안전한 것이 아니다.
  상장폐지된 페어를 복원하지 못해 생존편향이 부분적으로만 해소됐다.

설계
  - 매일(1d 봉) 감시 페어 전체의 lookback일 수익률을 계산해 순위를 매긴다
  - 상위 top_k -> 롱, 하위 top_k -> 숏
  - hold_days 경과하면 청산 (순위에서 벗어나도 청산)
  - 손절은 안전망만 (개별 손절이 아니라 포트폴리오 분산으로 위험을 관리하는 전략)
"""
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter


class XSectMomentumStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"

    # 횡단면 전략이라 개별 페어의 ROI/손절로 나가면 롱숏 균형이 깨진다.
    # 청산은 custom_exit(보유기간)이 전담하고, 아래 둘은 안전망으로만 둔다.
    minimal_roi = {"0": 100.0}
    stoploss = -0.60
    trailing_stop = False

    # custom_exit 을 쓰려면 반드시 True (freqtrade는 use_exit_signal 안에서만 호출한다)
    use_exit_signal = True
    exit_profit_only = False

    process_only_new_candles = True
    startup_candle_count = 120

    # 파라미터. 위 주석대로 '고원 중앙'을 쓰고 튜닝하지 않는다.
    lookback = IntParameter(7, 90, default=14, space="buy")
    top_k = IntParameter(1, 5, default=3, space="buy")
    hold_days = IntParameter(1, 14, default=3, space="sell")

    @property
    def can_short(self) -> bool:
        # 클래스 속성으로 두면 spot 설정에서 freqtrade가 시작 시점에 하드 에러를 낸다
        return self.config.get("trading_mode") == "futures"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # bot_loop_start 에서 채우고 populate_entry_trend 에서 읽는다
        self._longs: set = set()
        self._shorts: set = set()
        self._ranked_at = None

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return float(self.config.get("leverage", 1))

    # ------------------------------------------------------------------
    # 순위 계산: freqtrade 는 페어별로 독립 호출되므로, 전체를 보는 계산은
    # 루프 시작 시점에 한 번만 해두고 각 페어가 그 결과를 참조한다.
    # ------------------------------------------------------------------
    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        lb = self.lookback.value
        k = self.top_k.value
        scores = {}

        for pair in self.dp.current_whitelist():
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or len(df) < lb + 2:
                continue
            # 마지막 행은 아직 진행 중인 봉일 수 있으므로 완성된 봉만 쓴다.
            closes = df["close"].to_numpy()
            now, past = closes[-2], closes[-2 - lb]
            if not np.isfinite(now) or not np.isfinite(past) or past <= 0:
                continue
            scores[pair] = now / past - 1.0

        # 상위/하위를 뽑으려면 양쪽에 최소 k개씩은 있어야 한다
        if len(scores) < 2 * k:
            self._longs, self._shorts = set(), set()
            return

        order = sorted(scores, key=scores.get)
        self._shorts = set(order[:k])
        self._longs = set(order[-k:])
        self._ranked_at = current_time

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        lb = self.lookback.value
        dataframe["ret_lb"] = dataframe["close"].pct_change(lb)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        # 순위는 페어 전체를 봐야 나오므로 bot_loop_start 의 결과를 쓴다.
        # 백테스트에서는 bot_loop_start 가 매 봉 호출되지 않아 이 전략은
        # 라이브 전용이다 (검증은 별도 스크립트로 수행했다 - 상단 주석 참고).
        if pair in self._longs:
            dataframe.loc[dataframe.index[-1], ["enter_long", "enter_tag"]] = (1, "xs_top")
        elif pair in self._shorts:
            dataframe.loc[dataframe.index[-1], ["enter_short", "enter_tag"]] = (1, "xs_bottom")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs) -> Optional[str]:
        """보유기간이 지나면 순위와 무관하게 청산 (검증한 리밸런싱 규칙)."""
        if (current_time - trade.open_date_utc) >= timedelta(days=self.hold_days.value):
            return "rebalance"
        return None
