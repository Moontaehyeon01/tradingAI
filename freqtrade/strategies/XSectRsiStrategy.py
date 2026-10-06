# -*- coding: utf-8 -*-
"""
XSectRsiStrategy — 횡단면 RSI 순위 (시장중립 롱숏)
==================================================

XSectMomentumStrategy 와 규칙이 전부 같고 **순위 점수만** 다르다.
  - 순위: 어제 마감 종가 기준 RSI(14) (14일 수익률 대신)
    RSI 높은 순 롱 3 / 낮은 순 숏 3
  - 나머지(보유 3일 + 순위권이면 1일 연장/최대 4일, 변동성 익절 x2.0,
    48시간 재진입 대기, 09:00~10:00 KST에만 진입, 방향별 최대 3개)는 부모 그대로.

RSI는 "최근 14일 움직임 중 오른 폭의 비중"이라, 같은 14일 상승이라도
하루 급등으로 오른 종목보다 꾸준히 오른 종목을 위로 올린다.
백테스트와 똑같이 단순평균(Cutler) 방식으로 계산한다(Wilder 평활 아님).

  2026-10-06 비교 백테스트 (고정 19개, 2020~2026, 왕복 0.10%, 현재 실전 규칙)
                     14일 수익률   RSI(14)
    샤프               2.23         2.72
    최대낙폭           -22%         -16%
    수수료 0.3% 샤프   1.62         2.08
    연도별 우세        -            7년 중 6년 (2023만 열세)
  한계: 14일이 RSI 기간 중 튀는 값(이웃 기간 평균 2.14)이라 실전 기대치는 2.1~2.4.
        부트스트랩으로 현재보다 나을 확률 93.6% (95% 기준엔 못 미침).
        거래대금 상위 N개 유니버스에선 현재 방식과 2승 2패 1무.

같은 바이낸스 계좌(잔고 공유)에서 XSectMomentum 과 함께 돌 수 있다.
바이낸스 단방향 모드에선 같은 종목 포지션이 하나로 합쳐지므로, 계좌에
이미 다른 봇/수동 포지션이 있는 종목에는 진입하지 않는다(_foreign_pairs).
그 종목이 순위권이면 다음 순위가 대신 후보가 된다(재진입 대기와 같은 방식).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from freqtrade.persistence import Trade

from XSectMomentumStrategy import XSectMomentumStrategy

logger = logging.getLogger(__name__)


class XSectRsiStrategy(XSectMomentumStrategy):
    rsi_period = 14
    # 계좌 포지션 조회 주기 - 진입 판단(매 루프)마다 거래소를 부르지 않게 캐시한다.
    FOREIGN_REFRESH = timedelta(seconds=60)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # 이 봇 거래가 아닌데 계좌에 열려 있는 포지션의 페어(다른 봇/수동)
        self._foreign_pairs: set = set()
        self._foreign_checked_at: Optional[datetime] = None
        self._foreign_ok = False

    def _rank_score(self, closes: np.ndarray) -> Optional[float]:
        n = self.rsi_period
        if len(closes) < n + 1:
            return None
        diff = np.diff(closes[-(n + 1):])
        if not np.all(np.isfinite(diff)):
            return None
        gain = diff.clip(min=0).mean()
        loss = (-diff).clip(min=0).mean()
        if gain == 0 and loss == 0:
            return None  # 14일 내내 가격이 안 움직임 - 순위를 매길 근거가 없다
        if loss == 0:
            return 100.0
        return float(100.0 - 100.0 / (1.0 + gain / loss))

    # ------------------------------------------------------------------
    # 같은 계좌의 다른 포지션과 겹치지 않게
    # ------------------------------------------------------------------
    def _account_positions(self, pair: Optional[str] = None) -> set:
        """계좌에 열린 포지션의 페어 중 이 봇 거래가 아닌 것. 조회 실패 시 예외."""
        positions = self.dp._exchange.fetch_positions(pair)
        own = {t.pair for t in Trade.get_open_trades()}
        return {
            p["symbol"] for p in positions
            if p.get("symbol") and float(p.get("contracts") or 0) != 0
        } - own

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        if (self._foreign_checked_at is None
                or current_time - self._foreign_checked_at >= self.FOREIGN_REFRESH):
            self._foreign_checked_at = current_time
            try:
                self._foreign_pairs = self._account_positions()
                self._foreign_ok = True
            except Exception as exc:  # noqa: BLE001
                # 직전 목록은 유지하되 진입은 confirm_trade_entry 에서 막는다
                self._foreign_ok = False
                logger.warning("계좌 포지션 조회 실패 - 신규 진입 보류: %s", exc)
        super().bot_loop_start(current_time, **kwargs)

    def _is_blocked(self, pair: str, side: str, now: datetime) -> bool:
        # 다른 포지션이 있는 종목은 건너뛰고 다음 순위로 채운다
        return pair in self._foreign_pairs or super()._is_blocked(pair, side, now)

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time: datetime,
                             entry_tag: Optional[str], side: str, **kwargs) -> bool:
        if not super().confirm_trade_entry(pair, order_type, amount, rate, time_in_force,
                                           current_time, entry_tag, side, **kwargs):
            return False
        if not self._foreign_ok:
            return False
        # 캐시(최대 60초) 사이에 다른 봇이 같은 종목을 잡았을 수 있어 주문 직전에 다시 확인
        try:
            if self._account_positions(pair):
                logger.info("%s: 계좌에 다른 포지션이 있어 진입 안 함", pair)
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: 계좌 포지션 조회 실패 - 진입 안 함: %s", pair, exc)
            return False
        return True
