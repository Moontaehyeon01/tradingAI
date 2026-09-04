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

  2026-09-04 재검증 (바이낸스 선물 19개 페어 일봉 직접 수집, 위와 동일 규칙 재현):
    수수료 0%로 돌리면 위 수치가 거의 그대로 재현됨(연 수익률/MDD/BTC상관/구간
    승률/순위상관 전부 근접) - 전략 로직 자체는 위 검증과 일치한다는 뜻.
    다만 포지션 6개(롱3+숏3)가 각자 실제 체결 수수료를 무는 것으로 정확히
    계산하면(같은 계좌 안에서도 서로 다른 코인끼리는 상계가 안 됨) 그림이
    많이 나빠진다 - 0.10%/side 기준 연 +28%로 급락, 0.20%/side에서는 적자.
    최근 2년만 떼어 보면(검증구간) 파라미터 격자 대부분이 마이너스.
    -> 위에 적힌 "고원 평균 +54.3%"는 수수료가 사실상 반영 안 된 수치였을
    가능성이 높다. 실제 라이브 성과는 이 주석보다 훨씬 박할 수 있다.

  2026-09-04 익절가 추가 근거: 3일 청산(hold_days=3) 그대로 두고 진입가
    대비 가격이 ±30% 움직이면(레버리지 반영 전 순수 가격 기준) 3일을 안
    기다리고 바로 청산하도록 테스트한 결과, 수수료 반영 후에도 연 수익률이
    뚜렷하게 개선됐다(+19.7% -> +48.4%, MDD -84%대 -> -65%대, t값 1.6 -> 2.4).
    좁은 익절(2~15%)은 오히려 손해 - 큰 흐름을 너무 일찍 끊고 짧은 거래마다
    수수료만 문다. 25~35% 구간이 고원을 이루고 있어 30%로 잡았다. 걷는검증
    (5구간 x 2가지 구간나누기)에서도 시간청산만 쓰는 것보다 일관되게 낫거나
    비슷했다. (참고: hold_days 자체를 10일로 늘리면 익절 없이도 더 좋다는
    결과도 있었으나, 이번엔 "3일은 유지"라는 요청에 따라 익절만 추가함.)

  2026-09-04 고정 30% -> 종목별 변동성 스케일링으로 교체: 고정 30%는 BTC(3일
    변동성 표준편차 ~5%)한테는 사실상 절대 안 닿는 값이라 익절이 없는 것과
    같았고(실측 발동률 0.8%, ETH는 0.0%), DOGE(~18%)한테는 오히려 자주 걸려서
    (6.6%) 큰 흐름을 끊었다. 종목마다 "3일 수익률 표준편차 x 3배"를 그 종목의
    익절폭으로 쓰도록 바꿨다(5~100% 사이로 clip). 평균 익절폭은 고정 30%와
    비슷한 ~29%로 유지되지만, BTC 발동률 0.8%->4.6%, ETH 0.0%->1.5%로
    올라가면서 전체 성과도 더 좋아졌다(연 +48.4% -> +55.5%, MDD -65%대 ->
    -62%대, t값 2.4 -> 2.6). 걷는검증에서도 두 구간나누기 방식 모두 평균
    수익률이 고정 30% 대비 거의 2배로 나왔다.
    자세한 스캔 결과는 대시보드/커밋 기록 참고.

설계
  - 매일(1d 봉) 감시 페어 전체의 lookback일 수익률을 계산해 순위를 매긴다
  - 상위 top_k -> 롱, 하위 top_k -> 숏
  - 진입가 대비, 그 종목의 변동성에 맞춘 익절폭만큼 가격이 유리하게
    움직이면 즉시 청산 (아래 take_profit_vol_mult 주석 참고)
  - 그게 아니면 hold_days 경과 시 청산 (순위에서 벗어나도 청산)
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
    # 익절폭 계산에 최근 take_profit_vol_window(180)일치 수익률이 필요해서
    # 순위 계산에만 필요했던 120에서 늘렸다.
    startup_candle_count = 200

    # 파라미터. 위 주석대로 '고원 중앙'을 쓰고 튜닝하지 않는다.
    lookback = IntParameter(7, 90, default=14, space="buy")
    top_k = IntParameter(1, 5, default=3, space="buy")
    hold_days = IntParameter(1, 14, default=3, space="sell")

    # 익절폭 = 그 종목의 최근 take_profit_vol_window일 "3일 수익률" 표준편차
    # x take_profit_vol_mult, [take_profit_min_pct, take_profit_max_pct] 사이로
    # clip. 레버리지 반영 전 순수 가격 기준(minimal_roi를 안 쓰는 이유는 이전과
    # 동일 - leverage 설정이 바뀌어도 가격 목표가 안 흔들리게 하기 위함).
    # 종목마다 다른 고정폭을 쓰는 이유는 위 2026-09-04 주석 참고.
    take_profit_vol_mult = 3.0
    take_profit_vol_window = 180
    take_profit_min_pct = 0.05
    take_profit_max_pct = 1.00

    @property
    def can_short(self) -> bool:
        # 클래스 속성으로 두면 spot 설정에서 freqtrade가 시작 시점에 하드 에러를 낸다
        return self.config.get("trading_mode") == "futures"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # bot_loop_start 에서 채우고 populate_entry_trend / custom_exit 에서 읽는다
        self._longs: set = set()
        self._shorts: set = set()
        self._ranked_at = None
        self._tp_by_pair: dict = {}

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
        tp_by_pair = {}

        for pair in self.dp.current_whitelist():
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or len(df) < lb + 2:
                continue
            # 마지막 행은 아직 진행 중인 봉일 수 있으므로 완성된 봉만 쓴다.
            closes = df["close"].to_numpy()
            now, past = closes[-2], closes[-2 - lb]
            if np.isfinite(now) and np.isfinite(past) and past > 0:
                scores[pair] = now / past - 1.0

            # 익절폭: 이 종목의 최근 3일 수익률 표준편차 x 배수. 랭킹 계산과
            # 별개로 - 오늘 순위에 안 들어도 이미 열려있는 포지션의 익절폭은
            # 계속 최신으로 유지해야 하므로 감시 페어 전체에 대해 계산한다.
            ret3 = df["close"].pct_change(3)
            # 진행 중인 마지막 봉은 제외하고, 최근 vol_window개만 본다.
            window = ret3.iloc[-(self.take_profit_vol_window + 2):-1].dropna()
            if len(window) >= 60:
                vol = float(window.std())
                if np.isfinite(vol) and vol > 0:
                    tp = self.take_profit_vol_mult * vol
                    tp_by_pair[pair] = float(
                        np.clip(tp, self.take_profit_min_pct, self.take_profit_max_pct)
                    )
        self._tp_by_pair = tp_by_pair

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
        """익절 목표를 먼저 보고, 없으면 보유기간이 지날 때 순위와 무관하게
        청산한다(검증한 리밸런싱 규칙). 익절폭은 종목별 변동성 스케일링(순수
        가격 기준, 레버리지 미반영) - 위 take_profit_vol_mult 주석 참고.
        해당 종목의 변동성을 아직 못 구했으면(상장 직후 등) 익절 없이
        hold_days 청산만 적용한다 - 진입 자체는 lookback만 있으면 되지만
        변동성은 훨씬 긴 히스토리(take_profit_vol_window)가 필요해서다."""
        tp_move = self._tp_by_pair.get(pair)
        if tp_move is not None:
            if trade.is_short:
                price_move = (trade.open_rate - current_rate) / trade.open_rate
            else:
                price_move = (current_rate - trade.open_rate) / trade.open_rate
            if price_move >= tp_move:
                return "take_profit"

        if (current_time - trade.open_date_utc) >= timedelta(days=self.hold_days.value):
            return "rebalance"
        return None
