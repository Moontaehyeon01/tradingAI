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

  2026-09-14 순위를 하루 1회로 고정: bot_loop_start 가 루프(몇 초~몇십 초)
    마다 순위를 다시 계산하고 있었는데, 자정 직후 몇 분은 일부 페어의 당일
    봉이 아직 안 들어온 상태라 이 시간대에 도는 여러 번의 루프마다 순위
    구성원이 미묘하게 달라질 수 있었다. populate_entry_trend는 페어마다
    독립 호출되는데 그 순간 self._longs/_shorts가 어느 스냅샷이었는지에
    따라 페어별로 다른 "오늘의 목표"를 보게 되고, 한 번 신호 없이 지나간
    페어는 process_only_new_candles 때문에 그날 다시 기회가 없었다.
    실측으로 확인된 피해: 9/11~9/13 사흘간 하루에 2~3개씩만 신호가 나서
    롱4:숏2로 시장중립이 깨진 채 누적됨(9/9엔 아예 슬롯 하나가 남은 증거금
    부족으로 8개월 최소단위로 겨우 체결되기도 함 - 전부 같은 근본 원인).
    이제 하루 중 처음 확정된 순위를 그날 내내 고정해서 쓴다(자정 후 2분은
    데이터가 덜 갱신됐을 수 있어 그 사이엔 순위를 비워 진입을 아예 막고,
    2분이 지난 뒤 첫 계산 결과를 그날 종일 재사용). 실패해도(페어 데이터
    부족 등) 그날은 재시도하지 않고 다음날까지 대기 - 재시도가 오히려
    또 다른 시점의 스냅샷을 만들어 같은 문제를 반복시키기 때문이다.

  2026-09-17 위 수정이 오히려 진입을 통째로 막고 있었음을 발견: 당일 봉은
    자정(00:00:00)에 "새 봉"으로 즉시 나타나는데, process_only_new_candles=True
    라서 populate_entry_trend는 그 새 봉에 대해 딱 한 번만 호출되고 결과가
    캐시된다. 그런데 bot_loop_start의 순위 확정은 자정+2분 버퍼 이후에야
    끝나므로, 그 딱 한 번의 호출 시점엔 self._longs/self._shorts가 항상
    비어있는 상태였다 - 19개 페어 전부 그 순간 "무신호"로 캐시되고,
    process_only_new_candles 때문에 순위가 나중에 채워져도 그날은 다시
    평가되지 않았다. 그 결과 9/14부터 9/17까지 나흘 연속 신규 진입이
    0건이었다(실계좌에서 실측 확인). 순위를 하루 1회로 고정하는 것과
    process_only_new_candles=True 는 서로 상충한다 - 후자를 꺼서
    populate_entry_trend가 매 루프 다시 평가되게 하고, 순위 고정 자체는
    위 bot_loop_start의 날짜 잠금이 계속 담당한다(이미 포지션이 열린
    페어는 freqtrade가 중복 진입을 막아주므로 매 루프 재평가돼도 안전함).

  2026-09-19 롱4:숏2(또는 그 반대) 불균형 발견: 빈 슬롯을 채울 때 방향
    구분 없이 그날 새로 발견된 후보 중 먼저 처리되는 것부터 채워왔다.
    익절은 롱/숏 어느 쪽이든 가격이 유리해지면 아무 때나 개별적으로
    발동하므로, 어느 날 우연히 한쪽 방향만 먼저 익절돼 슬롯이 비면
    다음 리밸런스에서 그 슬롯이 반대쪽이 아니라 아무 후보로나 채워져
    시장중립 구조(3롱:3숏)가 깨진 채 여러 날 누적될 수 있었다(실측:
    9/18 롱2개 익절 -> 9/19 리밸런스에서 그 자리에 롱1+숏1이 채워져
    최종 롱2:숏4). confirm_trade_entry()에서 그 방향 보유 개수가 이미
    top_k 이상이면 진입을 거부하도록 고쳐서, 빈 슬롯이 항상 부족한
    쪽(아직 top_k 미만인 방향) 후보로만 채워지게 했다.

  2026-10-04 보유기간 만기 청산 직후 같은 종목 재진입 문제: 만기가 되면
    순위와 무관하게 청산하고, 빈 자리를 그날 순위권 종목으로 채웠는데,
    청산한 종목이 아직 순위권이면 같은 자리에 그대로 다시 들어갔다.
    청산+같은 가격 재진입은 계속 보유하는 것과 같으면서 왕복 수수료와
    스프레드만 더 내는 셈이다. 이제 만기(3일)가 돼도 그 종목이 그날 같은
    방향 순위권(top_k)에 있으면 하루 더 보유하고, 4일(max_hold_days)이
    되면 순위와 무관하게 청산한다. 청산한 종목은 48시간(reentry_cooldown_hours)
    동안 방향 무관 재진입을 막는다 - 거래 기록의 마지막 청산 시각 기준으로
    직접 계산(freqtrade 잠금은 일봉 경계로 올림돼 ~72시간이 됨). 대기 중인
    종목이 top_k 안에 있으면 그 자리는 다음 순위 종목이 대신 후보가 되어,
    슬롯이 비거나 롱/숏 균형이 깨지지 않게 했다(_pick_eligible).

  2026-10-04 순위가 하루 늦은 가격으로 매겨지던 문제: freqtrade 데이터프레임엔
    완성된 봉만 있는데 마지막 행을 진행 중인 봉으로 착각해 끝에서 2번째 봉을
    기준으로 써왔다 - 실전 순위가 검증(백테스트, 최신 마감가 기준)보다 하루
    늦게 반응하고 있었다. 마지막 행(어제 마감 봉)을 쓰도록 고치고, 모든 종목에
    어제 봉이 들어온 걸 확인한 뒤에 순위를 확정한다(최대 자정+30분 대기).

  2026-10-06 장중 진입 문제: 진입 판단은 하루 종일 도는데, 재진입 대기 종목
    자리를 다음 순위가 채우는 규칙(위 10-04) 이후로 수동 청산·장중 익절로
    자리가 빈 순간 아무 때나 새 종목이 들어갔다(실측: 06:11 KST 수동 청산
    -> 06:31 감지 -> ETH 숏/ADA 롱 즉시 진입). 신규 진입은 매일 자정~01:00 UTC
    (09:00~10:00 KST, 오늘 순위 확정 후)에만 허용한다(ENTRY_WINDOW).

  2026-10-06 순위 점수(_rank_score)와 후보 제외 조건(_is_blocked)을 메서드로
    분리했다(동작은 그대로). 같은 규칙에 RSI(14) 순위만 바꾼 XSectRsiStrategy가
    이 둘을 덮어써서 쓴다.

설계
  - 매일(1d 봉) 감시 페어 전체의 lookback일 수익률을 계산해 순위를 매긴다
    (하루 중 자정+2분 이후 첫 계산 결과를 그날 내내 고정 - 위 2026-09-14 주석 참고)
  - 상위 top_k -> 롱, 하위 top_k -> 숏
  - 진입가 대비, 그 종목의 변동성에 맞춘 익절폭만큼 가격이 유리하게
    움직이면 즉시 청산 (아래 take_profit_vol_mult 주석 참고)
  - 그게 아니면 hold_days(3일) 경과 시 청산하되, 그날 같은 방향 순위권이면
    하루 더 보유하고 max_hold_days(4일)에는 무조건 청산 (위 2026-10-04 주석 참고)
  - 청산한 종목은 48시간 재진입 금지
  - 신규 진입은 매일 09:00~10:00 KST(순위 확정 후)에만 - 장중에 빈 자리는 다음날까지 비워둠
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

    # 2026-09-17: True였을 때 당일 봉의 populate_entry_trend가 자정 직후
    # (순위가 아직 안 정해진 시점)에 한 번만 평가되고 캐시돼서, 그날 순위가
    # 나중에 확정돼도 다시 반영되지 않아 나흘 내내 진입이 0건이었다(위
    # 2026-09-17 주석 참고). False로 두면 매 루프 재평가하므로 순위가
    # 늦게 확정돼도 그 다음 루프에서 바로 반영된다.
    process_only_new_candles = False
    # 익절폭 계산에 최근 take_profit_vol_window(180)일치 수익률이 필요해서
    # 순위 계산에만 필요했던 120에서 늘렸다.
    startup_candle_count = 200

    # 파라미터. 위 주석대로 '고원 중앙'을 쓰고 튜닝하지 않는다.
    lookback = IntParameter(7, 90, default=14, space="buy")
    top_k = IntParameter(1, 5, default=3, space="buy")
    hold_days = IntParameter(1, 14, default=3, space="sell")
    # hold_days가 지나도 같은 방향 순위권이면 하루 더 보유하되, 이 일수가
    # 되면 순위와 무관하게 청산한다(위 2026-10-04 주석 참고).
    max_hold_days = 4
    # 청산한 종목은 이 시간 동안 (방향 무관) 재진입하지 않는다. freqtrade의
    # CooldownPeriod/PairLock은 잠금 끝을 다음 일봉 시작(UTC 00:00)으로 올려버려서
    # 1d 타임프레임에선 48시간이 실제로 ~72시간이 된다 - 그래서 거래 기록의
    # 마지막 청산 시각으로 직접 계산한다(_in_reentry_cooldown).
    reentry_cooldown_hours = 48

    # 익절폭 = 그 종목의 최근 take_profit_vol_window일 "3일 수익률" 표준편차
    # x take_profit_vol_mult, [take_profit_min_pct, take_profit_max_pct] 사이로
    # clip. 레버리지 반영 전 순수 가격 기준(minimal_roi를 안 쓰는 이유는 이전과
    # 동일 - leverage 설정이 바뀌어도 가격 목표가 안 흔들리게 하기 위함).
    # 종목마다 다른 고정폭을 쓰는 이유는 위 2026-09-04 주석 참고.
    # 2026-09-17 재검증: 2019-09~2026-09 19페어 일봉으로 lookback x
    # take_profit_vol_mult 격자 재검증(로컬 스크립트, 왕복수수료 0.10%).
    # mult=3.0 -> 2.0으로 낮추자 전반기/후반기 두 구간 모두에서 t-stat이
    # 개선되고(3.09 -> 4.10) MDD도 줄었다(-32.2% -> -17.0%), 익절 발동률은
    # 5%->12.7%로 여전히 낮은 편이라 "짧은 익절 반복 복리"로 숫자만 부풀리는
    # 구간(발동률 50%대까지 치솟는 mult<1 구간에서 확인됨 - 실제 엣지가 아니라
    # 슬리피지 0/무제한 유동성을 가정한 백테스트 복리 함정으로 판단)과는
    # 구분된다. lookback은 7~90 전체가 완만한 고원이라 14 그대로 유지.
    take_profit_vol_mult = 2.0
    take_profit_vol_window = 180
    take_profit_min_pct = 0.05
    take_profit_max_pct = 1.00

    @property
    def can_short(self) -> bool:
        # 클래스 속성으로 두면 spot 설정에서 freqtrade가 시작 시점에 하드 에러를 낸다
        return self.config.get("trading_mode") == "futures"

    # 자정 직후 이 시간 동안은 순위를 계산하지 않는다 - 일부 페어의 당일 봉이
    # 아직 안 들어왔을 수 있어서, 너무 일찍 확정하면 그 미갱신 스냅샷이
    # 하루 종일 굳어버린다.
    RANK_LOCK_DELAY = timedelta(minutes=2)
    # 그래도 어제 봉이 안 들어온 종목이 있으면 이 시각까지 기다렸다가, 넘으면
    # 들어온 종목만으로 순위를 정한다.
    RANK_FRESH_WAIT = timedelta(minutes=30)
    # 신규 진입은 매일 리밸런스 시간(자정~이 시각, 오늘 순위 확정 후)에만 한다.
    # 진입 판단 자체는 하루 종일 돌기 때문에, 이게 없으면 수동 청산·장중 익절로
    # 자리가 빈 순간 다음 순위 종목이 아무 때나 들어간다(2026-10-06 확인).
    ENTRY_WINDOW = timedelta(hours=1)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # 그날의 전체 순위(롱은 강한 순, 숏은 약한 순) - 하루 1회 확정
        self._long_order: list = []
        self._short_order: list = []
        # 위 순위에서 재진입 대기 종목을 건너뛴 실제 후보 top_k - 매 루프 갱신.
        # bot_loop_start 에서 채우고 populate_entry_trend / custom_exit 에서 읽는다
        self._longs: set = set()
        self._shorts: set = set()
        self._ranked_at = None
        # 오늘(date) 순위를 이미 확정했는지 - 확정했으면 그날은 다시 계산하지
        # 않는다(성공/실패 여부와 무관하게 "오늘은 시도 끝"으로 취급).
        self._ranked_date = None
        self._tp_by_pair: dict = {}
        self._entries_open = False

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return float(self.config.get("leverage", 1))

    # ------------------------------------------------------------------
    # 순위 계산: freqtrade 는 페어별로 독립 호출되므로, 전체를 보는 계산은
    # 루프 시작 시점에 한 번만 해두고 각 페어가 그 결과를 참조한다.
    # ------------------------------------------------------------------
    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        # 익절폭은 순위와 무관하게 매 루프 최신화한다 - 오늘 순위에 안 들어도
        # 이미 열려있는 포지션의 익절폭은 계속 최신으로 유지해야 한다.
        self._tp_by_pair = self._compute_tp_by_pair()
        self._update_daily_ranking(current_time)
        # 순위는 하루 고정이지만, 재진입 대기가 풀리는 시점은 하루 중 아무 때나
        # 올 수 있어서 실제 후보는 매 루프 다시 고른다.
        k = self.top_k.value
        self._longs = self._pick_eligible(self._long_order, "long", k, current_time)
        self._shorts = self._pick_eligible(self._short_order, "short", k, current_time)
        self._entries_open = self._in_entry_window(current_time)

    def _in_entry_window(self, now: datetime) -> bool:
        since_midnight = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._ranked_date == now.date() and since_midnight <= self.ENTRY_WINDOW

    def _pick_eligible(self, order: list, side: str, k: int, now: datetime) -> set:
        """순위 순서대로 보면서 재진입 대기 중인 종목은 건너뛰고 k개를 고른다.
        top_k 안에 대기 종목이 있으면 그 자리를 다음 순위가 대신 채워서, 후보가
        없어 슬롯이 비고 롱/숏 균형이 깨지는 걸 막는다. 중간 순위 너머(반대쪽
        후보 영역)까지는 내려가지 않는다 - 그건 더 이상 모멘텀이 아니다."""
        picked: set = set()
        for pair in order[: len(order) // 2]:
            if len(picked) >= k:
                break
            if self._is_blocked(pair, side, now):
                continue
            picked.add(pair)
        return picked

    def _is_blocked(self, pair: str, side: str, now: datetime) -> bool:
        """순위권이어도 후보에서 건너뛸 종목인지 (하위 클래스가 조건을 더한다)."""
        return self._in_reentry_cooldown(pair, now) or self.is_pair_locked(pair, side=side)

    def _in_reentry_cooldown(self, pair: str, now: datetime) -> bool:
        since = now - timedelta(hours=self.reentry_cooldown_hours)
        return bool(Trade.get_trades_proxy(pair=pair, is_open=False, close_date=since))

    def _update_daily_ranking(self, current_time: datetime) -> None:
        today = current_time.date()
        if self._ranked_date == today:
            return  # 오늘은 이미 확정했다 - 재계산하지 않는다(위 2026-09-14 주석 참고)

        # 날짜가 바뀌었는데 아직 오늘 순위를 못 정했다 -> 새로 정하기 전까지는
        # 어제 순위로 잘못 진입하지 않도록 일단 비워둔다.
        self._long_order, self._short_order = [], []

        since_midnight = current_time - current_time.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if since_midnight < self.RANK_LOCK_DELAY:
            return  # 자정 버퍼 시간 - 이번 루프는 넘기고 다음 루프에 다시 시도

        lb = self.lookback.value
        k = self.top_k.value
        # freqtrade 데이터프레임엔 완성된 봉만 들어있다 - 마지막 행이 곧 어제
        # (가장 최근에 마감된) 일봉이다. 예전엔 마지막 행을 진행 중인 봉으로
        # 잘못 알고 끝에서 2번째를 써서, 순위가 하루 늦은 가격으로 매겨지고
        # 있었다(2026-10-04 확인 - 백테스트는 최신 마감 가격 기준).
        expected_last = (current_time - timedelta(days=1)).date()
        scores = {}
        stale = []
        for pair in self.dp.current_whitelist():
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or "close" not in df.columns or len(df) < lb + 1:
                continue
            if df["date"].iloc[-1].date() != expected_last:
                stale.append(pair)  # 어제 봉이 아직 안 들어옴
                continue
            score = self._rank_score(df["close"].to_numpy())
            if score is not None:
                scores[pair] = score

        # 일부 종목만 어제 봉이 반영된 상태로 순위를 매기면 종목마다 기준일이
        # 달라진다 - 전부 들어올 때까지 확정을 미룬다(아직 아무것도 안 정했으니
        # 재시도해도 스냅샷이 섞이지 않는다). 30분이 지나도 안 들어오는 종목은
        # 그날 순위에서 뺀다.
        if stale and since_midnight < self.RANK_FRESH_WAIT:
            return

        # 성공하든 실패하든 "오늘은 시도 끝"으로 표시한다 - 데이터가 부족해서
        # 못 정했다고 같은 날 안에서 계속 재시도하면 그것대로 시점마다 다른
        # 스냅샷을 만들 수 있다. 못 정한 날은 그냥 하루 쉬고 내일 다시 본다.
        self._ranked_date = today

        # 상위/하위를 뽑으려면 양쪽에 최소 k개씩은 있어야 한다
        if len(scores) < 2 * k:
            return

        order = sorted(scores, key=scores.get)
        self._short_order = order          # 약한 순
        self._long_order = order[::-1]     # 강한 순
        self._ranked_at = current_time

    def _rank_score(self, closes: np.ndarray) -> Optional[float]:
        """순위 점수(높을수록 롱 쪽). closes 의 마지막 값이 어제 마감 종가.
        이 전략은 lookback일 수익률 - 하위 클래스(XSectRsiStrategy)가 바꿔 쓴다.
        점수를 못 구하면 None (그 종목은 그날 순위에서 빠진다)."""
        lb = self.lookback.value
        now, past = closes[-1], closes[-1 - lb]
        if np.isfinite(now) and np.isfinite(past) and past > 0:
            return float(now / past - 1.0)
        return None

    def _compute_tp_by_pair(self) -> dict:
        tp_by_pair = {}
        for pair in self.dp.current_whitelist():
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            # 컨테이너 막 재시작 직후처럼 일부 페어 데이터가 아직 안 채워진
            # 순간엔 df가 비어있거나 close 컬럼이 없을 수 있다(2026-09-29
            # 재시작 때 KeyError('close')로 한 번 확인됨) - None 체크만으론
            # 못 막아서 컬럼 존재까지 확인한다.
            if df is None or "close" not in df.columns or len(df) == 0:
                continue
            ret3 = df["close"].pct_change(3)
            # 데이터프레임엔 완성된 봉만 있으므로 마지막 봉까지 포함해 최근 vol_window개
            window = ret3.iloc[-self.take_profit_vol_window:].dropna()
            if len(window) >= 60:
                vol = float(window.std())
                if np.isfinite(vol) and vol > 0:
                    tp = self.take_profit_vol_mult * vol
                    tp_by_pair[pair] = float(
                        np.clip(tp, self.take_profit_min_pct, self.take_profit_max_pct)
                    )
        return tp_by_pair

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
        # 리밸런스 시간 밖이면 신호 자체를 안 낸다(confirm_trade_entry 거부 로그가
        # 몇 초마다 쌓이는 것도 피함). _longs/_shorts는 보유 연장 판단에도 쓰이므로
        # 그대로 두고 신호만 막는다.
        if not self._entries_open:
            return dataframe
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
        """익절 목표를 먼저 보고, 없으면 보유기간(3일)이 지났을 때 그 종목이
        오늘 같은 방향 순위권에서 빠졌으면 청산, 순위권이면 하루 더 보유,
        4일째엔 무조건 청산한다. 익절폭은 종목별
        변동성 스케일링(순수 가격 기준, 레버리지 미반영) - 위
        take_profit_vol_mult 주석 참고. 해당 종목의 변동성을 아직 못
        구했으면(상장 직후 등) 익절 없이 보유기간 청산만 적용한다."""
        tp_move = self._tp_by_pair.get(pair)
        if tp_move is not None:
            if trade.is_short:
                price_move = (trade.open_rate - current_rate) / trade.open_rate
            else:
                price_move = (current_rate - trade.open_rate) / trade.open_rate
            if price_move >= tp_move:
                return "take_profit"

        age = current_time - trade.open_date_utc
        if age < timedelta(days=self.hold_days.value):
            return None
        if age >= timedelta(days=self.max_hold_days):
            return "max_hold"  # 연장은 하루까지 - 순위권이어도 청산
        # 오늘 순위가 아직 안 정해졌으면(자정+2분 버퍼) 만기 청산을 미룬다.
        # 만기가 대부분 자정 직후라, 안 미루면 빈 순위를 보고 "순위권 아님"
        # 으로 오판해 계속 보유했어야 할 포지션을 청산해버린다. 순위 계산이
        # 실패한 날(양쪽 다 비어있음)도 같은 이유로 그날은 보유한다.
        if self._ranked_date != current_time.date() or not (self._long_order or self._short_order):
            return None
        ranked_same_side = self._shorts if trade.is_short else self._longs
        if pair in ranked_same_side:
            return None  # 여전히 순위권 - 청산 후 같은 종목 재진입(수수료만 낭비) 대신 보유 연장
        return "rebalance"

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time: datetime,
                             entry_tag: Optional[str], side: str, **kwargs) -> bool:
        """롱/숏 어느 한쪽이 top_k(기본 3)개를 이미 채웠으면 그 방향 신규 진입을
        막는다.

        2026-09-19 확인된 문제: 빈 슬롯을 채울 때 방향 구분 없이 그날 새로
        발견된 후보 중 처리 순서상 먼저 걸리는 것부터 채우다 보니, 한쪽
        방향(예: 롱)만 먼저 익절돼 슬롯이 비면 다음날 리밸런스에서 그 슬롯이
        롱/숏 아무 후보로나 채워져 롱2:숏4처럼 시장중립이 깨진 채 굳어질 수
        있었다. 이제 confirm_trade_entry에서 그 방향 보유 개수를 세어
        top_k 이상이면 진입 자체를 거부한다 - 그러면 빈 슬롯은 반대쪽(아직
        top_k 미만인) 방향의 후보로만 채워져 3:3 구조가 항상 유지된다."""
        if not self._in_entry_window(current_time):
            return False
        is_short_entry = side == "short"
        open_same_side = sum(
            1 for t in Trade.get_open_trades() if t.is_short == is_short_entry
        )
        return open_same_side < self.top_k.value
