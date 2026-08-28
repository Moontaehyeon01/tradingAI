#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
바이낸스 선물 신호 감시 -> 텔레그램 알림

무엇을 하는가
  바이낸스 USDⓈ-M 무기한 선물의 1시간봉을 직접 읽어서 '박스권 돌파' 신호를
  찾고, 새 신호가 뜨면 텔레그램으로 알려준다.

무엇을 하지 않는가
  주문을 내지 않는다. 알림 전용이다. 거래소 API 키가 필요 없고(공개 시세
  엔드포인트만 사용), 따라서 이 프로그램이 돈을 움직일 방법 자체가 없다.

왜 박스권 돌파만 보는가
  2026-08-27 검증에서 RSI 다이버전스 / 쌍바닥 / 이평선 접촉 / 프랙탈 /
  허스트 지수 등은 전부 비용을 넘지 못했다(72회 검정, 통과 0건).
  무작위 진입 대조군을 통과한 유일한 신호가 박스권 돌파였다.
    무작위 진입 -0.063%/거래  vs  박스 돌파 +0.269%/거래 (차이 p=0.001)
  엣지가 없다고 확인된 지표로 알림을 보내면 노이즈만 쌓이므로 넣지 않았다.
  다른 신호를 추가하려면 SIGNALS 아래에 함수를 하나 더 붙이면 된다.

사용법
  python tools/signal_alert.py --test      텔레그램 연결만 확인
  python tools/signal_alert.py --status    현재 박스 상태 출력(알림 안 보냄)
  python tools/signal_alert.py --once      1회 스캔 후 종료
  python tools/signal_alert.py --loop      매시간 봉 마감 직후 자동 스캔
  옵션: --top 30                 감시할 거래대금 상위 심볼 수
        --symbols BTCUSDT,ETHUSDT  직접 지정(--top 무시)
        --no-telegram            콘솔에만 출력
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"

# --- 신호 파라미터: 서버에서 돌고 있는 BoxBreakoutStrategy와 동일하게 맞춤 ---
BOX_PERIOD = 24        # 박스를 구성할 직전 봉 수 (1시간봉 24개 = 24시간)
BOX_MAX_WIDTH = 0.03   # 박스 폭 상한. 이보다 넓으면 '횡보'로 보지 않음
STOP_PCT = 0.015       # 참고용 손절폭(가격 기준)
MAX_HOLD_H = 48        # 참고용 최대 보유시간

NEAR_MISS_WIDTH = 0.04  # 아직 조건은 아니지만 '근접'으로 볼 상한

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "tools", ".signal_alert_state.json")
ALERT_COOLDOWN_H = 12   # 같은 심볼+방향 재알림 최소 간격


# ---------------------------------------------------------------- 설정 읽기
def load_env():
    """.env 를 읽어 환경변수로 올린다(이미 설정돼 있으면 그대로 둠)."""
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def tg_config():
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get(
        "FREQTRADE__TELEGRAM__TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get(
        "FREQTRADE__TELEGRAM__CHAT_ID", "")
    return token, chat


# ---------------------------------------------------------------- HTTP
def api(path, **params):
    url = FAPI + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signal-alert/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))   # 429 등은 잠깐 쉬고 재시도
    raise last


def send_telegram(text, quiet=False):
    token, chat_id = tg_config()
    if not token or not chat_id:
        print("[텔레그램] 토큰/챗ID가 없습니다. .env 의 FREQTRADE__TELEGRAM__* 를 확인하세요.")
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "disable_notification": "true" if quiet else "false",
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("ok", False)
    except Exception as exc:  # noqa: BLE001
        print("[텔레그램] 전송 실패: " + str(exc))
        return False


# ---------------------------------------------------------------- 심볼/시세
def top_symbols(n):
    """24시간 거래대금 상위 USDT 무기한 심볼."""
    ex = api("/fapi/v1/exchangeInfo")
    perp = set()
    for s in ex["symbols"]:
        if (s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
                and s["symbol"].endswith("USDT")):
            perp.add(s["symbol"])
    tick = api("/fapi/v1/ticker/24hr")
    rows = [t for t in tick if t["symbol"] in perp]
    rows.sort(key=lambda t: -float(t["quoteVolume"]))
    return [t["symbol"] for t in rows[:n]]


def klines(symbol, limit=BOX_PERIOD + 30):
    """1시간봉. 진행 중인 마지막 봉은 버리고 '마감된 봉'만 돌려준다."""
    raw = api("/fapi/v1/klines", symbol=symbol, interval="1h", limit=limit)
    now_ms = time.time() * 1000
    out = []
    for k in raw:
        if k[6] > now_ms:      # 아직 안 끝난 봉
            continue
        out.append({
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]), "close_time": k[6],
        })
    return out


# ---------------------------------------------------------------- 신호
def box_breakout(candles):
    """
    박스권 돌파 판정.

    마지막 '마감된' 봉을 판정 대상으로 하고, 박스는 그 직전 24봉으로 만든다.
    현재 봉을 박스 계산에 넣으면 돌파와 동시에 박스가 넓어져 판정이 순환하므로
    반드시 제외한다(라이브 전략의 shift(1) 과 같은 처리).
    """
    if len(candles) < BOX_PERIOD + 1:
        return None
    cur = candles[-1]
    box = candles[-(BOX_PERIOD + 1):-1]
    hi = max(c["high"] for c in box)
    lo = min(c["low"] for c in box)
    if lo <= 0:
        return None
    width = (hi - lo) / lo

    vols = [c["volume"] for c in box]
    mean_v = sum(vols) / len(vols)
    var = sum((v - mean_v) ** 2 for v in vols) / max(len(vols) - 1, 1)
    sd = var ** 0.5
    vol_z = (cur["volume"] - mean_v) / sd if sd > 0 else 0.0

    is_box = width <= BOX_MAX_WIDTH
    side = None
    if is_box and cur["close"] > hi:
        side = "LONG"
    elif is_box and cur["close"] < lo:
        side = "SHORT"

    pos = (cur["close"] - lo) / (hi - lo) * 100 if hi > lo else float("nan")
    return {
        "side": side, "is_box": is_box, "width": width,
        "box_high": hi, "box_low": lo, "close": cur["close"],
        "vol_z": vol_z, "close_time": cur["close_time"], "pos": pos,
    }


SIGNALS = {"box_breakout": box_breakout}


# ---------------------------------------------------------------- 상태 저장
def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_PATH)


def should_alert(state, symbol, side, close_time):
    """같은 심볼+방향을 쿨다운 안에서 반복 알림하지 않는다."""
    prev = state.get(symbol + ":" + side)
    if prev is None:
        return True
    if prev.get("close_time") == close_time:      # 같은 봉 재처리
        return False
    age_h = (close_time - prev.get("close_time", 0)) / 3600000.0
    return age_h >= ALERT_COOLDOWN_H


# ---------------------------------------------------------------- 출력
def fmt_price(x):
    if x >= 1000:
        return "{:,.2f}".format(x)
    if x >= 1:
        return "{:,.4f}".format(x)
    return "{:.6f}".format(x)


def alert_text(symbol, r):
    side_kr = "매수 (롱)" if r["side"] == "LONG" else "매도 (숏)"
    emoji = "\U0001F7E2" if r["side"] == "LONG" else "\U0001F534"
    stop = (r["close"] * (1 - STOP_PCT) if r["side"] == "LONG"
            else r["close"] * (1 + STOP_PCT))
    when = datetime.fromtimestamp(r["close_time"] / 1000, timezone.utc)
    return (
        emoji + " <b>" + side_kr + "</b>  <code>" + symbol + "</code>\n\n"
        + "박스권 " + fmt_price(r["box_low"]) + " ~ " + fmt_price(r["box_high"])
        + "  (폭 {:.2f}%)\n".format(r["width"] * 100)
        + "돌파 종가 <b>" + fmt_price(r["close"]) + "</b>\n"
        + "거래량 z-score {:+.1f}\n\n".format(r["vol_z"])
        + "참고 손절 " + fmt_price(stop)
        + " ({:.1f}%)\n".format(-STOP_PCT * 100)
        + "참고 보유 최대 {}시간\n\n".format(MAX_HOLD_H)
        + "<i>" + when.strftime("%Y-%m-%d %H:%M")
        + " UTC 봉 마감 기준 · 알림 전용, 자동주문 아님</i>"
    )


def strip_html(t):
    for tag in ("<b>", "</b>", "<code>", "</code>", "<i>", "</i>"):
        t = t.replace(tag, "")
    return t


def scan(symbols, notify=True, verbose=True):
    state = load_state()
    hits, near, rows = [], [], []

    for i, sym in enumerate(symbols):
        try:
            r = box_breakout(klines(sym))
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print("  {:<14} 조회 실패: {}".format(sym, exc))
            continue
        if r is None:
            continue
        rows.append((sym, r))
        if r["side"]:
            hits.append((sym, r))
        elif r["width"] <= NEAR_MISS_WIDTH:
            near.append((sym, r))
        if i % 10 == 9:
            time.sleep(0.3)      # 레이트리밋 여유

    if verbose:
        rows.sort(key=lambda x: x[1]["width"])
        print("\n{:<14}{:>9}{:>8}{:>9}{:>14}  신호".format(
            "심볼", "박스폭", "박스권", "위치", "종가"))
        for sym, r in rows:
            pos = "{:.0f}%".format(r["pos"]) if r["pos"] == r["pos"] else "-"
            print("{:<14}{:>8.2f}%{:>8}{:>9}{:>14}  {}".format(
                sym, r["width"] * 100, "O" if r["is_box"] else "X",
                pos, fmt_price(r["close"]), r["side"] or ""))

    sent = 0
    for sym, r in hits:
        if not should_alert(state, sym, r["side"], r["close_time"]):
            if verbose:
                print("  (쿨다운으로 생략: {} {})".format(sym, r["side"]))
            continue
        text = alert_text(sym, r)
        if notify:
            if send_telegram(text):
                sent += 1
                state[sym + ":" + r["side"]] = {"close_time": r["close_time"]}
        else:
            print("\n--- 알림 미리보기 ---")
            print(strip_html(text))
    save_state(state)

    n_box = len([1 for _, r in rows if r["is_box"]])
    print("\n스캔 {}개 · 신호 {}건 · 알림전송 {}건 · 박스권 형성중 {}개".format(
        len(rows), len(hits), sent, n_box))
    if near and verbose:
        near.sort(key=lambda x: x[1]["width"])
        tip = ", ".join("{}({:.1f}%)".format(s, r["width"] * 100) for s, r in near[:5])
        print("근접(폭 {:.0f}% 이내): {}".format(NEAR_MISS_WIDTH * 100, tip))
    return hits


def seconds_to_next_hour(offset=20):
    """다음 정시 + offset초까지 남은 시간. 봉이 확실히 마감된 뒤 조회하려고 여유를 둔다."""
    return (3600 - (time.time() % 3600)) + offset


def main():
    load_env()
    ap = argparse.ArgumentParser(description="바이낸스 신호 감시 -> 텔레그램 알림")
    ap.add_argument("--test", action="store_true", help="텔레그램 연결 테스트")
    ap.add_argument("--status", action="store_true", help="현재 상태만 출력(알림 안 보냄)")
    ap.add_argument("--once", action="store_true", help="1회 스캔 후 종료")
    ap.add_argument("--loop", action="store_true", help="매시간 봉 마감 후 자동 스캔")
    ap.add_argument("--top", type=int, default=30, help="감시할 거래대금 상위 심볼 수")
    ap.add_argument("--symbols", type=str, default="", help="쉼표로 직접 지정")
    ap.add_argument("--no-telegram", action="store_true", help="콘솔에만 출력")
    a = ap.parse_args()

    if a.test:
        ok = send_telegram(
            "✅ <b>신호 알림 연결 확인</b>\n\n"
            "바이낸스 박스권 돌파 감시 프로그램이 텔레그램에 연결되었습니다.\n"
            "<i>알림 전용이며 자동 주문 기능은 없습니다.</i>")
        print("텔레그램 전송 성공" if ok else "텔레그램 전송 실패")
        return 0 if ok else 1

    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        syms = top_symbols(a.top)
    print("감시 대상 {}개: {}{}".format(
        len(syms), ", ".join(syms[:8]), " ..." if len(syms) > 8 else ""))
    print("조건: 직전 {}봉 박스 폭 <= {:.0f}% 이고 종가가 박스 이탈".format(
        BOX_PERIOD, BOX_MAX_WIDTH * 100))

    notify = not (a.no_telegram or a.status)

    if a.status or a.once:
        scan(syms, notify=notify)
        return 0

    if a.loop:
        print("\n루프 시작 — 매시간 봉 마감 직후 스캔합니다. (Ctrl+C 로 종료)")
        scan(syms, notify=notify)          # 시작 시 1회
        while True:
            wait = seconds_to_next_hour()
            nxt = datetime.fromtimestamp(time.time() + wait, timezone.utc)
            print("\n다음 스캔 {} UTC ({:.1f}분 후)".format(
                nxt.strftime("%H:%M:%S"), wait / 60))
            time.sleep(wait)
            try:
                scan(syms, notify=notify)
            except Exception as exc:  # noqa: BLE001
                print("[스캔 오류] " + str(exc))

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
