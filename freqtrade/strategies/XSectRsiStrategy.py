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
이미 다른 봇/수동 포지션이 있는 종목에는 진입하지 않는다 - 이 처리는
2026-10-17부터 부모(XSectMomentumStrategy)가 두 봇 공통으로 한다.
"""
from typing import Optional

import numpy as np

from XSectMomentumStrategy import XSectMomentumStrategy


class XSectRsiStrategy(XSectMomentumStrategy):
    rsi_period = 14

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
