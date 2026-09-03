# -*- coding: utf-8 -*-
"""
하방 변동성 사이징 — 확정판
================================================================

검증을 통과한 단 하나의 설정. 파라미터는 고정이며, 바꾸려면 재검증이 필요하다.

    포지션 = clip(scale / 하방변동성_30일, 0, 1.0)

  - 하방 변동성 = sqrt(mean(min(r,0)^2)) over 30 days
  - scale 은 과거 데이터로만 재추정 (평균 노출도가 EXPOSURE 가 되도록)
  - 방향 예측이 아니다. "얼마나 실을지" 만 정한다.

검증 요약 (고정레버 대조군 대비 Sharpe)
  BTC 전체 +0.040 / ETH +0.097 / BTC 후반 +0.040   (walk-forward 기준)
  burn-in × 재추정주기 28조합 × 3검증 = 84칸 전부 양수
  창 길이 25~40 고원 확인 (그 밖은 불안정)

*** 반드시 읽을 것: 우위는 국면에 고르게 분포하지 않는다 ***

  국면별 총수익 (B&H / 고정레버 대조군 / 확정판)

    BTC 2022 폭락    -72.8% / -67.4% / -71.2%   <- 대조군에 패
    BTC 2023-24 상승  563.6% / 532.3% / 562.7%   <- 승
    BTC 2025-26 하락  -40.3% / -37.4% / -37.8%   <- 미세 패
    ETH 2022 폭락    -72.4% / -66.0% / -68.9%   <- 대조군에 패
    ETH 2023-24 상승  226.3% / 218.4% / 242.9%   <- 승
    ETH 2025-26 하락  -50.8% / -46.0% / -47.8%   <- 미세 패

  하락 국면 4곳에서 전부 대조군에 진다. 전체 우위는 상승장에서 나온 것이다.
  이유: 하방변동성은 '이미 떨어진 뒤' 포지션을 줄이고,
        폭락 후 변동성이 높게 유지되는 동안의 급반등을 놓친다.
        하락장은 상시 고변동이라 사실상 고정레버와 같아지고, 반등 미참여분만 손해다.

  => 이것은 낙폭 방어 전략이 아니라 '상승장 안에서 조정을 피하는' 전략이다.
     현재처럼 고점 대비 크게 하락한 국면에서는 고정레버 0.85 와 실질적으로 같다.
     regime_table() 로 직접 재현할 수 있다.

기각된 것들: LSTM 가격·방향·변동성 예측, 전체(하방 아닌) 변동성 사이징,
            MAXLEV > 1.0, 하방+추세 결합(파라미터 3개, ETH에서 열세)

사용법
    python dvol_strategy.py                  # 오늘 포지션 + 검증 재현
    python dvol_strategy.py BTC/KRW ETH/KRW  # 종목 지정
"""
import sys
import numpy as np
import pandas as pd
import FinanceDataReader as fdr

# ── 확정 파라미터 (재검증 없이 변경 금지) ──────────────────────
WINDOW   = 30      # 하방 변동성 창. 25~40 고원의 중앙
MAXLEV   = 1.0     # 축소만. 1.0 초과는 MDD 를 악화시킴 (검증됨)
EXPOSURE = 0.85    # 목표 평균 노출도. 위험선호도 파라미터
BURN     = 252     # scale 추정 최소 기간 (민감도 없음)
REBAL    = 30      # scale 재추정 주기 (민감도 없음)
FEE      = 0.0005  # 편도 수수료 0.05%
# ──────────────────────────────────────────────────────────────


def load(ticker, start='2021-01-01', end=None):
    df = fdr.DataReader(ticker, start, end)
    df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])   # 당일 미확정 행 제거
    return df[['Open', 'High', 'Low', 'Close', 'Volume']]


def downside_vol(close, window=WINDOW):
    """하방 변동성. shift(1) 로 미래 정보를 차단한다 — 절대 빼지 말 것."""
    r = np.log(close / close.shift(1))
    neg = r.clip(upper=0)
    return np.sqrt((neg ** 2).rolling(window).mean()).shift(1)


def _scale(inv, target=EXPOSURE, maxlev=MAXLEV, iters=60):
    """clip 후 평균 레버리지가 target 이 되는 스케일 (이분법)"""
    lo, hi = 1e-8, 1e8
    for _ in range(iters):
        mid = np.sqrt(lo * hi)
        if np.clip(mid * inv, 0, maxlev).mean() < target:
            lo = mid
        else:
            hi = mid
    return np.sqrt(lo * hi)


def leverage(inv, burn=BURN, rebal=REBAL):
    """walk-forward: 매 시점 과거 데이터로만 scale 재추정"""
    n = len(inv)
    lev = np.full(n, np.nan)
    sc = None
    for t in range(n):
        if t < burn:
            continue
        if sc is None or (t - burn) % rebal == 0:
            sc = _scale(inv[:t])              # t 시점까지만 사용
        lev[t] = min(sc * inv[t], MAXLEV)
    return lev


def _metrics(ret, name, lev=None):
    eq = np.cumprod(1 + ret)
    peak = np.maximum.accumulate(eq)
    yrs = len(ret) / 365
    dn = ret[ret < 0]
    return dict(전략=name,
                총수익=eq[-1] - 1,
                CAGR=eq[-1] ** (1 / yrs) - 1 if eq[-1] > 0 else -1.0,
                연변동성=ret.std() * np.sqrt(365),
                MDD=((eq - peak) / peak).min(),
                Sharpe=ret.mean() / (ret.std() + 1e-12) * np.sqrt(365),
                Sortino=ret.mean() / (dn.std() + 1e-12) * np.sqrt(365),
                평균레버=float(np.mean(lev)) if lev is not None else 1.0)


def backtest(ticker, frac=0.0):
    """확정 전략을 walk-forward 로 백테스트. 고정레버 대조군과 함께 반환."""
    c = load(ticker).Close
    d = pd.DataFrame({'dv': downside_vol(c),
                      'nxt': np.log(c / c.shift(1)).shift(-1)}).dropna()
    inv = 1.0 / np.clip(d['dv'].to_numpy(), 1e-6, None)
    lev = leverage(inv)

    ok = ~np.isnan(lev)
    if frac:
        idx = np.where(ok)[0]
        ok[:] = False
        ok[idx[int(len(idx) * frac):]] = True

    simple = np.exp(d['nxt'].to_numpy()[ok]) - 1
    lv = lev[ok]
    net = lv * simple - FEE * np.abs(np.diff(np.concatenate([[0.], lv])))

    # 대조군은 같은 평균 노출도로 맞춘다 (타이밍 효과만 비교)
    flat = np.full(len(simple), lv.mean())
    rows = [_metrics(simple, 'Buy & Hold'),
            _metrics(flat * simple, f'고정레버 {lv.mean():.2f} (대조군)', flat),
            _metrics(net, '하방변동성 사이징 (확정판)', lv)]
    return pd.DataFrame(rows), d.index[ok]


def position_today(ticker):
    """오늘 잡아야 할 포지션 (0=전량현금, 1.0=풀매수)"""
    c = load(ticker).Close
    dv = downside_vol(c).dropna()
    inv = 1.0 / np.clip(dv.to_numpy(), 1e-6, None)
    sc = _scale(inv[:-1])                     # 어제까지의 데이터로 scale 추정
    return float(np.clip(sc * inv[-1], 0, MAXLEV)), float(dv.iloc[-1]), dv.index[-1]


REGIMES = [('2022 폭락',       '2021-11-10', '2022-12-31'),
           ('2023-24 상승',    '2023-01-01', '2024-12-31'),
           ('2025-26 하락',    '2025-10-06', '2030-01-01')]


def regime_table(ticker):
    """국면별 성적. 우위가 어디서 나오는지 확인하는 용도."""
    c = load(ticker).Close
    d = pd.DataFrame({'dv': downside_vol(c),
                      'nxt': np.log(c / c.shift(1)).shift(-1)}).dropna()
    inv = 1.0 / np.clip(d['dv'].to_numpy(), 1e-6, None)
    lev = leverage(inv)
    base = ~np.isnan(lev)

    def tot(x):
        return np.cumprod(1 + x)[-1] - 1

    def mdd(x):
        eq = np.cumprod(1 + x)
        return ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()

    rows = []
    for nm, s, e in REGIMES + [('전체', '2000-01-01', '2030-01-01')]:
        m = base & (d.index >= s) & (d.index <= e)
        if m.sum() < 30:
            continue
        simple = np.exp(d['nxt'].to_numpy()[m]) - 1
        lv = lev[m]
        net = lv * simple - FEE * np.abs(np.diff(np.concatenate([[0.], lv])))
        ctl = np.full(len(simple), lv.mean()) * simple
        rows.append(dict(국면=nm, 일수=int(m.sum()),
                         BH=tot(simple), 대조군=tot(ctl), 확정판=tot(net),
                         판정='승' if tot(net) > tot(ctl) else '패',
                         BH_MDD=mdd(simple), 확정_MDD=mdd(net), 평균레버=lv.mean()))
    return pd.DataFrame(rows)


def _show(T, title):
    f = T.copy()
    for x in ['총수익', 'CAGR', '연변동성', 'MDD']:
        f[x] = f[x].map(lambda v: f"{v:.1%}")
    for x in ['Sharpe', 'Sortino', '평균레버']:
        f[x] = f[x].map(lambda v: f"{v:.2f}")
    print(f"\n{title}")
    print('-' * 96)
    print(f.to_string(index=False))
    print(f"  대조군 대비:  Sharpe {T.iloc[2].Sharpe - T.iloc[1].Sharpe:+.3f}"
          f"   Sortino {T.iloc[2].Sortino - T.iloc[1].Sortino:+.3f}")


def main(tickers):
    print(__doc__.split('사용법')[0].rstrip())
    print('=' * 96)
    print('오늘의 포지션')
    print('=' * 96)
    for tk in tickers:
        pos, dv, date = position_today(tk)
        bar = '#' * int(round(pos * 40))
        print(f"  {tk:9s} {date.date()}  하방변동성 {dv:.4f}   포지션 {pos:.2f}  |{bar:<40s}|")

    print()
    print('=' * 96)
    print('검증 재현 (walk-forward)')
    print('=' * 96)
    for tk in tickers:
        T, idx = backtest(tk)
        _show(T, f"[{tk}]  n={len(idx)}  {idx[0].date()} ~ {idx[-1].date()}")
    T, idx = backtest(tickers[0], frac=0.5)
    _show(T, f"[{tickers[0]} 후반 50%]  n={len(idx)}  {idx[0].date()} ~ {idx[-1].date()}")

    print()
    print('=' * 96)
    print('국면별 성적 — 우위가 어디서 나오는가')
    print('=' * 96)
    for tk in tickers:
        R = regime_table(tk)
        f = R.copy()
        for x in ['BH', '대조군', '확정판', 'BH_MDD', '확정_MDD']:
            f[x] = f[x].map(lambda v: f"{v:.1%}")
        f['평균레버'] = f['평균레버'].map(lambda v: f"{v:.2f}")
        print(f"\n[{tk}]")
        print(f.to_string(index=False))

    print()
    print('하락 국면에서는 확정판이 고정레버 대조군에 진다.')
    print('전체 우위는 상승장 구간에서 나온 것이다 — 낙폭 방어 전략이 아니다.')
    print('하방변동성은 이미 떨어진 뒤에 포지션을 줄이고, 이후 급반등을 놓치기 때문이다.')
    print()
    print('검증 구간(2021~2026)은 2022 폭락(BTC -63%)과 2025-26 하락(고점 대비 -40%)을')
    print('포함한다. 국면 커버리지는 충분하나, 자산은 2개뿐이다.')


if __name__ == '__main__':
    main(sys.argv[1:] or ['BTC/KRW', 'ETH/KRW'])
