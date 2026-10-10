#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard/server.py
--------------------
freqtrade REST API를 감싸는 얇은 프록시 겸 정적 파일 서버.
브라우저는 이 서버에만 붙고, 실제 freqtrade 인증 정보는 서버 쪽(.env)에만 존재함.

실행:
    pip install -r dashboard/requirements.txt
    python dashboard/server.py
    -> http://localhost:5000 접속
"""

import hashlib
import hmac
import json
import os
import re
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, Response, send_from_directory
from dotenv import load_dotenv

import predictor

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent.parent / ".env")

USERNAME = os.environ.get("FREQTRADE__API_SERVER__USERNAME", "freqtrader")
PASSWORD = os.environ.get("FREQTRADE__API_SERVER__PASSWORD", "")

# 대시보드 자체 로그인 (freqtrade 봇 인증과는 별개 - 인터넷에 공개할 때 반드시 필요)
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD가 .env에 설정되어 있지 않습니다. "
        "비밀번호 없이는 대시보드를 실행하지 않습니다 (인터넷 공개 시 위험)."
    )

# 바이낸스 계좌 직접 조회용(읽기 전용으로만 사용).
# freqtrade API는 "봇이 아는 거래"만 알려주기 때문에, 사용자가 앱/웹에서 직접 잡은
# 포지션이나 걸어둔 미체결 주문은 봇 쪽에서 보이지 않는다. 그 부분만 거래소에
# 직접 물어본다. 주문 생성/취소는 하지 않고 조회 엔드포인트만 호출한다.
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BINANCE_FAPI = "https://fapi.binance.com"

TELEGRAM_BOT_TOKEN = os.environ.get("FREQTRADE__TELEGRAM__TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID", "")
# freqtrade 컨테이너가 이 엔드포인트를 부를 때 쓰는 공유 비밀값 (대시보드 로그인 계정/비밀번호와는 별개,
# 그리고 이 엔드포인트는 인터넷에 열려있는 /webhook 경로라서 아무나 못 부르게 막는 용도)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# AI 시황 의견(참고용, 자동매매와 무관) 생성에 쓴다. 키가 없으면 그 페이지만
# "설정 필요" 상태로 비활성화되고 나머지 대시보드는 그대로 동작한다.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


BOTS = [
    # 주의: 거래당 실효배율 = (tradable_balance_ratio / max_open_trades) x leverage.
    # 슬롯 수를 줄이면 레버리지를 안 건드려도 거래당 위험이 올라간다.
    # 2026-09-03 기준: (0.5 / 2) x 3 = 0.75배.
    #
    # max_hold_h / box_max_width / lookback / top_k 는 전략 파라미터라 freqtrade
    # API로는 안 나온다. 화면 표시용으로 여기 적어둔다. 전략 파라미터를 바꾸면
    # 이 값도 같이 맞춰야 한다.
    #
    # signal_kind: 진입 조건 현황 패널이 봇마다 다른 방식으로 그려야 해서 붙인
    # 태그다. "box"는 박스권 돌파(박스 범위/폭/돌파까지 거리), "xsect_momentum"은
    # 순위 기반(전체 페어 수익률 순위/롱·숏 후보) - 컬럼 구성 자체가 다르다.
    # XSectMomentum 과 같은 규칙에 순위만 RSI(14) (XSectRsiStrategy). 2026-10-06
    # 박스돌파 V2 자리(8086)를 대신함. rank_score 로 진입조건 패널의 순위 계산을 바꾼다.
    {"id": "xsectrsi", "name": "RSI (RSI 순위)",
     "url": "http://127.0.0.1:8086", "leverage": 2,
     "max_hold_h": 72, "signal_kind": "xsect_momentum",
     "timeframe": "1d", "lookback": 14, "top_k": 3,
     "rank_score": "rsi", "rsi_period": 14,
     # 계좌에 다른 봇/수동 포지션이 있는 종목은 건너뜀(전략 _foreign_pairs) -
     # 진입조건 패널도 같은 종목을 건너뛰고 다음 순위를 후보로 표시한다.
     "skip_foreign_positions": True,
     "take_profit_vol_scaled": True,
     "hold_extends_if_ranked": True, "max_hold_hard_h": 96,
     "reentry_cooldown_h": 48},
    {"id": "xsectmomentum", "name": "Momentum (횡단면 모멘텀)",
     "url": "http://127.0.0.1:8087", "leverage": 2,
     "max_hold_h": 72, "signal_kind": "xsect_momentum",
     "timeframe": "1d", "lookback": 14, "top_k": 3,
     # 익절폭이 고정값이 아니라 종목별 변동성 스케일링이라(전략의
     # take_profit_vol_mult/window/min/max 참고) 여기 상수 하나로는 표시를
     # 못 한다 - exit_targets() 가 이 플래그를 보고 _xsect_vol_scaled_tp() 로
     # 종목별로 직접 계산한다. 전략의 저 네 파라미터를 바꾸면 아래 함수의
     # 같은 이름 상수도 같이 맞출 것.
     "take_profit_vol_scaled": True,
     # 보유기간(max_hold_h)이 지나도 그날 같은 방향 순위권이면 하루 더 보유하고
     # max_hold_hard_h 에는 무조건 청산한다(전략 hold_days/max_hold_days).
     # exit_targets() 가 연장 중이면 상한까지 남은 시간을 따로 표시한다.
     "hold_extends_if_ranked": True, "max_hold_hard_h": 96,
     # 전략 reentry_cooldown_hours 와 같은 값 - 진입조건 패널이 재진입 대기 종목을
     # 건너뛰고 다음 순위를 후보로 표시하는 데 쓴다.
     "reentry_cooldown_h": 48},
    # 박스돌파 V2(8086)는 2026-10-06 XSectRSI 로 교체하며 없앰(실거래 10건, -8.99 USDT,
    # DB user_data/boxbreakout_v2_live.sqlite 는 서버에 남겨둠).
    # v1은 2026-08-28 격자탐색 결과 v2 조합(박스12봉/폭4%/익절35%/48h)이 학습·홀드아웃
    # 양쪽에서 더 나아서 중단함. 되살리려면 아래 줄과 docker compose 서비스를 함께.
    # {"id": "boxbreakout", "name": "BoxBreakoutStrategy (박스돌파)", "url": "http://127.0.0.1:8085", "leverage": 5},
    # 아래 두 전략은 2026-08-27 검증에서 진입 신호에 통계적 엣지가 없음이 확인되어
    # BoxBreakoutStrategy로 교체됨. 봇을 다시 띄우면 이 줄들을 되살리면 된다.
    # {"id": "trend", "name": "MultiConfluenceStrategy (추세추종)", "url": "http://127.0.0.1:8083", "leverage": 5},
    # {"id": "meanreversion", "name": "MeanReversionStrategy (평균회귀)", "url": "http://127.0.0.1:8084", "leverage": 5},
]

TICKER_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# 알림 기록 (대시보드 "최근 알림" 패널용). 서비스 재시작해도 유지되도록 파일에도 남김.
NOTIFICATIONS_FILE = ROOT / "notifications.jsonl"
NOTIFICATIONS_MAX = 200
notifications: deque = deque(maxlen=NOTIFICATIONS_MAX)
if NOTIFICATIONS_FILE.exists():
    with open(NOTIFICATIONS_FILE, encoding="utf-8") as f:
        # 파일은 오래된 것부터 한 줄씩 추가돼있음(append). 화면은 항상 최신이 맨 위여야
        # 하므로, 파일을 오래된 순서 그대로 읽으면서 appendleft로 넣으면
        # 마지막(가장 최근) 줄이 맨 앞에 오게 되어 실시간 알림(appendleft)과 순서가 맞음.
        # (예전엔 append를 써서 재시작 후엔 순서가 거꾸로 뒤집히는 버그가 있었음)
        for line in f.readlines()[-NOTIFICATIONS_MAX:]:
            try:
                notifications.appendleft(json.loads(line))
            except json.JSONDecodeError:
                pass


def _notif_dedup_key(pair: str, when_iso: str) -> tuple:
    # 초 단위까지만 비교한다. 웹훅 알림과 backfill_notifications()이 같은
    # 거래를 실제 체결시각(open_date/close_date) 기준으로 기록해야 이 키가
    # 맞아떨어진다 - 예전엔 웹훅 쪽이 "수신 시각"(now())을 썼는데, 그러면
    # 수신 지연 1~2초만으로도 여기서 어긋나 같은 거래 알림이 두 번(게다가
    # config의 bot_name과 대시보드 표시 이름이 서로 달라 마치 다른 봇인
    # 것처럼) 나갔다.
    #
    # 2026-09-21: pair 표기도 통일해야 한다 - freqtrade 봇의 pair는
    # "AAVE/USDT:USDT"(선물 표기)인데, 수동 청산 감지 쪽(fetch_manual_trade_history)
    # 은 같은 거래를 "AAVE/USDT"(선물 접미사 없이)로 만든다. 봇이 리밸런스로
    # 청산한 걸 _bot_closed_keys()가 타이밍상 못 걸러내면(자정 리밸런스 직후
    # 봇 거래 내역이 아직 API에 안 잡힌 순간의 레이스) 같은 청산이 "수동"으로도
    # 잡혀 들어오는데, 이때 pair 표기가 서로 달라 이 dedup 키가 안 겹쳐서
    # 텔레그램이 두 번(봇 알림 1번 + "수동" 알림 1번) 나갔다. ":" 뒤를 잘라
    # 정규화하면 둘 다 같은 키가 되어 두 번째는 dedup에 걸러진다.
    norm_pair = pair.split(":")[0] if pair else pair
    return (norm_pair, when_iso[:19])


# 2026-09-30: pair 표기를 맞추고 나서도(위 2026-09-21 주석) 같은 거래에
# 텔레그램이 두 번 가는 사례가 계속 나왔다(NEAR 등, 9/27~9/29 실측) - 거래소에서
# 직접 청산되면 freqtrade 자신의 실시간 웹훅과 대시보드의 수동감지
# (backfill_notifications, 120초 주기 백그라운드 스레드)가 "동시에" 같은
# 이벤트를 보게 되는데, 그 순간 둘 다 "_notif_seen_keys에 아직 없네" 라고
# 확인만 하고 나서 각자 기록하면(체크와 삽입 사이에 락이 없어서) 둘 다
# 통과해버리는 경쟁 상태였다. 체크+삽입을 하나의 락 안에서 원자적으로
# 처리해야 그중 하나만 "선점"에 성공한다.
_notif_claim_lock = threading.Lock()


def _display_bot_name(name: str) -> str:
    """봇 이름 앞의 "XSect" 를 뗀다(2026-10-06 요청 - 화면/텔레그램 표기 통일).
    freqtrade 설정의 webhook bot_name 은 봇을 재시작해야 바뀌고, 이미 쌓인 알림
    기록에도 옛 이름이 남아 있어서 여기서 일괄로 맞춘다."""
    return re.sub(r"^XSect", "", name or "")


def _claim_notification(pair: str, when_iso: str) -> bool:
    """이 거래를 지금 이 경로가 처음 알린 것인지 원자적으로 확인한다.
    True면 알림을 보내도 된다(이번이 처음) - False면 이미 다른 경로가
    같은 순간 먼저 선점했으니 건너뛴다."""
    key = _notif_dedup_key(pair, when_iso)
    # 같은 청산을 freqtrade(주문 체결 시각)와 수동 감지(실현손익 기록 시각)가 1~2초
    # 다르게 볼 수 있어(2026-10-09) 앞뒤 2초까지 같은 거래로 본다.
    try:
        base = datetime.fromisoformat(key[1])
        near = {(key[0], (base + timedelta(seconds=d)).isoformat()[:19]) for d in range(-2, 3)}
    except ValueError:
        near = {key}
    with _notif_claim_lock:
        if near & _notif_seen_keys:
            return False
        _notif_seen_keys.add(key)
        return True


def _normalize_freqtrade_dt(raw: str) -> str:
    """freqtrade webhook .format() 템플릿이 datetime을 str()로 그대로 박아
    보내주는 형식("2026-09-17 12:24:20.364926+00:00")을 다른 곳에서 쓰는
    ISO 표기("...T...+00:00")로 맞춘다."""
    s = raw.strip().replace(" ", "T", 1)
    if "+" not in s[10:] and not s.endswith("Z"):
        s += "+00:00"
    return s


# backfill_notifications()의 중복판정 기준. notifications(위 deque)는 화면에
# 보여줄 최근 200건만 들고 있는데, 이걸 그대로 dedup 기준으로 쓰면 200건보다
# 오래돼 밀려난 알림은 다음 백필 주기(120초)마다 "새로 발견한 것"으로 착각돼
# 텔레그램이 하루종일 반복 발송되는 버그가 있었다(실제로 겪음). 파일 전체
# (append-only, 무제한 보관)를 기준으로 한 별도 set으로 dedup해야 한다.
_notif_seen_keys: set = set()
if NOTIFICATIONS_FILE.exists():
    with open(NOTIFICATIONS_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("pair") and entry.get("time"):
                _notif_seen_keys.add(_notif_dedup_key(entry["pair"], entry["time"]))


def _resort_notifications() -> None:
    """notifications는 '맨 앞 = 최신'이 불변식인데, backfill이 뒤늦게 발견한
    과거 이력을 파일 끝에 그냥 append(=다음 재시작 때 맨 앞 쪽으로 로드)해버리면
    이 불변식이 깨진다 - 실제로 겪음: 재시작 때 오전 0시대 리밸런싱 기록이
    이미 로드돼있던 낮 12시대 알림보다 앞에 꽂혀서, 프론트의 "새 알림 감지"
    기준시각이 과거로 되돌아가며 이미 재생한 알림음이 계속 반복 재생됐다.
    대량으로 채워 넣은 직후(시작 시 파일 로드, backfill)에만 time 기준으로
    다시 정렬해 이 불변식을 되살린다 - 실시간 웹훅 한 건마다 매번 정렬할
    필요는 없다(그 경로는 애초에 순서대로 들어옴)."""
    ordered = sorted(notifications, key=lambda n: n.get("time") or "", reverse=True)
    notifications.clear()
    notifications.extend(ordered)


_resort_notifications()


def record_notification(entry: dict):
    notifications.appendleft(entry)
    if entry.get("pair") and entry.get("time"):
        _notif_seen_keys.add(_notif_dedup_key(entry["pair"], entry["time"]))
    try:
        with open(NOTIFICATIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="")


# 로그아웃 표시용 쿠키.
# HTTP Basic 인증은 브라우저가 자격증명을 캐시해두고 계속 자동으로 보내기 때문에
# "서버가 잊어버리는" 방식의 로그아웃이 존재하지 않는다. 그래서 이 쿠키가 있으면
# 자격증명이 같이 와도 미인증으로 취급한다. 로그인하면 이 쿠키를 지운다.
LOGOUT_COOKIE = "dash_logged_out"


def _is_authed() -> bool:
    if request.cookies.get(LOGOUT_COOKIE):
        return False
    auth = request.authorization
    return bool(
        auth and auth.username == DASHBOARD_USERNAME and auth.password == DASHBOARD_PASSWORD
    )


def _auth_challenge():
    return Response(
        "인증이 필요합니다.",
        401,
        {"WWW-Authenticate": 'Basic realm="TradeOps Dashboard"'},
    )


@app.before_request
def check_dashboard_auth():
    """
    열람은 로그인 없이, 조작은 로그인해야만.

      - GET/HEAD (대시보드 화면, 조회용 API, 정적 파일) -> 인증 불필요
        단, /api/account 는 예외로 조회도 인증 필수 (미체결 주문이 그대로 드러남)
      - 그 외 메서드(POST 등: 봇 시작/정지) -> 인증 필수
      - /webhook 은 freqtrade 컨테이너가 부르는 경로라 브라우저 로그인과 무관하게
        WEBHOOK_SECRET으로 따로 검증함 (webhook_relay 함수 안에서 체크)
      - /login 은 브라우저 Basic 인증 팝업을 띄우기 위한 전용 경로
      - /api/logout 은 이미 로그아웃된 상태에서 또 눌러도 문제없도록 인증에서 제외
    """
    if request.path == "/webhook":
        return None

    if request.path == "/api/logout":
        return None

    if request.path == "/login":
        # 로그인 시도이므로 로그아웃 표시는 무시하고 자격증명만 본다
        auth = request.authorization
        ok = bool(
            auth
            and auth.username == DASHBOARD_USERNAME
            and auth.password == DASHBOARD_PASSWORD
        )
        if not ok:
            return _auth_challenge()
        return None

    # /api/account 만은 조회라도 로그인 필수.
    # 다른 조회 API는 잔고·손익처럼 "이미 벌어진 일"만 보여주지만, 이건 아직
    # 체결되지 않고 걸어둔 지정가 주문의 가격과 수량이 전부 드러난다.
    # 남이 보면 그대로 앞에서 채갈 수 있는 정보라 열람 자체를 막는다.
    if request.path == "/api/account" and not _is_authed():
        return jsonify({"ok": False, "error": "로그인이 필요합니다."}), 401

    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    if not _is_authed():
        return jsonify({"ok": False, "error": "로그인이 필요합니다."}), 401


@app.get("/login")
def login():
    """브라우저 Basic 인증 팝업을 띄우고, 성공하면 대시보드로 돌려보낸다."""
    resp = Response(
        '<meta charset="utf-8"><meta http-equiv="refresh" content="0; url=/">'
        "로그인되었습니다. 이동 중...",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )
    resp.delete_cookie(LOGOUT_COOKIE, path="/")
    return resp


@app.post("/api/logout")
def logout():
    """
    로그아웃 표시 쿠키를 심는다.

    Basic 인증 특성상 브라우저는 이후에도 자격증명을 계속 보내지만,
    _is_authed()가 이 쿠키를 먼저 확인하고 미인증으로 처리한다.
    다시 로그인하려면 /login 으로 가면 되고, 그때 이 쿠키가 지워진다.
    """
    resp = jsonify({"ok": True, "authenticated": False})
    resp.set_cookie(
        LOGOUT_COOKIE, "1",
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.get("/api/whoami")
def whoami():
    """프론트가 로그인 여부를 알아내 버튼 활성/비활성을 결정하는 데 사용."""
    return jsonify({"authenticated": _is_authed()})


def call_bot(base_url: str, path: str, params: dict | None = None):
    resp = requests.get(
        f"{base_url}{path}",
        params=params or {},
        auth=(USERNAME, PASSWORD),
        timeout=4,
    )
    resp.raise_for_status()
    return resp.json()


# XSectMomentum의 익절폭을 대시보드에서 재현하기 위한 캐시. 전략의
# take_profit_vol_mult(2.0)/window(180)/min(0.05)/max(1.00)와 반드시 같은
# 값을 써야 한다 - 전략을 고치면 여기도 같이 고칠 것.
_XSECT_TP_VOL_MULT = 2.0
_XSECT_TP_VOL_WINDOW = 180
_XSECT_TP_MIN_PCT = 0.05
_XSECT_TP_MAX_PCT = 1.00
_xsect_tp_cache: dict = {}
_XSECT_TP_TTL = 6 * 3600  # 변동성은 하루 사이 크게 안 바뀌니 자주 다시 구할 필요 없다


def _binance_closed_daily_closes(pair: str, limit: int) -> list:
    """바이낸스 선물 일봉 종가(마감된 봉만, 오래된 순). 실패하면 [].
    freqtrade pair_candles 와 같은 기준(마지막 = 가장 최근에 마감된 봉)을 맞춘다."""
    symbol = pair.split(":")[0].replace("/", "")
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit + 1},
            timeout=8,
        )
        resp.raise_for_status()
        now_ms = time.time() * 1000
        return [float(k[4]) for k in resp.json() if k[6] < now_ms]  # k[6] = 봉 마감 시각
    except Exception:  # noqa: BLE001
        return []


def _xsect_vol_scaled_tp(pair: str) -> float | None:
    """전략의 bot_loop_start 와 동일한 계산을 대시보드에서 독립적으로 재현한다
    (전략 내부 상태는 REST API로 안 나와서 직접 다시 구하는 수밖에 없다).
    바이낸스 일봉을 받아 최근 3일 수익률 표준편차 x 배수를 낸다."""
    symbol = pair.split(":")[0].replace("/", "")
    now = time.time()
    cached = _xsect_tp_cache.get(symbol)
    if cached is not None and now - cached["ts"] < _XSECT_TP_TTL:
        return cached["value"]
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1d", "limit": _XSECT_TP_VOL_WINDOW + 5},
            timeout=8,
        )
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
    except Exception:  # noqa: BLE001
        return cached["value"] if cached else None

    # 진행 중인 마지막 봉 제외, 3일 수익률의 표준편차
    closes = closes[:-1]
    if len(closes) < 63:
        value = None
    else:
        ret3 = [(closes[i] / closes[i - 3] - 1.0) for i in range(3, len(closes))]
        vol = statistics.stdev(ret3) if len(ret3) >= 60 else None
        value = (
            max(_XSECT_TP_MIN_PCT, min(_XSECT_TP_VOL_MULT * vol, _XSECT_TP_MAX_PCT))
            if vol and vol > 0
            else None
        )
    _xsect_tp_cache[symbol] = {"ts": now, "value": value}
    return value


def exit_targets(trade: dict, bot: dict, config: dict) -> dict:
    """
    포지션이 '어디서 / 언제' 끝나는지를 계산한다.

    익절가: minimal_roi 는 '레버리지 적용 후 계좌 수익률' 단위라서 가격으로 바꾸려면
            레버리지로 나눠야 한다. ROI 35% + 레버리지 3배 -> 가격 11.7% 이동.
            XSectMomentumStrategy처럼 minimal_roi 로 익절을 안 하고 custom_exit
            안에서 종목별 변동성 스케일링(순수 가격 기준)으로 직접 비교하는
            전략도 있다 - 이런 전략은 freqtrade API에 진짜 익절 조건이
            노출되지 않으므로(minimal_roi 는 "100.0(=10000%)"처럼 절대 안
            닿는 값으로 꺼둔 상태) BOTS 설정의 take_profit_vol_scaled 플래그가
            있으면 _xsect_vol_scaled_tp() 로 종목별로 직접 계산한다. 이것도
            없고 minimal_roi 도 100%를 넘으면 "익절가 없음"으로 둔다(정상
            ROI 목표가 계좌 기준 100%를 넘는 경우는 없으므로, 그 이상은
            사실상 꺼둔 것으로 본다).
    시간청산: 전략의 custom_exit(max_hold_candles)이 담당하는데 freqtrade API로는
            노출되지 않아 BOTS 설정의 max_hold_h 를 쓴다.
    """
    out = {"take_profit_abs": None, "hold_remaining_h": None, "hold_total_h": None,
           "hold_extended": False}

    lev = trade.get("leverage") or bot.get("leverage") or 1
    roi = config.get("minimal_roi") or {}
    roi0 = roi.get("0")
    open_rate = trade.get("open_rate")
    tp_move = None
    if bot.get("take_profit_vol_scaled") and trade.get("pair"):
        tp_move = _xsect_vol_scaled_tp(trade["pair"])
    if tp_move and open_rate:
        out["take_profit_abs"] = (open_rate * (1 - tp_move) if trade.get("is_short")
                                  else open_rate * (1 + tp_move))
    # roi0 >= 1.0(=100%)은 실제 익절 목표가 아니라 "절대 안 닿게" 걸어둔
    # 안전장치용 값으로 본다 - 정상적인 ROI 목표가 계좌 기준 100%를 넘는 경우는 없다.
    elif roi0 and roi0 < 1.0 and open_rate and lev:
        move = float(roi0) / float(lev)          # 계좌 기준 -> 가격 기준
        out["take_profit_abs"] = (open_rate * (1 - move) if trade.get("is_short")
                                  else open_rate * (1 + move))

    max_hold = bot.get("max_hold_h")
    if max_hold and trade.get("open_date"):
        try:
            # freqtrade는 "2026-08-31 00:00:22" (UTC) 형식으로 준다
            opened = datetime.strptime(trade["open_date"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
            out["hold_remaining_h"] = round(max(0.0, max_hold - elapsed), 1)
            out["hold_total_h"] = max_hold
            hard = bot.get("max_hold_hard_h")
            if bot.get("hold_extends_if_ranked") and hard and elapsed >= max_hold:
                out["hold_extended"] = True
                out["hold_remaining_h"] = round(max(0.0, hard - elapsed), 1)
        except (ValueError, TypeError):
            pass
    return out


# 지표(박스 상태) 캐시. 대시보드는 4초마다 갱신되는데 박스는 1시간봉이라
# 매번 봇에 물어보면 불필요한 부하만 생긴다.
_signal_cache = {"ts": 0.0, "data": None}
SIGNAL_TTL = 60


def _box_signals(bot: dict) -> list[dict]:
    """박스 돌파 전략의 진입 조건: 페어별 박스 범위/폭/돌파까지 남은 거리."""
    try:
        wl = call_bot(bot["url"], "/api/v1/whitelist").get("whitelist", [])
    except Exception:  # noqa: BLE001
        return []
    limit = bot.get("box_max_width", 0.04)
    out = []
    for pair in wl:
        try:
            d = call_bot(bot["url"], "/api/v1/pair_candles",
                         {"pair": pair, "timeframe": "1h", "limit": 3})
        except Exception:  # noqa: BLE001
            continue
        cols = {c: i for i, c in enumerate(d.get("columns", []))}
        rows = d.get("data") or []
        if not rows or "box_width" not in cols:
            continue
        r = rows[-1]

        def val(name):
            i = cols.get(name)
            return r[i] if i is not None else None

        hi, lo, width, close = (val("box_high"), val("box_low"),
                                val("box_width"), val("close"))
        if hi is None or lo is None or width is None or not close:
            continue
        out.append({
            "bot": bot["id"], "pair": pair,
            "box_high": hi, "box_low": lo, "box_width": width,
            "close": close, "limit": limit,
            "is_box": width <= limit,
            # 돌파까지 남은 거리(%). 음수면 이미 그 방향으로 벗어난 상태.
            "to_high_pct": (hi - close) / close * 100,
            "to_low_pct": (close - lo) / close * 100,
            "enter_long": bool(val("enter_long")),
            "enter_short": bool(val("enter_short")),
        })
    return out


def _xsect_signals(bot: dict) -> list[dict]:
    """횡단면 모멘텀의 진입 조건: 박스가 아니라 순위다.

    실제 롱/숏 판정은 봇 프로세스 내부 상태(XSectMomentumStrategy.bot_loop_start
    가 채우는 self._longs/_shorts)에만 있고 freqtrade API로는 노출되지 않는다.
    그래서 같은 규칙(마지막 행 = 가장 최근에 마감된 일봉 기준 lookback일 수익률 -
    freqtrade pair_candles 엔 완성된 봉만 있다)을 여기서 그대로 재현해 순위를 매긴다.
    상위 top_k = 롱 후보, 하위 top_k = 숏 후보.
    """
    try:
        wl = call_bot(bot["url"], "/api/v1/whitelist").get("whitelist", [])
    except Exception:  # noqa: BLE001
        return []
    lb = bot.get("lookback", 14)
    top_k = bot.get("top_k", 3)
    tf = bot.get("timeframe", "1d")

    # 순위권이어도 재진입이 막힌 종목: (1) 마지막 청산 후 reentry_cooldown_h 이내
    # (전략 _in_reentry_cooldown 과 같은 계산), (2) freqtrade 자체 잠금(청산 직후
    # 다음 일봉까지 같은 방향 자동잠금 등). 둘 다 (pair, side) -> 해제시각(ms)으로 모은다.
    now_ms = time.time() * 1000
    locks: dict = {}
    try:
        for lk in call_bot(bot["url"], "/api/v1/locks").get("locks", []):
            if lk.get("active") and lk.get("lock_end_timestamp", 0) > now_ms:
                key = (lk["pair"], lk.get("side") or "*")
                locks[key] = max(locks.get(key, 0), lk["lock_end_timestamp"])
    except Exception:  # noqa: BLE001
        pass
    cooldown_ms = bot.get("reentry_cooldown_h", 0) * 3600 * 1000
    if cooldown_ms:
        try:
            for t in call_bot(bot["url"], "/api/v1/trades", {"limit": 500}).get("trades", []):
                if t.get("is_open") or not t.get("close_timestamp"):
                    continue
                end = t["close_timestamp"] + cooldown_ms
                if end > now_ms:
                    key = (t["pair"], "*")
                    locks[key] = max(locks.get(key, 0), end)
        except Exception:  # noqa: BLE001
            pass

    # 순위 점수: 기본은 lookback일 수익률, rank_score="rsi" 면 RSI(rsi_period)
    # (XSectRsiStrategy._rank_score 와 같은 단순평균 방식). 수익률은 참고용으로 항상 같이 낸다.
    score_kind = bot.get("rank_score", "ret")
    rsi_n = bot.get("rsi_period", 14)
    need = max(lb, rsi_n) + 1

    scores = []
    for pair in wl:
        closes = []
        try:
            d = call_bot(bot["url"], "/api/v1/pair_candles",
                         {"pair": pair, "timeframe": tf, "limit": need + 2})
            cols = {c: i for i, c in enumerate(d.get("columns", []))}
            ci = cols.get("close")
            if ci is not None:
                closes = [row[ci] for row in d.get("data") or []]
        except Exception:  # noqa: BLE001
            pass
        # 정지된 봇은 캔들을 안 받아와서 비어있다 - 바이낸스 일봉(마감된 봉만)으로 대신 계산
        if len(closes) < need:
            closes = _binance_closed_daily_closes(pair, need + 2)
        if len(closes) < need:
            continue
        now_c, past_c = closes[-1], closes[-1 - lb]
        if not now_c or not past_c:
            continue
        item = {"bot": bot["id"], "pair": pair, "close": now_c,
                "ret": now_c / past_c - 1.0, "score_kind": score_kind}
        if score_kind == "rsi":
            diffs = [b - a for a, b in zip(closes[-rsi_n - 1:-1], closes[-rsi_n:])]
            gain = sum(x for x in diffs if x > 0) / rsi_n
            loss = sum(-x for x in diffs if x < 0) / rsi_n
            if gain == 0 and loss == 0:
                continue
            item["score"] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
        else:
            item["score"] = item["ret"]
        scores.append(item)

    # 상위/하위 top_k 를 가르려면 양쪽에 최소 top_k개씩은 있어야 한다
    # (XSectMomentumStrategy.bot_loop_start 와 같은 조건).
    if len(scores) < 2 * top_k:
        return []

    # 계좌에 이 봇 것이 아닌 포지션이 있는 종목(다른 봇/수동) - 전략이 진입하지 않고 건너뛴다
    foreign: set = set()
    if bot.get("skip_foreign_positions"):
        try:
            own = {t["pair"] for t in call_bot(bot["url"], "/api/v1/status")}
            acct = fetch_account()
            for p in acct.get("positions", []):
                pair = f"{p['base']}/USDT:USDT"
                if pair not in own:
                    foreign.add(pair)
        except Exception:  # noqa: BLE001
            pass

    scores.sort(key=lambda x: x["score"])
    n = len(scores)
    for i, s in enumerate(scores):
        s.update({"rank": i + 1, "total": n, "status": "wait", "lock_until": None,
                  "foreign": s["pair"] in foreign})  # 1 = 최하위

    # 전략 _pick_eligible 과 같은 규칙: 순위 순서대로 보면서 재진입 대기 종목(과
    # skip_foreign_positions 봇이면 다른 포지션이 있는 종목)은 건너뛰고 top_k개를 후보로. 중간 순위(n//2)를 넘어서까지는 내려가지 않는다.
    def pick(order: list, side: str) -> None:
        picked = 0
        for s in order[: n // 2]:
            if picked >= top_k:
                break
            end = max(locks.get((s["pair"], side), 0), locks.get((s["pair"], "*"), 0))
            if end:
                s["lock_until"] = datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()
                continue
            if s["foreign"]:
                continue
            s["status"] = side
            picked += 1

    pick(scores[::-1], "long")
    pick(scores, "short")
    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores


def fetch_signals():
    """지금 가동 중인 봇의 진입 조건. '지금 왜 진입을 안 하는지'를 화면에서 답하기 위한 것.

    정지된 박스형 봇은 여기서 걸러진다 - 안 도는 봇의 신호를 보여줘 봐야 혼란만 준다.
    순위형(xsect_momentum) 봇은 정지 중이어도 표시하고 stopped 로 표시한다.
    이렇게 하면 대시보드 전원 버튼으로 봇을 켜고 끌 때마다 이 패널이 무엇을
    기준으로 도는지 코드를 따로 손볼 필요가 없다.
    """
    now = time.time()
    if _signal_cache["data"] is not None and now - _signal_cache["ts"] < SIGNAL_TTL:
        return _signal_cache["data"]

    out = []
    for bot in BOTS:
        try:
            cfg = call_bot(bot["url"], "/api/v1/show_config")
        except Exception:  # noqa: BLE001
            continue
        kind = bot.get("signal_kind", "box")
        running = cfg.get("state") == "running"
        # 순위형 봇은 정지 중이어도 "켜면 무엇을 살지"를 보여준다(2026-10-06,
        # 모멘텀 -> RSI 전환 중 RSI 봇 순위를 미리 보려고). 박스형은 그대로 숨긴다.
        if not running and kind != "xsect_momentum":
            continue

        items = _xsect_signals(bot) if kind == "xsect_momentum" else _box_signals(bot)
        for item in items:
            item["kind"] = kind
            item["stopped"] = not running
            item.setdefault("bot_name", bot["name"])
        out.extend(items)
    _signal_cache.update({"ts": now, "data": out})
    return out


@app.get("/api/signals")
def api_signals():
    return jsonify({"signals": fetch_signals()})


# ---------------------------------------------------------------------------
# 계좌 직접 조회 (수동 매매 / 미체결 주문)
#
# freqtrade는 "자기가 낸 주문"만 안다. 사용자가 바이낸스 앱이나 웹에서 직접 잡은
# 포지션, 직접 걸어둔 지정가 주문은 봇 DB에 없어서 대시보드에 전혀 나오지 않았다.
# 그래서 이 두 가지만 거래소 REST API로 직접 가져온다.
#
# 보안: 조회 엔드포인트(positionRisk / openOrders)만 호출한다. 주문 생성·취소는
#       이 서버에서 하지 않는다.
# ---------------------------------------------------------------------------

ORDER_TYPE_KO = {
    "LIMIT": "지정가",
    "MARKET": "시장가",
    "STOP": "스탑 지정가",
    "STOP_MARKET": "스탑 시장가",
    "TAKE_PROFIT": "익절 지정가",
    "TAKE_PROFIT_MARKET": "익절 시장가",
    "TRAILING_STOP_MARKET": "트레일링 스탑",
}

# 바이낸스는 주문을 낸 경로를 clientOrderId 접두사로 남긴다. 봇(ccxt)이 낸 주문과
# 사람이 앱/웹에서 낸 주문을 구분하는 가장 확실한 단서라서 이걸 먼저 본다.
MANUAL_ORDER_PREFIXES = ("web_", "android_", "ios_", "mobile_", "autoclose-")


def binance_signed(path: str, params: dict | None = None):
    """바이낸스 USDⓈ-M 선물 서명 GET. 조회 전용."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET 가 .env에 없습니다")
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    query = urlencode(p)
    signature = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    resp = requests.get(
        f"{BINANCE_FAPI}{path}?{query}&signature={signature}",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        timeout=6,
    )
    resp.raise_for_status()
    return resp.json()


def _bot_symbols() -> tuple[set, dict]:
    """
    봇이 관리 중인 심볼과, 봇이 감시하는 심볼(화이트리스트)을 모은다.

    반환: (봇이 실제로 포지션을 들고 있는 심볼, {심볼: 봇 이름})
    freqtrade의 페어 표기 "BTC/USDT:USDT" 를 바이낸스 심볼 "BTCUSDT" 로 바꾼다.
    """
    held, watched = set(), {}
    for bot in BOTS:
        try:
            for t in call_bot(bot["url"], "/api/v1/status"):
                held.add(t["pair"].split(":")[0].replace("/", ""))
        except Exception:  # noqa: BLE001
            pass
        try:
            for pair in call_bot(bot["url"], "/api/v1/whitelist").get("whitelist", []):
                watched[pair.split(":")[0].replace("/", "")] = bot["name"]
        except Exception:  # noqa: BLE001
            pass
    return held, watched


# 대시보드는 4초마다 갱신된다. 그대로 거래소에 붙이면 요청 한도(weight)를 낭비하므로
# 짧게 캐시한다. 미체결 주문은 사람이 직접 넣는 것이라 몇 초 늦어도 문제없다.
_account_cache = {"ts": 0.0, "data": None}
ACCOUNT_TTL = 8


def fetch_account() -> dict:
    now = time.time()
    if _account_cache["data"] is not None and now - _account_cache["ts"] < ACCOUNT_TTL:
        return _account_cache["data"]

    out = {"ok": True, "error": None, "positions": [], "orders": []}
    try:
        raw_pos = binance_signed("/fapi/v2/positionRisk")
        raw_ord = binance_signed("/fapi/v1/openOrders")
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": str(exc)})
        _account_cache.update({"ts": now, "data": out})
        return out

    held, watched = _bot_symbols()

    for p in raw_pos:
        amt = float(p.get("positionAmt") or 0)
        if amt == 0:
            continue
        symbol = p["symbol"]
        lev = float(p.get("leverage") or 1) or 1
        notional = abs(float(p.get("notional") or 0))
        # 격리 마진이면 실제 투입 증거금이 그대로 있고, 교차면 명목가/레버리지로 환산.
        margin = float(p.get("isolatedMargin") or 0) or (notional / lev if lev else 0)
        pnl = float(p.get("unRealizedProfit") or 0)
        out["positions"].append({
            "symbol": symbol,
            "base": symbol.replace("USDT", ""),
            "side": "short" if amt < 0 else "long",
            "amount": abs(amt),
            "entry_price": float(p.get("entryPrice") or 0),
            "mark_price": float(p.get("markPrice") or 0),
            "liquidation_price": float(p.get("liquidationPrice") or 0) or None,
            "leverage": lev,
            "notional": notional,
            "margin": margin,
            "pnl": pnl,
            # 레버리지가 적용된 계좌 기준 수익률(= 투입 증거금 대비)
            "pnl_pct": (pnl / margin * 100) if margin else 0,
            "managed": symbol in held,
        })

    for o in raw_ord:
        symbol = o["symbol"]
        cid = o.get("clientOrderId", "") or ""
        if cid.startswith(MANUAL_ORDER_PREFIXES):
            manual = True
        else:
            # 접두사로 판단이 안 되면, 봇이 감시하는 페어인지로 갈음한다.
            manual = symbol not in watched
        price = float(o.get("price") or 0)
        stop_price = float(o.get("stopPrice") or 0)
        qty = float(o.get("origQty") or 0)
        filled = float(o.get("executedQty") or 0)
        out["orders"].append({
            "order_id": o.get("orderId"),
            "symbol": symbol,
            "base": symbol.replace("USDT", ""),
            "side": o.get("side"),                       # BUY / SELL
            "type": o.get("type"),
            "type_ko": ORDER_TYPE_KO.get(o.get("type"), o.get("type")),
            "price": price or None,
            "stop_price": stop_price or None,
            "qty": qty,
            "filled": filled,
            "reduce_only": bool(o.get("reduceOnly")),
            "manual": manual,
            "owner": "수동" if manual else watched.get(symbol, "봇"),
            "time": int(o.get("time") or 0),
        })

    out["orders"].sort(key=lambda x: (x["symbol"], x["side"], -(x["price"] or 0)))
    _account_cache.update({"ts": now, "data": out})
    return out


@app.get("/api/account")
def api_account():
    return jsonify(fetch_account())


# freqtrade가 자체 계산하는 profit_all_percent는 내부적으로
# "현재 가용 잔고 - 지금까지 번 돈"으로 시작자본을 역산한다(freqtrade
# Wallets.get_starting_balance). BoxBreakoutV2와 XSectMomentum이 같은
# 바이낸스 계좌를 공유하다 보니, 이 "현재 가용 잔고"에는 상대 봇이 방금
# 포지션을 열고 닫으며 쓰는 증거금까지 섞여 들어간다 - 그래서 이 봇은
# 아무 거래도 안 했는데 상대 봇이 매매할 때마다 이 봇의 총손익 %가 같이
# 흔들리는 현상이 생겼다(우리 쪽 total_profit_pct 도 total_equity를 그대로
# 가져다 쓰던 예전 방식이라 같은 문제를 겪었다). 시작자본을 매 요청마다
# 실시간으로 역산하는 대신 한 번 계산해서 한동안 고정해두면, 진짜 잔고
# 변화(입출금, 봇 간 잔고 재배분)가 있을 때만 서서히 갱신되고 상대 봇의
# 매매 노이즈로는 흔들리지 않는다.
STARTING_CAPITAL_TTL = 4 * 3600  # 4시간
# 대시보드를 배포할 때마다(하루에도 여러 번) 프로세스가 재시작되면서 메모리
# 캐시가 날아갔는데, 마침 그 순간 봇이 정지 상태라 시작자본을 새로 잴 수
# 없으면(아래 fetch_bot_summary 참고) 엉뚱한 값으로 되돌아가는 사고가 있었다
# (-8.99 USDT 손실이 -3239% 로 표시됨). 재시작에도 살아남도록 파일에 저장한다.
STARTING_CAPITAL_CACHE_FILE = ROOT / "starting_capital_cache.json"


def _load_starting_capital_cache() -> dict:
    try:
        return json.loads(STARTING_CAPITAL_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_starting_capital_cache() -> None:
    try:
        STARTING_CAPITAL_CACHE_FILE.write_text(
            json.dumps(_starting_capital_cache), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


_starting_capital_cache: dict[str, dict] = _load_starting_capital_cache()


def _stable_starting_capital(key: str, current_value: float, profit_abs: float) -> float:
    now = time.time()
    entry = _starting_capital_cache.get(key)
    if entry is not None and now - entry["ts"] < STARTING_CAPITAL_TTL:
        return entry["value"]
    value = max(current_value - profit_abs, 0)
    _starting_capital_cache[key] = {"ts": now, "value": value}
    _save_starting_capital_cache()
    return value


def fetch_bot_summary(bot: dict, days: int = 14) -> dict:
    out = {"id": bot["id"], "name": bot["name"], "leverage": bot.get("leverage", 1), "connected": False}
    try:
        balance = call_bot(bot["url"], "/api/v1/balance")
        profit = call_bot(bot["url"], "/api/v1/profit")
        open_trades = call_bot(bot["url"], "/api/v1/status")
        # limit 만 걸면 안 된다 - /api/v1/trades 는 trade_id 오름차순(오래된
        # 것부터)이라, 청산 건수가 limit 을 넘는 순간부터 "최신"이 아니라
        # "가장 오래된" 거래들이 잘려 들어온다. 넉넉히 받아서 아래에서
        # close_date 기준으로 직접 재정렬한다.
        recent = call_bot(bot["url"], "/api/v1/trades", {"limit": 500})
        # 누적손익추이 그래프의 기간 선택(14/30/60일)에 맞춰 조회한다.
        daily = call_bot(bot["url"], "/api/v1/daily", {"timescale": days})
        config = call_bot(bot["url"], "/api/v1/show_config")

        # freqtrade의 balance.total_bot 필드는 신뢰 불가:
        # 1) 계좌에 남아있는 선물/현물 외 다른 자산(BNB 더스트 등)까지 합산됨
        # 2) 두 봇이 같은 계좌를 공유하다 보니, 다른 봇이 연 포지션의 증거금까지
        #    이쪽 봇 소유로 잘못 잡히는 경우가 있음(is_bot_managed 플래그 무시하고 합산)
        # -> "이 봇 몫의 여유 USDT" + "이 봇이 실제로 관리하는(is_bot_managed=true) 포지션의
        #    증거금"만 직접 더해서 정확한 값을 계산함
        currencies = balance.get("currencies", [])
        usdt_entry = next((c for c in currencies if c.get("currency") == "USDT"), None)
        own_free = usdt_entry["bot_owned"] if usdt_entry else 0
        own_positions_margin = sum(
            c.get("est_stake_bot") or 0
            for c in currencies
            if c.get("is_position") and c.get("is_bot_managed")
        )
        balance_bot_owned = own_free + own_positions_margin
        profit_all_abs = profit.get("profit_all_coin", 0)
        # 정지된 봇은 포지션도 없고 own_free도 "계좌 전체 여유 USDT x 이 봇의
        # ratio" 로 계속 흔들리는 값이라(다른 봇이 증거금을 얼마나 쓰고
        # 있는지에 따라 같이 움직임), 이 상태에서 시작자본을 다시 역산하면
        # 이 값이 우연히 작을 때 "실손실 -8.99 USDT가 -97%" 같은 헛수치가
        # 나온다(실제로 겪음). 정지 중엔 새로 역산하지 않고, 마지막으로
        # 실제 가동 중일 때 캐시해둔 값을 그대로 쓴다.
        if config.get("state") == "stopped":
            cached = _starting_capital_cache.get(bot["id"])
            # 캐시가 아예 없으면(파일도 없던 첫 실행 등) 그래도 같은 공식으로는
            # 맞춰서 반환한다 - balance_bot_owned를 그대로 쓰면 방금 겪었던
            # 사고와 똑같이 분모가 손실액보다 작아서 % 가 폭발할 수 있다.
            bot_starting_capital = (
                cached["value"] if cached else max(balance_bot_owned - profit_all_abs, 0)
            )
        else:
            bot_starting_capital = _stable_starting_capital(
                bot["id"], balance_bot_owned, profit_all_abs
            )

        # 봇이 정지 상태면 화면에는 잔고를 0으로 보여준다.
        #
        # 이건 tradable_balance_ratio 를 낮추는 것과는 다른 방식으로 접근한
        # 것이다 - 한 번 ratio 를 0.01 로 낮춰서 "화면에 잔고가 적게 보이게"
        # 했다가, freqtrade가 손익%/MDD 를 계산할 때도 같은 ratio 를 분모로
        # 쓰는 바람에 실제 손익(-8.99 USDT)이 -409% 라는 말도 안 되는 수치로
        # 나온 적이 있다(그 ratio 로 줄어든 시작자본을 분모로 나눴기 때문).
        # ratio 는 freqtrade 내부 계산에까지 영향을 주므로, "화면에만" 보이는
        # 값을 바꾸고 싶으면 이렇게 표시 레이어에서 오버라이드해야
        # 손익률/MDD 계산은 건드리지 않는다.
        if config.get("state") == "stopped":
            balance_bot_owned = 0.0

        # 계좌 전체 실제 잔고(봇 소유 여부와 무관). "총자산" 표시에 쓴다.
        # balance_bot_owned는 신규 봇이라 거래 이력이 없으면 0에 가깝게 나오는데,
        # 그걸 총자산으로 보여주면 실제 계좌에 돈이 있는데도 0으로 보인다.
        account_total = balance.get("total", 0) or 0

        # 봇이 관리하지 않는 포지션(사용자가 직접 잡은 것 등).
        # 계좌 잔고 대부분이 여기 묶여 있을 수 있어서 따로 보여준다.
        unmanaged = [
            {
                "pair": c.get("currency"),
                "side": c.get("side"),
                "position": c.get("position"),
                "est_stake": c.get("est_stake"),
            }
            for c in currencies
            if c.get("is_position") and not c.get("is_bot_managed")
        ]

        out.update(
            {
                "connected": True,
                "account_total": account_total,
                "unmanaged_positions": unmanaged,
                "dry_run": config.get("dry_run", True),
                # freqtrade의 봇 상태: "running" | "stopped" | "paused"
                # 대시보드의 시작/정지 버튼이 이 값을 보고 표시를 바꾼다
                "state": config.get("state", "unknown"),
                "balance_total": balance_bot_owned,
                "starting_capital": balance.get("starting_capital", 0),
                "profit_closed_abs": profit.get("profit_closed_coin", 0),
                "profit_closed_pct": profit.get("profit_closed_percent", 0),
                "profit_all_abs": profit_all_abs,
                # freqtrade가 내려주는 profit_all_percent 대신 위 _stable_starting_capital
                # 로 직접 계산한다 - 계좌를 공유하는 다른 봇의 매매만으로도 흔들리는
                # 문제(위 주석 참고)를 피하기 위함.
                "profit_all_pct": (
                    profit_all_abs / bot_starting_capital * 100 if bot_starting_capital else 0
                ),
                # 위 % 의 분모 - api_summary 가 손익을 보정하면 % 도 다시 계산한다
                "stable_starting_capital": bot_starting_capital,
                "trade_count": profit.get("trade_count", 0),
                "winrate": profit.get("winrate", 0),
                # freqtrade가 내려주는 max_drawdown(비율)도 profit_all_percent와 똑같이
                # 계좌 공유로 오염된 시작자본을 분모로 써서 100%를 넘는 값(188% 등)이
                # 나올 수 있다. max_drawdown_abs(달러 낙폭)는 순수 거래 기록만으로
                # 계산되니 이건 그대로 믿고, 분모만 우리가 쓰는 안정된
                # bot_starting_capital로 바꿔서 비율을 직접 낸다.
                "max_drawdown": (
                    profit.get("max_drawdown_abs", 0) / bot_starting_capital
                    if bot_starting_capital
                    else profit.get("max_drawdown", 0)
                ),
                "open_trades": [
                    {
                        "trade_id": t.get("trade_id"),
                        "pair": t["pair"],
                        "is_short": t["is_short"],
                        "amount": t.get("amount") or 0,
                        "leverage": t.get("leverage", 1),
                        "open_rate": t["open_rate"],
                        "current_rate": t.get("current_rate"),
                        "profit_pct": t.get("profit_pct", 0),
                        "profit_abs": t.get("profit_abs", 0),
                        "stake_amount": t.get("stake_amount", 0),
                        "open_date": t.get("open_date"),
                        "liquidation_price": t.get("liquidation_price"),
                        # 이 전략은 손절선이 진입가 대비 고정%가 아니라 박스 경계라
                        # 포지션마다 다르다. 화면에서 "어디서 잘리는지"를 보려면 필요.
                        "stop_loss_abs": t.get("stop_loss_abs"),
                        # 진입 주문이 아직 안 체결됐으면 거래소엔 포지션이 없다 - 아래
                        # api_summary 의 계좌 대조에서 "이미 청산됨"으로 오판하지 않게
                        "has_open_orders": t.get("has_open_orders", False),
                        **exit_targets(t, bot, config),
                    }
                    for t in open_trades
                ],
                "recent_trades": [
                    {
                        "pair": t["pair"],
                        "is_short": t.get("is_short", False),
                        "close_profit_pct": t.get("close_profit_pct"),
                        "close_profit_abs": t.get("close_profit_abs"),
                        "open_date": t.get("open_date"),
                        "close_date": t.get("close_date"),
                        "exit_reason": t.get("exit_reason"),
                        "exit_reason_ko": exit_reason_ko(t.get("exit_reason")),
                    }
                    for t in sorted(
                        (t for t in recent.get("trades", []) if not t.get("is_open")),
                        key=lambda t: t.get("close_date") or "",
                        reverse=True,
                    )[:30]
                ],
                "daily": [
                    {
                        "date": d["date"],
                        "abs_profit": d.get("abs_profit", 0),
                        "starting_balance": d.get("starting_balance", 0),
                    }
                    for d in reversed(daily.get("data", []))
                ],
            }
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def post_bot(base_url: str, path: str):
    resp = requests.post(f"{base_url}{path}", auth=(USERNAME, PASSWORD), timeout=6)
    resp.raise_for_status()
    return resp.json()


@app.post("/api/bots/<bot_id>/<action>")
def api_bot_control(bot_id: str, action: str):
    """
    봇 시작/정지.
      start : 자동매매 시작 (신규 진입 + 청산 관리 모두 재개)
      stop  : 자동매매 정지. 신규 진입이 멈춘다.
              ※ 주의: 이미 열려 있는 포지션이 있으면 그 포지션의 손절/익절 관리도
                 같이 멈춘다. 열린 포지션이 있는 상태로 정지하면 그 포지션은
                 사용자가 직접 관리해야 한다.
    """
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "error": "지원하지 않는 동작입니다"}), 400

    bot = next((b for b in BOTS if b["id"] == bot_id), None)
    if bot is None:
        return jsonify({"ok": False, "error": "봇을 찾을 수 없습니다"}), 404

    try:
        # 정지 요청인데 열린 포지션이 있으면 경고를 같이 돌려준다(막지는 않음)
        warning = None
        if action == "stop":
            try:
                open_trades = call_bot(bot["url"], "/api/v1/status")
                if open_trades:
                    pairs = ", ".join(t["pair"] for t in open_trades)
                    warning = (
                        f"열린 포지션 {len(open_trades)}건({pairs})의 손절/익절 관리도 "
                        f"함께 멈춥니다. 직접 관리하셔야 합니다."
                    )
            except Exception:  # noqa: BLE001
                pass

        result = post_bot(bot["url"], f"/api/v1/{action}")
        state = "running" if action == "start" else "stopped"
        return jsonify({
            "ok": True,
            "state": state,
            "message": result.get("status", ""),
            "warning": warning,
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 502


# 봇 진입 직후엔 계좌 조회 캐시(ACCOUNT_TTL)가 아직 옛 값일 수 있다 - 이 시간 안에 연
# 거래는 계좌에 안 보여도 그대로 둔다.
FRESH_TRADE_GRACE_SEC = 60


def _drop_positions_closed_on_exchange(connected: list, acct: dict) -> None:
    """봇 기록엔 열려 있는데 바이낸스 계좌엔 없는 포지션을 화면에서 뺀다.

    2026-10-06: 모멘텀 봇을 정지한 뒤 바이낸스에서 직접 5개를 청산했는데, 정지된
    freqtrade 는 거래소를 안 보므로 봇 DB엔 6개가 계속 "보유 중"으로 남아 대시보드에
    유령 포지션으로 떴다. 실제 계좌를 기준으로 맞춘다. 그 거래의 미실현손익(옛 시세
    기준)도 봇 손익에서 뺀다 - 실제 청산 손익은 수동 손익(income)으로 따로 잡히므로
    빼지 않으면 같은 거래가 두 번 계산된다. 계좌 조회 실패 시엔 손대지 않는다.

    2026-10-10: "계좌에 같은 페어·방향 포지션이 있으면 살아있음"으로만 보니, 모멘텀 봇의
    유령 AAVE 롱(10/6 직접 청산, 기록 정리 불가)이 RSI 봇이 새로 잡은 AAVE 롱 때문에
    모멘텀 탭에도 다시 떴다. 계좌의 포지션 수량을 가장 최근에 연 거래부터 나눠 주고,
    남은 수량으로 덮이는 거래만 살아있는 것으로 본다(옛 유령 거래가 남의 포지션을 못 가져감).
    """
    if not acct.get("ok"):
        return
    remaining: dict = {}
    for p in acct.get("positions", []):
        k = (f"{p['base']}/USDT:USDT", p["side"])
        remaining[k] = remaining.get(k, 0.0) + abs(float(p.get("amount") or 0))
    now = datetime.now(timezone.utc)

    def opened_at(t):
        try:
            return datetime.strptime(t.get("open_date") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    alive = set()
    every = [(b["id"], t) for b in connected for t in b.get("open_trades", [])]
    for bid, t in sorted(every, key=lambda x: opened_at(x[1]), reverse=True):
        key = (t["pair"], "short" if t.get("is_short") else "long")
        fresh = (now - opened_at(t)).total_seconds() < FRESH_TRADE_GRACE_SEC
        need = float(t.get("amount") or 0)
        if t.get("has_open_orders") or fresh:
            alive.add((bid, t.get("trade_id")))
            remaining[key] = max(remaining.get(key, 0.0) - need, 0.0)
        elif remaining.get(key, 0.0) >= need * 0.99 and remaining.get(key, 0.0) > 0:
            alive.add((bid, t.get("trade_id")))
            remaining[key] -= need

    for b in connected:
        live, gone = [], []
        for t in b.get("open_trades", []):
            (live if (b["id"], t.get("trade_id")) in alive else gone).append(t)
        if gone:
            b["open_trades"] = live
            b["closed_on_exchange"] = [t["pair"] for t in gone]
            b["profit_all_abs"] = b.get("profit_all_abs", 0) - sum(t.get("profit_abs") or 0 for t in gone)
            cap = b.get("stable_starting_capital")
            b["profit_all_pct"] = b["profit_all_abs"] / cap * 100 if cap else 0


@app.get("/api/summary")
def api_summary():
    # 누적손익추이 그래프의 기간 선택. 정해둔 값 밖이면(URL 조작 등) 기본값으로
    # 조용히 되돌린다 - 임의의 timescale 을 그대로 각 봇에 흘려보내고 싶지 않다.
    days = request.args.get("days", 14, type=int)
    if days not in (14, 30, 60):
        days = 14

    bots = [fetch_bot_summary(b, days=days) for b in BOTS]
    connected = [b for b in bots if b.get("connected")]
    acct = fetch_account()
    _drop_positions_closed_on_exchange(connected, acct)

    # 총자산: 모든 봇이 같은 바이낸스 계좌를 공유하므로 합산하면 중복 계산이 된다.
    # 계좌 전체 잔고를 한 번만 취한다(봇마다 같은 값을 보고함).
    total_equity = max((b.get("account_total", 0) for b in connected), default=0)

    # 손익은 봇별로 각자 관리하는 거래에서 나오므로 합산이 맞다.
    bot_profit_abs = sum(b.get("profit_all_abs", 0) for b in connected)
    # 수동 매매 손익(계좌 실현손익 중 봇이 기록하지 않은 부분). 조회 실패 시 None -
    # 그 경우 총손익은 봇 손익만으로 계산해 화면이 완전히 비진 않게 한다.
    manual_pnl = fetch_manual_pnl(days=days)
    manual_profit_abs = manual_pnl.get("total")
    total_profit_abs = bot_profit_abs + (manual_profit_abs or 0)

    # 원금(시작 자본)은 봇별 starting_capital 을 그냥 더하면 안 된다 - 봇들이
    # 시차를 두고 같은 계좌를 공유하므로(예: XSectMomentum 이 시작한 시점엔
    # 이미 BoxBreakoutV2 가 벌어들인 돈이 잔고에 섞여 있었다), 단순 합산은
    # 같은 원금을 여러 번 세는 꼴이 된다 - 실측으로 발견됨: 합산 320 > 지금
    # 자산 211 (원금이 현재 자산보다 큰, 있을 수 없는 숫자가 나왔었다).
    # "현재 자산 - 지금까지 번 돈 = 원금" 으로 역산하는 게 맞다.
    #
    # 이걸 매 요청마다 실시간으로 다시 역산하면, total_equity(바이낸스 잔고
    # 조회)와 total_profit_abs(freqtrade가 캐시해둔 시세 기준 평가)가 서로
    # 완전히 같은 순간의 값이 아니라서 몇 초 간격으로도 미세하게 어긋나고,
    # 원금 자체가 크지 않다 보니 그 몇 달러 차이가 총손익률 %에서는 몇 %p
    # 단위로 증폭되어 "자산은 그대로인데 %만 계속 바뀌는" 것처럼 보였다.
    # _stable_starting_capital 로 같은 값을 한동안 고정해서 이 노이즈를 없앤다.
    total_starting = _stable_starting_capital("total", total_equity, total_profit_abs)

    # 봇이 관리하지 않는 포지션(직접 잡은 것)을 페어 기준으로 중복 제거.
    # 화이트리스트 밖 페어(QQQ/TQQQ 등)는 freqtrade API 자체가 몰라서 여기
    # 안 잡힌다 - "보유 포지션" 카운트는 이것만으로는 못 만든다(아래에서 보강).
    #
    # 주의: fetch_bot_summary() 의 "unmanaged_positions" 는 그 봇 자신의
    # /api/v1/balance 응답에 있는 is_bot_managed 만 보고 판단한 것이다.
    # 계좌를 공유하는 다른 봇이 연 포지션은 "나(이 봇)" 입장에서는 당연히
    # is_bot_managed=false 로 나오므로, 봇별 목록을 그냥 합치면 실제로는
    # 우리 봇 중 하나가 관리 중인 포지션(ETH/LINK/T/SUI/EGLD/UNI 등)까지
    # "미관리"로 잘못 뜬다. 연결된 봇들의 open_trades 에 실제로 있는 페어는
    # 제외해야 진짜 수동 포지션만 남는다. (balance 응답의 "currency"가
    # 선물 포지션에서는 "ETH" 가 아니라 "ETH/USDT:USDT" 처럼 open_trades의
    # pair와 같은 전체 표기로 나온다 - 실측으로 확인함.)
    managed_pairs = {t["pair"] for b in connected for t in b.get("open_trades", [])}
    unmanaged = {}
    for b in connected:
        for p in b.get("unmanaged_positions", []):
            if p["pair"] in managed_pairs:
                continue
            unmanaged[p["pair"]] = p

    # "보유 포지션" 카드는 봇이 여는 포지션뿐 아니라 계좌에 실제로 떠 있는
    # 수동 포지션도 세야 한다. 바이낸스를 직접 봐야 화이트리스트 밖 페어도
    # 잡힌다 - 그동안 QQQ 롱처럼 봇이 모르는 포지션이 있어도 "보유 포지션 0"
    # 으로 잘못 표시됐다.
    manual_positions = [p for p in acct.get("positions", []) if not p.get("managed")] if acct.get("ok") else []
    manual_position_count = len(manual_positions)
    # 포지션 방향(도넛) 위젯도 봇 포지션만 세고 있었다 - 수동 포지션의
    # 롱/숏 개수를 따로 내서 프론트가 봇 것과 합산하게 한다.
    manual_longs = sum(1 for p in manual_positions if p.get("side") == "long")
    manual_shorts = sum(1 for p in manual_positions if p.get("side") == "short")

    return jsonify(
        {
            "bots": bots,
            "combined": {
                "total_equity": total_equity,
                "total_starting": total_starting,
                "total_profit_abs": total_profit_abs,
                "bot_profit_abs": bot_profit_abs,
                "manual_profit_abs": manual_profit_abs,
                # 누적손익추이 차트에서 "수동" 시리즈로 그릴 최근 14일 일별 손익
                "manual_daily": manual_pnl.get("daily", []),
                "manual_position_count": manual_position_count,
                "manual_longs": manual_longs,
                "manual_shorts": manual_shorts,
                "total_profit_pct": (total_profit_abs / total_starting * 100) if total_starting else 0,
                "unmanaged_positions": list(unmanaged.values()),
                # 청산 이력 테이블에서 봇 기록과 합쳐서 보여줄 수동 청산들.
                "manual_trades": fetch_manual_trade_history(),
            },
            "server_time": int(time.time()),
        }
    )


def _fetch_futures_tickers(symbols: list[str]) -> list[dict]:
    """봇이 실제로 거래하는 시세와 맞춰서 스팟이 아니라 선물(USDT-M) 가격을 씀.
    선물 24hr 티커 API는 스팟과 달리 "symbols" 배열 파라미터를 지원하지 않아서
    전체를 받아온 뒤 여기서 원하는 심볼만 골라낸다."""
    resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=4)
    resp.raise_for_status()
    wanted = set(symbols)
    return [
        {
            "symbol": t["symbol"],
            "price": float(t["lastPrice"]),
            "change_pct": float(t["priceChangePercent"]),
            # AI 리포트에 24시간 레인지/거래대금 맥락을 주기 위해 추가.
            # 기존 /api/tickers(사이드바 시세) 호출부는 이 필드들을 안 써도
            # 무시되니 그대로 호환된다.
            "high": float(t["highPrice"]),
            "low": float(t["lowPrice"]),
            "quote_volume": float(t["quoteVolume"]),
        }
        for t in resp.json()
        if t["symbol"] in wanted
    ]


@app.get("/api/tickers")
def api_tickers():
    try:
        return jsonify(_fetch_futures_tickers(TICKER_SYMBOLS))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


# freqtrade가 내보내는 청산 사유 -> 화면/텔레그램에 쓸 한글 라벨.
#
# trailing_stop_loss 주의: 이 이름은 "트레일링 스탑을 켰다"는 뜻이 아니다.
# freqtrade는 '최초 손절선이 아닌, 이후 조여진 손절선'에 걸리면 무조건 이 이름을 쓴다.
# 지금 봇은 trailing_stop=false 이고 손절선을 조이는 건 custom_stoploss(박스 경계)뿐이라,
# 이 사유는 사실상 전부 "박스 경계 손절"이다. 그래서 그렇게 표기한다.
# (트레일링 스탑을 실제로 켠 전략을 다시 띄운다면 이 라벨은 맞지 않으므로 같이 손볼 것)
EXIT_REASON_KO = {
    "roi": "익절 (목표 도달)",
    "stop_loss": "손절 (안전망)",
    "trailing_stop_loss": "손절 (박스 경계)",
    "max_hold": "시간 청산",
    # XSectMomentum custom_exit 사유
    "rebalance": "보유기간 만료 (순위 이탈)",
    "take_profit": "익절 (변동성 기준)",
    "exit_signal": "전략 청산 신호",
    "force_exit": "수동 청산",
    "emergency_exit": "긴급 청산",
    "liquidation": "강제 청산 (청산가 도달)",
    "partial_exit": "부분 청산",
    # 봇이 아니라 거래소/사용자 쪽에서 포지션이 닫힌 경우
    "sold_on_exchange": "수동 청산 (거래소 직접)",
    "timeout": "주문 시간 초과",
    "cancelled": "주문 취소",
}


def exit_reason_ko(raw: str) -> str:
    """모르는 사유는 원문 그대로 둔다 - 조용히 감추는 것보다 낫다."""
    if not raw:
        return "–"
    return EXIT_REASON_KO.get(raw, raw)


# 봇 청산 기록과 계좌 실현손익(income)을 같은 청산으로 볼 시각 차이. 거래소에서 직접
# 닫은 걸 봇이 나중에 reload 로 기록하면 봇 쪽은 주문 체결 시각, income 은 기록 시각이라
# 2초까지 벌어지는 게 실측됐다(10/6 EGLD 12:42:10 vs 12:42:08).
BOT_CLOSE_MATCH_SEC = range(-3, 4)


def _bot_closed_keys() -> set:
    """(바이낸스 심볼, 초단위 UTC 타임스탬프) -> 각 봇이 자기 DB에 남긴 청산들.

    수동 청산 이력을 만들 때, 계좌 실현손익 이벤트가 이 키와 겹치면 이미 위의
    recent_trades 로 표시되고 있는 봇의 청산이므로 제외한다. 실측 결과 봇이 낸
    주문의 체결 시각과 계좌 realized PnL 이벤트의 타임스탬프는 초 단위까지
    정확히 일치했다(같은 체결에 대한 두 기록이므로 당연하다).

    한계: 지금 컨테이너가 떠 있지 않은 봇(예: 중단된 v1, 예전 추세추종/평균회귀)의
    과거 청산은 이 함수가 알 방법이 없어 "수동"으로 잘못 표시될 수 있다. BOTS에
    현재 등록된 봇들의 최근 거래만 걸러낼 수 있다.
    """
    keys = set()
    for bot in BOTS:
        try:
            trades = call_bot(bot["url"], "/api/v1/trades", {"limit": 500}).get("trades", [])
        except Exception:  # noqa: BLE001
            continue
        for t in trades:
            if t.get("is_open") or not t.get("close_date"):
                continue
            sym = t["pair"].split(":")[0].replace("/", "")
            try:
                ts = int(
                    datetime.strptime(t["close_date"], "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except ValueError:
                continue
            keys.add((sym, ts))
    return keys


def _closing_fill_info(symbol: str, trade_ids: list, around_ms: int) -> dict:
    """청산을 이룬 체결들을 userTrades 에서 찾아 방향/평균가를 복원한다.

    income(REALIZED_PNL) 이벤트 자체에는 방향(롱/숏)이 없다 - 체결 기록에서
    가져와야 한다. tradeId 로 정확히 매칭하고, 실패하면 같은 시간대에 손익이
    실현된 체결(=청산 체결)로 갈음한다.
    """
    try:
        fills = binance_signed(
            "/fapi/v1/userTrades",
            {
                "symbol": symbol,
                "startTime": around_ms - 5 * 60 * 1000,
                "endTime": around_ms + 60 * 1000,
                "limit": 200,
            },
        )
    except Exception:  # noqa: BLE001
        return {}
    ids = {i for i in trade_ids if i is not None}
    matched = [f for f in fills if f.get("id") in ids]
    if not matched:
        matched = [f for f in fills if abs(float(f.get("realizedPnl", 0) or 0)) > 1e-9]
    if not matched:
        return {}
    qty = sum(float(f["qty"]) for f in matched)
    if qty <= 0:
        return {}
    avg_price = sum(float(f["price"]) * float(f["qty"]) for f in matched) / qty
    # SELL 체결로 손익이 실현됐다면 롱을 줄인 것(청산된 포지션은 롱이었다).
    # BUY 체결로 실현됐다면 숏을 줄인 것.
    is_short = matched[0].get("side") == "BUY"
    return {"is_short": is_short, "avg_price": avg_price, "qty": qty}




def _income_realized_pnl(start_ms: int) -> list[dict]:
    """/fapi/v1/income(REALIZED_PNL) 을 start_ms 이후 전부 긁어온다 (1000건 단위 페이지네이션).

    체결 방향까지 복원해야 하는 청산 이력 표시(fetch_manual_trade_history)와 달리,
    손익 합계만 필요한 곳(fetch_manual_pnl)에서는 이 함수 하나면 된다 -
    페어당 추가 조회가 없어 훨씬 가볍고, 그래서 훨씬 넓은 기간을 봐도 괜찮다.
    """
    out: list[dict] = []
    st = start_ms
    while True:
        batch = binance_signed(
            "/fapi/v1/income", {"incomeType": "REALIZED_PNL", "startTime": st, "limit": 1000}
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        st = batch[-1]["time"] + 1
        if len(out) > 20000:  # 안전장치 - 여기까지 갈 일은 실질적으로 없다
            break
    return out


# 손익 합계는 체결 방향 복원이 필요 없어 표시용 이력보다 훨씬 가볍다. 그래서
# 상대적인 "최근 N일" 대신 "이 프로그램으로 실전 매매를 시작한 시점" 이후로
# 고정 기준일을 둔다.
#
# 처음엔 롤링 1년을 썼는데, 실제로 확인해보니 이 계좌에는 이 프로젝트와 무관한
# 2026-03/06월의 오래된 손실(-38, -82 등)이 섞여 있어서 "요즘은 수동으로 번
# 것밖에 없는데 왜 마이너스냐"는 착시를 만들었다. 롤링 윈도우는 시간이 지나면
# 8월 거래까지 잘라먹으므로, 날짜를 고정해야 한다.
MANUAL_PNL_START = datetime(2026, 8, 14, tzinfo=timezone.utc)
MANUAL_PNL_START_MS = int(MANUAL_PNL_START.timestamp() * 1000)

# income 조회(및 total 합계)는 그래프 기간(14/30/60일)과 무관하게 항상
# MANUAL_PNL_START 이후 전체를 봐야 한다 - 캐시를 여기서 한 번만 하고,
# 기간별 daily 슬라이스는 그 위에서 API 재호출 없이 즉시 계산한다.
_manual_pnl_raw_cache = {"ts": 0.0, "data": None}
MANUAL_PNL_TTL = 180


def _fetch_manual_pnl_raw() -> tuple:
    """(총합, 수동으로 분류된 income 원본 목록). 실패하면 (None, [])."""
    now = time.time()
    if (
        _manual_pnl_raw_cache["data"] is not None
        and now - _manual_pnl_raw_cache["ts"] < MANUAL_PNL_TTL
    ):
        return _manual_pnl_raw_cache["data"]

    try:
        raw = _income_realized_pnl(MANUAL_PNL_START_MS)
    except Exception:  # noqa: BLE001
        # 실패해도 직전 값을 유지한다(없으면 빈 결과) - 다만 ts는 갱신해야 한다.
        # 안 그러면 캐시가 계속 만료 상태로 남아 폴링마다(4초) 재시도하게 된다.
        _manual_pnl_raw_cache["ts"] = now
        return _manual_pnl_raw_cache["data"] or (None, [])

    bot_keys = _bot_closed_keys()
    manual = [
        x for x in raw
        if not any((x["symbol"], x["time"] // 1000 + d) in bot_keys for d in BOT_CLOSE_MATCH_SEC)
    ]
    total = sum(float(x["income"]) for x in manual)
    result = (total, manual)
    _manual_pnl_raw_cache.update({"ts": now, "data": result})
    return result


def fetch_manual_pnl(days: int = 14) -> dict:
    """전체 계좌 실현손익 중 봇이 기록하지 않은 부분 = 수동 매매 손익.

    총합("total")은 항상 MANUAL_PNL_START(8/14) 이후 전체 기준이고, 그래프
    기간 선택(14/30/60일)과 무관하다. "daily"만 그 기간에 맞춰 슬라이스한다
    - 봇의 /api/v1/daily 도 같은 timescale 로 조회해서 차트에 같은 창으로
    나란히 그릴 수 있게 맞췄다.

    total 이 None이면 조회 실패다 - 0으로 조용히 채우면 "수동 매매로 손익이
    전혀 없다"와 "지금 조회가 안 된다"가 화면에서 구분이 안 된다.
    """
    total, manual = _fetch_manual_pnl_raw()
    if total is None:
        return {"total": None, "daily": []}

    by_day: dict = {}
    for x in manual:
        d = datetime.fromtimestamp(x["time"] / 1000, timezone.utc).date()
        by_day[d] = by_day.get(d, 0.0) + float(x["income"])
    today = datetime.now(timezone.utc).date()
    # starting_balance 는 0으로 둔다 - 봇처럼 정해진 스테이크 풀이 없어서
    # "그날 시작 잔고 대비 수익률" 자체가 성립하지 않는 개념이다. 프론트는
    # starting_balance 가 0이면 그 시리즈의 수익률(%)을 억지로 계산하지 않고
    # 그냥 0으로 둔다 - 그래프의 금액 누적선 자체엔 영향이 없다.
    daily = [
        {
            "date": (today - timedelta(days=i)).isoformat(),
            "abs_profit": by_day.get(today - timedelta(days=i), 0.0),
            "starting_balance": 0,
        }
        for i in range(days - 1, -1, -1)
    ]

    return {"total": total, "daily": daily}


# 화면(전체 청산 이력)은 봇 기록과 합쳐 최근 15건만 보여준다. 그래서 굳이
# 넓게 긁을 필요가 없고, 넓게 긁으면 건마다 userTrades 조회가 붙어 느려진다
# (실측: 30일치 94건 조회에 30초). 최근 며칠 + 최대 20건으로 제한한다.
MANUAL_HISTORY_LOOKBACK_MS = 7 * 24 * 3600 * 1000  # 최근 7일
MANUAL_HISTORY_MAX_EPISODES = 20

# income + userTrades 를 함께 물어야 해서 봇 조회보다 훨씬 무겁고, 수동 청산은
# 자주 일어나지 않으므로 길게 캐시한다 (대시보드는 4초마다 갱신됨).
_manual_history_cache = {"ts": 0.0, "data": None}
MANUAL_HISTORY_TTL = 180


def fetch_manual_trade_history() -> list[dict]:
    """계좌 실현손익 중 봇이 기록하지 않은 것 = 사람이 거래소에서 직접 처리한 청산.

    /fapi/v1/income(REALIZED_PNL) 은 계좌 전체의 실현손익을 봇/수동 구분 없이 준다.
    _bot_closed_keys() 와 겹치는 항목은 이미 봇의 recent_trades 로 표시되므로 뺀다.
    남는 항목은 봇 화이트리스트에 아예 없는 페어(TQQQ 등)이거나, 화이트리스트 안이라도
    봇 DB에 없는 시각의 청산 - 둘 다 사람이 직접 처리한 것이다.
    """
    now = time.time()
    if (
        _manual_history_cache["data"] is not None
        and now - _manual_history_cache["ts"] < MANUAL_HISTORY_TTL
    ):
        return _manual_history_cache["data"]

    try:
        raw = _income_realized_pnl(int(now * 1000) - MANUAL_HISTORY_LOOKBACK_MS)
    except Exception:  # noqa: BLE001
        # 실패해도 직전 값을 유지한다 - 없으면 빈 리스트, 있으면 그대로.
        # data 를 [] 로 덮어쓰면 다음 성공 때 기존 항목이 전부 "새로 생긴 것"으로
        # 보여 알림이 한꺼번에 쏟아진다.
        _manual_history_cache["ts"] = now
        return _manual_history_cache["data"] or []

    bot_keys = _bot_closed_keys()
    raw.sort(key=lambda x: (x["symbol"], x["time"]))

    # 같은 청산이 부분체결로 여러 건 잡히는 경우가 실측에서 있었다(TQQQ 사례).
    # 같은 심볼에서 몇 초 안에 몰린 항목들을 하나의 청산으로 묶는다.
    # 주의: 이 시점의 episodes는 심볼별로 그룹된 뒤 시간순이 아니므로, 아래에서
    # last_time 기준으로 다시 정렬한 뒤에야 "최근 것"을 골라낼 수 있다.
    episodes: list[dict] = []
    for x in raw:
        sym = x["symbol"]
        sec = x["time"] // 1000
        if any((sym, sec + d) in bot_keys for d in BOT_CLOSE_MATCH_SEC):
            continue  # 봇이 이미 기록한 청산
        if episodes and episodes[-1]["symbol"] == sym and x["time"] - episodes[-1]["last_time"] <= 5000:
            ep = episodes[-1]
            ep["income"] += float(x["income"])
            ep["trade_ids"].append(x.get("tradeId"))
            ep["last_time"] = x["time"]
        else:
            episodes.append(
                {
                    "symbol": sym,
                    "income": float(x["income"]),
                    "trade_ids": [x.get("tradeId")],
                    "last_time": x["time"],
                }
            )

    # 화면에 보일 만큼(최근 것)만 남긴다. 여기서 자르지 않으면 활발히 수동
    # 매매하는 계좌에서는 episode마다 userTrades 조회가 붙어 캐시 갱신 때마다
    # 수십 초씩 걸린다(실측됨).
    episodes.sort(key=lambda e: e["last_time"], reverse=True)
    episodes = episodes[:MANUAL_HISTORY_MAX_EPISODES]

    out = []
    for ep in episodes:
        info = _closing_fill_info(ep["symbol"], ep["trade_ids"], ep["last_time"])
        pct = None
        if info.get("avg_price") and info.get("qty"):
            pct = ep["income"] / (info["avg_price"] * info["qty"]) * 100
        out.append(
            {
                "pair": ep["symbol"].replace("USDT", "") + "/USDT",
                "is_short": info.get("is_short"),
                "close_profit_abs": ep["income"],
                "close_profit_pct": pct,
                "close_date": datetime.fromtimestamp(ep["last_time"] / 1000, timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "exit_reason": "manual",
                "exit_reason_ko": "수동 청산 (거래소 직접)",
            }
        )
    out.sort(key=lambda x: x["close_date"], reverse=True)

    # 알림 기록은 여기서 하지 않는다. 예전엔 이 함수가 직전 캐시(prev)와
    # 비교해서 "새로 감지된 것"만 자체적으로 record_notification 했는데,
    # 이 캐시는 TTL마다 통째로 갈아끼워지고 episode 묶음의 close_date(마지막
    # 체결 시각)가 뒤늦게 도착한 부분체결 때문에 재계산 때마다 몇 분씩
    # 밀릴 수 있어서, 같은 청산 하나가 "새 항목"으로 다시 잡혀 알림이 중복
    # 기록되는 문제가 있었다(텔레그램은 안 가고 대시보드 "최근 알림"에만
    # 같은 거래가 두 번 뜸 - 2026-09-20 확인). 지금은 backfill_notifications()
    # 가 이 함수의 결과를 파일 전체 기준 영구 dedup(_notif_seen_keys)으로
    # 걸러서 기록+텔레그램 전송까지 전담하므로, 여기서 따로 기록하면 안 된다.
    _manual_history_cache.update({"ts": now, "data": out})
    return out


# freqtrade가 보내는 상태/경고/오류 메시지는 자유 형식 영어 문장이라 완벽한 번역은
# 불가능함. 자주 나오는 패턴만 골라 한글로 바꿔주고, 매칭 안 되는 문장은 원문을
# 그대로 붙여서 최소한 "번역이 안 된 문장"이라는 걸 알 수 있게 함.
STATUS_TRANSLATIONS = [
    (r"^running$", "실행 중"),
    (r"^stopped$", "정지됨"),
    (r"^reloaded$", "재적재됨"),
    (r"^paused$", "일시정지됨"),
    (r"insufficient (funds|balance)", "잔고 부족"),
    (r"is not available.*market", "해당 거래소에서 거래 불가능한 페어"),
    (r"could not be filled", "주문이 체결되지 않음"),
    (r"unable to place (a )?(buy|sell|entry|exit|stoploss) order", "주문 실행 실패"),
    (r"cancell?ing (entry|exit|buy|sell) order", "주문 취소 처리 중"),
    (r"time ?out", "시간 초과(타임아웃)"),
    (r"connection (error|timeout|refused)", "거래소 연결 오류"),
    (r"rate limit", "요청 한도 초과(레이트 리밋)"),
    (r"exchange error", "거래소 오류"),
    (r"network error", "네트워크 오류"),
    (r"unfilled timeout", "미체결 주문 타임아웃"),
    (r"stoploss.*order", "손절 주문 처리"),
    (r"liquidat", "강제 청산(청산가 도달)"),
    (r"margin call", "마진콜"),
    (r"bot stopped", "봇 정지됨"),
    (r"bot started", "봇 시작됨"),
    (r"process died|fatal exception|traceback", "봇 프로세스 오류 발생"),
]


def translate_status_message(raw_text: str) -> str:
    if not raw_text:
        return raw_text
    lowered = raw_text.lower()
    for pattern, ko in STATUS_TRANSLATIONS:
        if re.search(pattern, lowered):
            return f"{ko}\n(원문: {raw_text})"
    # 매칭되는 패턴이 없으면 번역 실패를 명시하고 원문을 그대로 보여줌
    return f"[번역 미지원 - 원문 그대로]\n{raw_text}"


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=5,
    )


@app.post("/webhook")
def webhook_relay():
    # freqtrade의 webhook 기능은 문자열 템플릿(.format())만 지원해서
    # "매수/매도" 같은 조건부 한글 변환을 config 안에서 직접 못 함.
    # 그래서 freqtrade가 원본 필드를 그대로 이 엔드포인트로 보내면,
    # 여기서 원하는 한글 형식으로 조립해서 텔레그램으로 재전송함.
    if request.args.get("secret") != WEBHOOK_SECRET or not WEBHOOK_SECRET:
        return Response("forbidden", 403)

    data = request.get_json(force=True, silent=True) or {}
    event = data.get("event", "")
    bot_name = _display_bot_name(data.get("bot_name", "봇"))
    pair = data.get("pair", "?")
    direction = data.get("direction", "")  # "Long" or "Short"
    is_short = direction == "Short"

    now_iso = datetime.now(timezone.utc).isoformat()
    # entry_fill/exit_fill은 실제 체결시각(open_date/close_date)이 있으면 그걸
    # dedup 기준시각으로 쓴다. now_iso(웹훅 수신 시각)를 쓰면 backfill_notifications()가
    # 같은 거래를 close_date 기준으로 나중에 다시 보고 "새 거래"로 착각해 알림이
    # 두 번(다른 bot_name 표기로) 나가는 문제가 있었다 - 수신 지연이 1~2초만
    # 벌어져도 초 단위 dedup key가 어긋났기 때문.
    trade_dt_raw = data.get("close_date") or data.get("open_date")
    event_time = _normalize_freqtrade_dt(trade_dt_raw) if trade_dt_raw else now_iso

    if event == "entry_fill":
        if not _claim_notification(pair, event_time):
            return jsonify({"ok": True, "skipped": "duplicate"})
        # 롱 진입 = 매수 주문, 숏 진입 = 매도 주문
        side_ko = "매도" if is_short else "매수"
        text = (
            f"[{bot_name}] [선물 진입]\n"
            f"종목: {pair}\n"
            f"방향: {side_ko}\n"
            f"상태: 체결\n"
            f"====================="
        )
        send_telegram(text)
        record_notification(
            {
                "time": event_time,
                "event": "entry_fill",
                "bot_name": bot_name,
                "pair": pair,
                "side_ko": side_ko,
                "is_short": is_short,
            }
        )

    elif event == "exit_fill":
        if not _claim_notification(pair, event_time):
            return jsonify({"ok": True, "skipped": "duplicate"})
        # 롱 청산 = 매도 주문, 숏 청산(환매수) = 매수 주문 -> 진입과 반대
        side_ko = "매수" if is_short else "매도"
        try:
            profit_ratio_pct = float(data.get("profit_ratio", 0)) * 100
        except (TypeError, ValueError):
            profit_ratio_pct = 0.0
        try:
            profit_amount = float(data.get("profit_amount", 0))
        except (TypeError, ValueError):
            profit_amount = 0.0
        stake_currency = data.get("stake_currency", "USDT")
        exit_reason_raw = data.get("exit_reason", "")
        reason_ko = exit_reason_ko(exit_reason_raw)
        text = (
            f"[{bot_name}] [선물 청산]\n"
            f"종목: {pair}\n"
            f"방향: {side_ko}\n"
            f"상태: 청산 완료\n"
            f"실현 수익률: {profit_ratio_pct:.4f}%\n"
            f"실현 손익: {profit_amount:.4f} {stake_currency}\n"
            f"사유: {reason_ko}\n"
            f"====================="
        )
        send_telegram(text)
        record_notification(
            {
                "time": event_time,
                "event": "exit_fill",
                "bot_name": bot_name,
                "pair": pair,
                "side_ko": side_ko,
                "is_short": is_short,
                "profit_ratio_pct": profit_ratio_pct,
                "profit_amount": profit_amount,
                "stake_currency": stake_currency,
                "exit_reason_ko": reason_ko,
            }
        )

    elif event in ("status", "warning", "startup", "exception"):
        # freqtrade가 매매 외에 보내는 상태/경고/오류 메시지.
        # notification_settings에서 telegram 네이티브 쪽은 꺼두고, 이 경로로만
        # 한글 라벨을 붙여서 보냄 (완전 자동번역은 아니고 자주 나오는 패턴 매칭).
        raw_status = data.get("status", "")
        event_label = {
            "status": "상태 알림",
            "warning": "⚠️ 경고",
            "startup": "봇 시작",
            "exception": "🚨 오류",
        }[event]
        translated = translate_status_message(raw_status)
        text = f"[{bot_name}] [{event_label}]\n{translated}\n====================="
        send_telegram(text)
        record_notification(
            {
                "time": now_iso,
                "event": event,
                "bot_name": bot_name,
                "raw_status": raw_status,
                "translated": translated,
            }
        )

    return jsonify({"ok": True})


@app.get("/api/notifications")
def api_notifications():
    return jsonify([
        {**n, "bot_name": _display_bot_name(n["bot_name"])} if n.get("bot_name") else n
        for n in notifications
    ])



@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def _send_backfill_telegram(t: dict) -> None:
    """실시간 웹훅으로는 못 받은 청산(수동 청산이 대부분 - 바이낸스에서 직접
    닫으면 freqtrade가 아예 모르는 거래라 웹훅 자체가 없다)을 나중에
    backfill_notifications()가 발견했을 때 보낸다. webhook_relay()의
    exit_fill 문구와 거의 같지만, 실시간이 아니라 뒤늦게 찾아낸 것임을
    표시해서 체결 시각과 알림 도착 시각이 다를 수 있다는 걸 알려준다."""
    side_ko = "매수" if t.get("is_short") else "매도"
    try:
        profit_ratio_pct = float(t.get("profit_ratio_pct") or 0)
    except (TypeError, ValueError):
        profit_ratio_pct = 0.0
    try:
        profit_amount = float(t.get("profit_amount") or 0)
    except (TypeError, ValueError):
        profit_amount = 0.0
    text = (
        f"[{t['bot_name']}] [선물 청산 · 뒤늦게 발견됨]\n"
        f"종목: {t['pair']}\n"
        f"방향: {side_ko}\n"
        f"상태: 청산 완료\n"
        f"실현 수익률: {profit_ratio_pct:.4f}%\n"
        f"실현 손익: {profit_amount:.4f} USDT\n"
        f"사유: {t.get('exit_reason_ko') or '-'}\n"
        f"====================="
    )
    send_telegram(text)


def _send_entry_telegram(bot_name: str, pair: str, is_short: bool, late: bool) -> None:
    side_ko = "매도" if is_short else "매수"
    send_telegram(
        f"[{bot_name}] [선물 진입{' · 뒤늦게 발견됨' if late else ''}]\n"
        f"종목: {pair}\n"
        f"방향: {side_ko}\n"
        f"상태: 체결\n"
        f"====================="
    )


def _record_entry(bot_name: str, pair: str, is_short: bool, when_iso: str) -> None:
    record_notification(
        {
            "time": when_iso,
            "event": "entry_fill",
            "bot_name": bot_name,
            "pair": pair,
            "side_ko": "매도" if is_short else "매수",
            "is_short": is_short,
        }
    )


# 진입 알림 백필은 최근 이 시간 안에 연 거래만 본다. 웹훅 dedup 키가 체결시각
# (open_date) 기준으로 바뀌기 전(2026-09)의 옛 진입 알림은 키가 달라서, 범위를
# 넓히면 오래전 진입을 "놓친 것"으로 착각해 다시 보낸다.
ENTRY_BACKFILL_LOOKBACK = timedelta(hours=6)
_backfill_lock = threading.Lock()


def backfill_notifications() -> int:
    # 주기 루프와 포지션 감시 루프(_position_watch_loop)가 동시에 부를 수 있다
    with _backfill_lock:
        return _backfill_notifications_locked()


def _backfill_notifications_locked() -> int:
    """청산 이력(봇 + 수동)에는 있는데 '최근 알림'엔 없는 것들을 시간순으로
    채워 넣는다.

    실시간 웹훅을 놓친 경우(대시보드가 잠깐 죽어 있던 사이 청산된 것 등)나,
    수동 청산처럼 애초에 실시간으로 안 잡히는 것들이 대상이다. pair+초 단위
    시각으로 이미 있는 알림과 겹치는지 걸러내므로, 서버를 몇 번을 재시작해도
    중복으로 쌓이지 않는다.

    dedup 기준은 화면표시용 notifications(최근 200건 deque)가 아니라 파일
    전체 기준의 _notif_seen_keys 다 - 200건 밖으로 밀려난 옛 항목을 매
    120초마다 "새로 발견"으로 착각해 텔레그램을 하루종일 반복 발송하던
    버그가 있었다. _claim_notification()으로 원자적으로 확인하는 이유는
    위 2026-09-30 주석 참고 (웹훅 실시간 처리와의 경쟁 상태 방지).
    """
    items = []
    bot_trades_for_entries = []
    for bot in BOTS:
        try:
            summary = fetch_bot_summary(bot)
        except Exception:  # noqa: BLE001
            continue
        if not summary.get("connected"):
            continue
        bot_trades_for_entries.append(
            (bot["name"], summary.get("open_trades", []) + summary.get("recent_trades", []))
        )
        for t in summary.get("recent_trades", []):
            if not t.get("close_date"):
                continue
            items.append(
                {
                    "bot_name": bot["name"],
                    "pair": t["pair"],
                    "is_short": t.get("is_short"),
                    "profit_ratio_pct": t.get("close_profit_pct"),
                    "profit_amount": t.get("close_profit_abs"),
                    "exit_reason_ko": t.get("exit_reason_ko"),
                    "close_date": t["close_date"],
                }
            )
    # 봇 기록상 아직 열려 있는 페어의 최근 수동 청산은 곧 대조(_reconcile_bot_trades)로
    # 그 봇의 청산이 되므로 "수동"으로 먼저 알리지 않는다. 10분이 지나도 안 바뀌면
    # (대조 실패) 그때는 수동으로 알린다.
    pending_bot_pairs = {
        t["pair"].split(":")[0]
        for _, trades in bot_trades_for_entries
        for t in trades
        if not t.get("close_date")
    }
    hold_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    for t in fetch_manual_trade_history():
        if t["pair"] in pending_bot_pairs and t["close_date"] >= hold_cutoff:
            continue
        items.append(
            {
                "bot_name": "수동",
                "pair": t["pair"],
                "is_short": t.get("is_short"),
                "profit_ratio_pct": t.get("close_profit_pct"),
                "profit_amount": t.get("close_profit_abs"),
                "exit_reason_ko": t.get("exit_reason_ko"),
                "close_date": t["close_date"],
            }
        )

    # 진입: 웹훅을 놓친 봇 진입(대시보드 재시작 중 체결 등)을 찾아 알린다
    since = datetime.now(timezone.utc) - ENTRY_BACKFILL_LOOKBACK
    entries = []
    for bot_name, trades in bot_trades_for_entries:
        for t in trades:
            od = t.get("open_date")
            if not od or (t.get("has_open_orders") and not t.get("close_date")):
                continue  # 진입 주문이 아직 체결 전
            try:
                opened = datetime.strptime(od, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if opened >= since:
                entries.append((od, bot_name, t))
    entries.sort(key=lambda x: x[0])
    added = 0
    for od, bot_name, t in entries:
        when_iso = od.replace(" ", "T") + "+00:00"
        if not _claim_notification(t["pair"], when_iso):
            continue
        _record_entry(bot_name, t["pair"], bool(t.get("is_short")), when_iso)
        _send_entry_telegram(bot_name, t["pair"], bool(t.get("is_short")), late=True)
        added += 1

    # 오래된 것부터 순서대로 appendleft 해야 최종적으로 최신이 맨 앞에 온다
    # (notifications 파일을 처음 불러올 때 쓰는 것과 같은 원리 - 위 주석 참고).
    items.sort(key=lambda x: x["close_date"])
    for t in items:
        when_iso = t["close_date"].replace(" ", "T") + "+00:00"
        if not _claim_notification(t["pair"], when_iso):
            continue
        record_notification(
            {
                "time": when_iso,
                "event": "exit_fill",
                "bot_name": t["bot_name"],
                "pair": t["pair"],
                "is_short": t["is_short"],
                "profit_ratio_pct": t["profit_ratio_pct"],
                "profit_amount": t["profit_amount"],
                "stake_currency": "USDT",
                "exit_reason_ko": t["exit_reason_ko"],
            }
        )
        # 여기 들어온 건 _claim_notification을 통과한, 이 경로가 처음
        # 발견한 청산이다 - 웹훅이 이미 같은 순간 선점했다면 위에서
        # continue 되므로 중복 발송될 일은 없다.
        _send_backfill_telegram(t)
        added += 1
    if added:
        # 위에서 넣은 새 항목들은 서로간엔 시간순이어도, 이미 있던 알림들
        # 사이 어딘가에 끼어야 할 수 있다(뒤늦게 발견된 과거 이력 등) -
        # 통째로 다시 정렬해야 "맨 앞 = 최신" 불변식이 안 깨진다.
        _resort_notifications()
    return added


BACKFILL_NOTIF_REFRESH_SEC = 120  # 수동 청산을 텔레그램으로 받아보는 데 걸리는 최대 지연


def _backfill_notifications_loop() -> None:
    """예전엔 서버 시작할 때 딱 한 번만 돌았다 - 그러면 수동 청산은 다음 재배포
    때까지(며칠씩 걸릴 수 있음) 텔레그램을 못 받는다. 이제 주기적으로 돌려서
    수동 거래도 몇 분 안에 알림이 오게 한다."""
    while True:
        try:
            if _reconcile_bot_trades():
                time.sleep(3)
            backfill_notifications()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(BACKFILL_NOTIF_REFRESH_SEC)


# ============================================================
# 계좌 포지션 실시간 감시 (2026-10-06)
#
# 봇이 낸 체결은 freqtrade 웹훅으로 바로 오지만, 바이낸스에서 직접 연/닫은 포지션은
# 웹훅이 없어서 위 백필 주기(120초)와 수동 이력 캐시(180초)를 기다려야 해 최대 몇 분씩
# 늦게 알림이 왔다. 계좌 포지션을 몇 초마다 직접 보고, 바뀐 게 있으면 그 자리에서
# 백필을 돌린다. 청산 직후엔 실현손익(income) 기록이 늦게 잡힐 수 있어 몇 번 더 본다.
# 백필 주기 루프는 그대로 두어, 여기서 놓친 것도 다음 주기에 잡히게 한다.
# ============================================================
POSITION_WATCH_SEC = 5
# 새 포지션이 생기면 이만큼 기다렸다가, 그때도 봇 거래가 아니면 수동 진입으로 알린다
# (봇 진입은 웹훅이 먼저 처리한다).
MANUAL_ENTRY_GRACE_SEC = 20
# 청산 감지 뒤 몇 번 다시 본다. 봇 거래 대조(_reconcile_bot_trades)가 먼저 돌아야
# 봇 포지션의 직접 청산이 "수동"으로 먼저 잡히지 않는다(RECONCILE_MIN_AGE_SEC 참고).
BACKFILL_RECHECK_DELAYS = (2, 18, 45, 90)


# 봇이 연 포지션을 거래소에서 직접 닫으면(2026-10-09 요청) 그 청산을 "수동"이 아니라
# 그 봇의 거래로 기록한다 - freqtrade 의 /trades/{id}/reload 가 거래소 주문 내역에서
# 그 청산 주문을 찾아 거래를 닫는다(사유 sold_on_exchange = "수동 청산", 실제 체결가로
# 손익 계산, 봇 정지 중에도 동작). freqtrade 도 스스로 같은 복구를 하지만 포지션 정보를
# 30분마다만 새로 읽어서, 그 사이 봇을 정지하면 거래가 영영 "보유 중"으로 남았다 -
# 그러면 청산 기록이 없어 48시간 재진입 대기도 안 돌았다(10/8 RSI 6건).
RECONCILE_MIN_AGE_SEC = 15   # 봇 자신의 청산이 처리될 시간을 준다(그 사이엔 손대지 않음)
RECONCILE_RETRY_SEC = 300    # 같은 거래를 다시 시도하는 최소 간격
_reconcile_tried: dict = {}  # (bot id, trade id) -> 마지막 시도 시각
_position_gone_since: dict = {}  # (pair, side) -> 계좌에서 처음 안 보인 시각


def _reconcile_bot_trades() -> int:
    """봇 기록엔 열려 있는데 계좌엔 없는 포지션을 그 봇이 거래소 기록으로 닫게 한다."""
    try:
        rows = binance_signed("/fapi/v2/positionRisk")
    except Exception:  # noqa: BLE001
        return 0
    held = set()
    for p in rows:
        amt = float(p.get("positionAmt") or 0)
        if amt:
            base = p["symbol"][:-4] if p["symbol"].endswith("USDT") else p["symbol"]
            held.add((f"{base}/USDT:USDT", "short" if amt < 0 else "long"))
    now = time.time()
    fixed = 0
    seen_gone = set()
    for bot in BOTS:
        try:
            trades = call_bot(bot["url"], "/api/v1/status")
        except Exception:  # noqa: BLE001
            continue
        for t in trades:
            key = (t["pair"], "short" if t.get("is_short") else "long")
            if key in held or t.get("has_open_orders"):
                continue
            seen_gone.add(key)
            first = _position_gone_since.setdefault(key, now)
            if now - first < RECONCILE_MIN_AGE_SEC:
                continue
            tk = (bot["id"], t["trade_id"])
            if now - _reconcile_tried.get(tk, 0) < RECONCILE_RETRY_SEC:
                continue
            _reconcile_tried[tk] = now
            # reload 는 그 페어에서 진입 이후 체결된 주문을 전부 이 거래에 붙인다. 계좌를
            # 같이 쓰는 다른 봇이 나중에 같은 코인을 거래했으면 그 주문까지 섞이므로,
            # 진입 1건 + 청산 1건만 있을 때만 한다(아니면 손대지 않고 수동으로 남긴다).
            try:
                base = t["pair"].split("/")[0]
                orders = binance_signed("/fapi/v1/allOrders", {
                    "symbol": f"{base}USDT", "startTime": int(t["open_timestamp"]) - 10_000,
                })
                filled = [o for o in orders if float(o.get("executedQty") or 0) > 0]
                if len(filled) != 2:
                    _reconcile_tried[tk] = float("inf")
                    continue
            except Exception:  # noqa: BLE001
                continue
            try:
                post_bot(bot["url"], f"/api/v1/trades/{t['trade_id']}/reload")
                fixed += 1
            except Exception:  # noqa: BLE001
                pass
    for k in list(_position_gone_since):
        if k not in seen_gone:
            del _position_gone_since[k]
    return fixed


def _bot_open_pairs() -> set:
    pairs = set()
    for bot in BOTS:
        try:
            for t in call_bot(bot["url"], "/api/v1/status"):
                pairs.add((t["pair"].split(":")[0], bool(t.get("is_short"))))
        except Exception:  # noqa: BLE001
            pass
    return pairs


def _notify_manual_entry(symbol: str, amt: float, update_ms: int) -> None:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    pair = f"{base}/USDT"
    is_short = amt < 0
    if (pair, is_short) in _bot_open_pairs():
        return  # 봇이 연 포지션 - 웹훅/백필이 알린다
    when_iso = datetime.fromtimestamp((update_ms or time.time() * 1000) / 1000, timezone.utc).isoformat()
    if not _claim_notification(pair, when_iso):
        return
    _record_entry("수동", pair, is_short, when_iso)
    _send_entry_telegram("수동", pair, is_short, late=False)


def _position_watch_loop() -> None:
    prev = None
    pending_entries: dict = {}  # symbol -> (알림 판단 시각, 수량, updateTime)
    recheck_at: list = []
    while True:
        now = time.time()
        try:
            cur = {}
            for p in binance_signed("/fapi/v2/positionRisk"):
                amt = float(p.get("positionAmt") or 0)
                if amt:
                    cur[p["symbol"]] = (amt, int(p.get("updateTime") or 0))
            if prev is not None:
                changed = any(cur.get(sym, (0.0, 0))[0] != old for sym, (old, _) in prev.items())
                for sym, (amt, upd) in cur.items():
                    old = prev.get(sym, (0.0, 0))[0]
                    if old == 0 or (old > 0) != (amt > 0):
                        pending_entries[sym] = (now + MANUAL_ENTRY_GRACE_SEC, amt, upd)
                        changed = True
                if changed:
                    _account_cache["ts"] = 0  # 화면도 다음 갱신(4초) 때 바로 새 값
                    recheck_at = sorted(set(recheck_at) | {now + d for d in BACKFILL_RECHECK_DELAYS})
            prev = cur
        except Exception:  # noqa: BLE001
            pass

        for sym, (due, amt, upd) in list(pending_entries.items()):
            if now >= due:
                del pending_entries[sym]
                try:
                    _notify_manual_entry(sym, amt, upd)
                except Exception:  # noqa: BLE001
                    pass
        if recheck_at and now >= recheck_at[0]:
            recheck_at = [t for t in recheck_at if t > now]
            try:
                if _reconcile_bot_trades():
                    time.sleep(3)  # reload 결과(청산 웹훅)가 먼저 들어오게
            except Exception:  # noqa: BLE001
                pass
            _manual_history_cache["ts"] = 0
            _manual_pnl_raw_cache["ts"] = 0
            try:
                backfill_notifications()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(POSITION_WATCH_SEC)


# ============================================================
# 봇 헬스체크 - 2026-09-29 CoinGecko 요청이 쌓여 DB 커넥션 풀이 고갈되면서
# XSectMomentum API 전체가 응답을 멈췄는데, 아무도 알림을 못 받아서 사용자가
# 직접 화면 보고 알아챌 때까지 몰랐던 사고가 있었다. 그때 실제로 죽은 건
# /api/v1/status 같은 DB를 타는 엔드포인트였으므로(/ping 같은 순수 liveness
# 체크는 이런 종류의 장애를 못 잡는다), 그 엔드포인트를 그대로 헬스체크에
# 쓴다. 상태가 바뀔 때(정상->장애, 장애->복구)만 텔레그램을 보내 도배되지
# 않게 한다.
BOT_HEALTH_CHECK_SEC = 60
BOT_HEALTH_FAIL_THRESHOLD = 2  # 연속 실패 2회(최대 약 2분)부터 "장애"로 판단 - 순간 네트워크 지연 오탐 방지
_bot_health_state: dict = {}


def _check_bot_health_once() -> None:
    for bot in BOTS:
        state = _bot_health_state.setdefault(bot["id"], {"failures": 0, "down": False})
        error_text = None
        try:
            call_bot(bot["url"], "/api/v1/status")
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            error_text = str(exc)[:200]

        if ok:
            if state["down"]:
                send_telegram(
                    f"[{bot['name']}] [서버 상태]\n"
                    f"API 응답이 다시 정상적으로 돌아왔습니다.\n"
                    f"====================="
                )
            state["failures"] = 0
            state["down"] = False
        else:
            state["failures"] += 1
            if state["failures"] >= BOT_HEALTH_FAIL_THRESHOLD and not state["down"]:
                state["down"] = True
                send_telegram(
                    f"[{bot['name']}] [🚨 서버 문제 감지]\n"
                    f"API가 {BOT_HEALTH_FAIL_THRESHOLD}회 연속(최근 약 "
                    f"{BOT_HEALTH_FAIL_THRESHOLD * BOT_HEALTH_CHECK_SEC}초) 응답하지 않습니다.\n"
                    f"포지션 관리(익절/보유기간 만기 청산)가 멈춰있을 수 있으니 확인이 필요합니다.\n"
                    f"오류: {error_text}\n"
                    f"====================="
                )


def _bot_health_check_loop() -> None:
    while True:
        try:
            _check_bot_health_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(BOT_HEALTH_CHECK_SEC)


# ============================================================
# 코인 뉴스 - 여러 매체 RSS를 모아 중복 제거 + 한글 번역
#
# "빨리 올리는 곳들 위주로 싹 다 긁어와서" 요청에 맞춰, 실시간성이 좋은
# 매체들의 RSS 피드를 모은다. 서버(AWS)에서 실제로 접근되는지, 어떤
# 포맷인지 하나씩 실측하고 골랐다 - bitcoinmagazine.com은 403으로 막혀서
# 뺐고, blockworks.co는 200은 오는데 <item> 자체가 없는 포맷이라 뺐다.
NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("NewsBTC", "https://www.newsbtc.com/feed/"),
    ("TheBlock", "https://www.theblock.co/rss.xml"),
    ("CryptoNews", "https://cryptonews.com/news/feed/"),
    ("U.Today", "https://u.today/rss"),
]

# 미국 증시·거시 뉴스 (2026-10-06 추가 - 왼쪽 뉴스 목록을 없애고, 코인/미국증시에
# 영향을 줄 만한 사건만 골라 "주요 뉴스" 한 줄과 AI 시황에 반영하기 위해).
# 서버에서 실제로 받아지는 것만 골랐다(Yahoo/FXStreet 는 XML 이 깨져서 뺐다).
MARKET_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
]

NEWS_MAX_ITEMS = 30  # 이 이상은 번역 비용/시간만 늘고 화면에서 의미가 없다
NEWS_REFRESH_SEC = 300  # 5분. 주요 뉴스 감지가 너무 늦지 않게(번역은 새 기사만 하므로 부담 적음)

_news_cache = {"ts": 0.0, "data": []}
_news_lock = threading.Lock()

# 번역 결과 캐시(원문 -> 번역문). 캐시가 없으면 대부분 재활용되는 기사를
# 10분마다 30건 x 2필드씩 매번 다시 번역 요청하게 되어, MyMemory 무료
# 일일 한도(익명 기준 하루 몇백 건 수준)를 몇 시간 만에 다 써버렸다
# (실제로 겪음: 오후에 이미 "YOU USED ALL AVAILABLE FREE TRANSLATIONS
# FOR TODAY" 429를 받기 시작함). 같은 원문은 캐시에서 즉시 돌려주고,
# 실제로 API를 부르는 건 새로 나온 기사뿐이라 호출량이 크게 준다.
#
# 그런데도 한도가 계속 다 닳는 걸 보고서야 알아챈 두 번째 원인: 이 캐시가
# 메모리에만 있어서, 대시보드를 배포/재시작할 때마다(하루에도 여러 번)
# 통째로 날아갔다. 재시작 직후 첫 새로고침에서 그 시점 30건 x 2필드가
# 전부 "처음 보는 텍스트"가 되어 한 번에 재번역되는 게 반복되며 한도를
# 스스로 계속 깎아먹고 있었다(starting_capital_cache 와 같은 종류의
# 실수). 파일로 저장해서 재시작에도 살아남게 한다.
_TRANSLATION_CACHE_FILE = ROOT / "translation_cache.json"


def _load_translation_cache() -> dict:
    try:
        return json.loads(_TRANSLATION_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_translation_cache() -> None:
    try:
        _TRANSLATION_CACHE_FILE.write_text(json.dumps(_translation_cache), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_translation_cache: dict[str, str] = _load_translation_cache()
_TRANSLATION_CACHE_MAX = 1000


def _strip_html(text: str) -> str:
    """RSS description에 섞여 오는 HTML 태그 제거."""
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _parse_rss(source: str, xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = _strip_html(item.findtext("description") or "")[:220]
        pub_raw = item.findtext("pubDate") or ""
        pub = None
        try:
            pub = parsedate_to_datetime(pub_raw)
        except (TypeError, ValueError):
            try:
                pub = datetime.fromisoformat(pub_raw.strip())
            except ValueError:
                pub = None
        if pub is not None and pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if not title or not link:
            continue
        out.append(
            {"source": source, "title": title, "link": link, "summary": summary, "published": pub}
        )
    return out


def _dedupe_news(items: list[dict]) -> list[dict]:
    """제목 유사도로 중복 제거 - 여러 매체가 같은 사건을 다른 제목으로 쓰는 경우가 흔하다.
    호출 전에 최신순 정렬을 해두면, 같은 사건이 겹칠 때 더 최근(=보통 더 자세한) 쪽이 남는다.
    """
    kept = []
    for it in items:
        norm = _norm_title(it["title"])
        if any(SequenceMatcher(None, norm, _norm_title(k["title"])).ratio() > 0.8 for k in kept):
            continue
        kept.append(it)
    return kept


def _translate_to_ko(text: str) -> str:
    """MyMemory 무료 번역(키 불필요). 실패하면 원문을 그대로 돌려준다 -
    번역이 안 됐다고 기사 자체를 감출 이유는 없다.

    구글 번역 비공식 엔드포인트도 시도해봤으나 이 서버 IP에서 자동화 요청으로
    차단당했다("Sorry... automated queries") - 그래서 MyMemory를 쓴다.

    같은 원문은 _translation_cache 에서 바로 돌려주고, API는 처음 보는
    텍스트에만 부른다(위 _translation_cache 정의부의 주석 참고 - 캐시
    없이 매 새로고침마다 전부 다시 부르다 무료 일일 한도를 소진한 적 있다).
    """
    if not text:
        return text
    if text in _translation_cache:
        return _translation_cache[text]
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:490], "langpair": "en|ko"},
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("responseStatus") not in (200, "200"):
            raise ValueError(data.get("responseDetails") or "translate quota/error")
        translated = data.get("responseData", {}).get("translatedText")
        result = translated or text
    except Exception:  # noqa: BLE001
        # 실패(한도 초과 등)는 캐시에 남기지 않는다 - 원문을 그대로 캐시해버리면
        # 한도가 풀린 뒤에도 다음 새로고침마다 재시도하지 않고 계속 원문만 돈다.
        time.sleep(0.15)
        return text
    time.sleep(0.15)  # 번역 API를 너무 몰아치지 않으려고 실제 호출 후에만 살짝 텀을 둔다
    if len(_translation_cache) >= _TRANSLATION_CACHE_MAX:
        _translation_cache.pop(next(iter(_translation_cache)))
    _translation_cache[text] = result
    return result


def _refresh_news_once() -> None:
    all_items = []
    for source, url in NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            all_items += _parse_rss(source, resp.text)
        except Exception:  # noqa: BLE001
            continue  # 매체 하나가 죽어도 나머지로 계속한다

    all_items.sort(
        key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
    all_items = _dedupe_news(all_items)[:NEWS_MAX_ITEMS]

    out = []
    for it in all_items:
        out.append(
            {
                "source": it["source"],
                "title": _translate_to_ko(it["title"]),
                "summary": _translate_to_ko(it["summary"]),
                "link": it["link"],
                "published": it["published"].isoformat() if it["published"] else None,
            }
        )
        # 번역 API(익명, 무료)를 너무 몰아치지 않으려고 항목 사이에 살짝 텀을 둔다
        time.sleep(0.15)

    market_items = []
    for source, url in MARKET_FEEDS:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            market_items += _parse_rss(source, resp.text)
        except Exception:  # noqa: BLE001
            continue
    market_items.sort(
        key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
    market_items = _dedupe_news(market_items)[:NEWS_MAX_ITEMS]

    with _news_lock:
        _news_cache.update({
            "ts": time.time(),
            "data": out,
            "market": [
                {"source": it["source"], "title": it["title"], "link": it["link"],
                 "published": it["published"].isoformat() if it["published"] else None}
                for it in market_items
            ],
        })
    _save_translation_cache()

    # 주요 뉴스 판별은 원문(영어) 기준 - 번역 품질과 무관하게 판단하게
    try:
        _detect_major_news(
            [dict(it, category_hint="코인") for it in all_items]
            + [dict(it, category_hint="미국증시") for it in market_items]
        )
    except Exception:  # noqa: BLE001
        pass


def _news_refresh_loop() -> None:
    """백그라운드 스레드. RSS 조회 + 번역(최대 30건 x 2 = 최대 60회 API 호출)은
    수십 초가 걸릴 수 있어서, 요청 스레드에서 동기로 하면 그동안 다른 API가
    다 막힌다. 그래서 별도 스레드에서 미리 채워두고, /api/news 는 캐시만
    즉시 돌려준다."""
    while True:
        try:
            _refresh_news_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(NEWS_REFRESH_SEC)


@app.get("/api/news")
def api_news():
    with _news_lock:
        return jsonify({"news": list(_news_cache["data"])})


# ---------------------------------------------------------------------------
# 주요 뉴스 (2026-10-06)
#
# 새로 들어온 기사(코인 + 미국증시·거시)를 GPT 에게 "코인 시장이나 미국 증시 전체를
# 움직일 만한가" 1~5점으로 매기게 해서, 4점 이상만 대시보드 상단 한 줄 뉴스와
# AI 시황의 "주요 이벤트"로 보여준다. 5점짜리가 새로 나오면 AI 시황을 30분 주기를
# 기다리지 않고 바로 다시 쓴다(비용 때문에 최소 간격 AI_OPINION_EVENT_MIN_GAP).
# 한 번 매긴 기사는 링크 기준으로 기억해서(파일 저장) 재시작해도 다시 안 매긴다.
# ---------------------------------------------------------------------------
MAJOR_NEWS_FILE = ROOT / "major_news.json"
MAJOR_NEWS_MIN_IMPORTANCE = 4
MAJOR_NEWS_KEEP = timedelta(hours=24)
MAJOR_NEWS_SCORE_WINDOW = timedelta(hours=6)  # 이보다 오래된 기사는 "지금 일어난 일"이 아니라 안 매긴다
MAJOR_NEWS_BATCH = 40
_major_news_lock = threading.Lock()


def _load_major_news() -> dict:
    try:
        d = json.loads(MAJOR_NEWS_FILE.read_text(encoding="utf-8"))
        return {"seen": d.get("seen", {}), "items": d.get("items", [])}
    except Exception:  # noqa: BLE001
        return {"seen": {}, "items": []}


_major_news = _load_major_news()


def _save_major_news() -> None:
    try:
        MAJOR_NEWS_FILE.write_text(json.dumps(_major_news, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _score_headlines(batch: list[dict]) -> list[dict]:
    lines = "\n".join(
        f"{i}. [{it['source']}] {it['title']} — {(it.get('summary') or '')[:160]}"
        for i, it in enumerate(batch)
    )
    prompt = f"""아래는 방금 수집된 뉴스 헤드라인이다(코인 매체 + 미국 증시·거시 매체).
각 기사가 "크립토 시장 전체" 또는 "미국 증시(나스닥·S&P500) 전체"의 가격에 단기적으로 영향을 줄 수 있는 정도를 1~5로 매겨라.

5 = 시장 전체를 즉시 움직일 수 있는 사건 (FOMC 금리 결정·연준 의장 발언, CPI/고용 등 핵심 지표 발표와 예상 이탈,
    대형 거래소 해킹·파산, 현물 ETF 승인/거부, 주요국 규제·금지 발표, 전쟁·지정학 충격, 시총 최상위 기업 실적 쇼크)
4 = 상당한 영향 가능 (주요 지표 발표 예정/결과, 대형 기관의 대규모 매수·매도, 주요 코인 대형 업그레이드·상장폐지,
    빅테크 실적·가이던스, 국채금리·달러 급변)
3 = 일부 종목/섹터에 영향
2 = 시장 영향 미미 (분석·전망 칼럼, 가격 해설)
1 = 무관 (홍보, 소형 프로젝트, 개별 소형주)

가격 움직임을 사후 해설하는 기사("비트코인 3% 상승" 등)는 새 사건이 아니면 2 이하로 매겨라.
headline_ko 는 한국어 한 줄(40자 이내, 핵심 사실만), impact_ko 는 코인/미국증시에 줄 수 있는 영향 한 문장.
수치나 사실을 지어내지 마라.

{lines}"""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": AI_OPINION_MODEL,
            "messages": [
                {"role": "system", "content": "너는 시장 영향도를 냉정하게 분류하는 금융 뉴스 데스크 에디터다. 과장하지 않는다."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "headline_scores",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                                        "category": {"type": "string", "enum": ["코인", "미국증시", "거시·정책", "지정학"]},
                                        "headline_ko": {"type": "string"},
                                        "impact_ko": {"type": "string"},
                                    },
                                    "required": ["id", "importance", "category", "headline_ko", "impact_ko"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["items"],
                        "additionalProperties": False,
                    },
                },
            },
            "temperature": 0.1,
            "max_tokens": 4000,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"]).get("items", [])


def _detect_major_news(items: list[dict]) -> None:
    if not OPENAI_API_KEY:
        return
    now = datetime.now(timezone.utc)
    with _major_news_lock:
        seen = _major_news["seen"]
        fresh = [
            it for it in items
            if it["link"] not in seen and it.get("published") and now - it["published"] <= MAJOR_NEWS_SCORE_WINDOW
        ]
    fresh.sort(key=lambda x: x["published"], reverse=True)
    fresh = fresh[:MAJOR_NEWS_BATCH]
    if not fresh:
        return
    scored = _score_headlines(fresh)

    new_top = []
    with _major_news_lock:
        for sc in scored:
            i = sc.get("id")
            if not isinstance(i, int) or not 0 <= i < len(fresh):
                continue
            it = fresh[i]
            _major_news["seen"][it["link"]] = now.isoformat()
            if sc["importance"] >= MAJOR_NEWS_MIN_IMPORTANCE:
                entry = {
                    "time": it["published"].isoformat(),
                    "found_at": now.isoformat(),
                    "importance": sc["importance"],
                    "category": sc["category"],
                    "headline": sc["headline_ko"],
                    "impact": sc["impact_ko"],
                    "source": it["source"],
                    "link": it["link"],
                    "title_en": it["title"],
                }
                _major_news["items"].append(entry)
                if sc["importance"] >= 5:
                    new_top.append(entry)
        # 응답에서 빠진 기사도 "본 것"으로 남겨 매번 다시 매기지 않게
        for it in fresh:
            _major_news["seen"].setdefault(it["link"], now.isoformat())
        cutoff = now - MAJOR_NEWS_KEEP
        _major_news["items"] = [
            e for e in _major_news["items"] if datetime.fromisoformat(e["time"]) >= cutoff
        ]
        _major_news["items"].sort(key=lambda e: e["time"], reverse=True)
        # seen 은 이틀치만 유지(피드에서 이미 사라진 링크)
        old = (now - timedelta(days=2)).isoformat()
        _major_news["seen"] = {k: v for k, v in _major_news["seen"].items() if v >= old}
    _save_major_news()
    if new_top:
        _request_ai_opinion_refresh(new_top[0]["headline"])


def major_news_items() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - MAJOR_NEWS_KEEP
    with _major_news_lock:
        return [e for e in _major_news["items"] if datetime.fromisoformat(e["time"]) >= cutoff]


@app.get("/api/major_news")
def api_major_news():
    return jsonify({"items": major_news_items()})


# ===================== AI 시황 의견 (참고용, 자동매매와 무관) =====================
#
# 이 프로젝트의 실전 봇(BoxBreakoutV2/XSectMomentum)은 전부 수개월치 백테스트로
# "이 규칙이 통계적 엣지가 있는가"를 검증하고 나서 실전에 올렸다. LLM이 그때그때
# 내놓는 시황 의견은 같은 방식으로 재현/검증할 방법이 없다(같은 입력에도 매번
# 다른 답이 나올 수 있고, 과거 시점의 답을 다시 받아볼 수도 없음) - 그래서 이건
# 절대 주문에 연결하지 않고, 사람이 참고만 하는 화면 한 칸으로만 존재한다.
#
# 뉴스 번역(MyMemory)과 다른 점: 이건 유료 API라서(무료 재시도 X) 대시보드
# 재배포 때마다 캐시가 날아가 즉시 재호출되는 걸 막는 게 이번엔 "한도 초과"가
# 아니라 "돈이 샌다"의 문제다 - 위 두 캐시(starting_capital, translation)에서
# 겪은 것과 같은 실수를 반복하지 않도록 처음부터 파일에 저장해서 재시작에도
# 살아남게 하고, TTL이 남아있으면 재시작 직후에도 재호출하지 않는다.
AI_OPINION_MODEL = "gpt-4o-mini"
AI_OPINION_REFRESH_SEC = 30 * 60  # 30분 - 시황 요약이 4초마다 바뀔 이유가 없고 호출당 비용도 든다
AI_OPINION_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
AI_OPINION_CACHE_FILE = ROOT / "ai_opinion_cache.json"
_ai_opinion_lock = threading.Lock()


def _load_ai_opinion_cache() -> dict:
    try:
        return json.loads(AI_OPINION_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"ts": 0.0, "data": None}


def _save_ai_opinion_cache() -> None:
    try:
        AI_OPINION_CACHE_FILE.write_text(json.dumps(_ai_opinion_cache), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_ai_opinion_cache: dict = _load_ai_opinion_cache()

# 주요 뉴스(5점)가 새로 나오면 30분 주기를 기다리지 않고 다시 쓴다. 다만 큰 사건 때
# 비슷한 기사가 연달아 나오므로 이 간격 안에서는 한 번만.
AI_OPINION_EVENT_MIN_GAP = 10 * 60
_ai_opinion_event = {"pending": None}


def _request_ai_opinion_refresh(reason: str) -> None:
    _ai_opinion_event["pending"] = reason


def _refresh_ai_opinion_once(trigger: str | None = None) -> None:
    if not OPENAI_API_KEY:
        return

    try:
        tickers = _fetch_futures_tickers(AI_OPINION_SYMBOLS)
    except Exception:  # noqa: BLE001
        tickers = []
    with _news_lock:
        headlines = [n["title"] for n in _news_cache["data"][:15]]
        market_headlines = [f"[{n['source']}] {n['title']}" for n in _news_cache.get("market", [])[:12]]
    majors = major_news_items()[:10]

    def _fmt_ticker(t: dict) -> str:
        rng = t["high"] - t["low"]
        pos_in_range = (t["price"] - t["low"]) / rng * 100 if rng > 0 else 50.0
        return (
            f"- {t['symbol']}: ${t['price']:,.4g} ({t['change_pct']:+.2f}% / 24h), "
            f"24h 레인지 ${t['low']:,.4g}~${t['high']:,.4g} (레인지 내 위치 {pos_in_range:.0f}%), "
            f"24h 거래대금 ${t['quote_volume']:,.0f}"
        )

    market_lines = "\n".join(_fmt_ticker(t) for t in tickers) or "(시세 조회 실패)"
    news_lines = "\n".join(f"- {h}" for h in headlines) or "(뉴스 없음)"
    us_news_lines = "\n".join(f"- {h}" for h in market_headlines) or "(뉴스 없음)"
    major_lines = "\n".join(
        f"- (중요도 {e['importance']}, {e['category']}, {e['time'][:16].replace('T', ' ')} UTC) {e['headline']} → {e['impact']}"
        for e in majors
    ) or "(지난 24시간 주요 이벤트 없음)"

    # 롱/숏 판단은 GPT가 아니라 매일 결과로 채점·재학습되는 예측 모델(predictor.py)이
    # 한다 - GPT는 결과를 보고 배우지 못하기 때문. GPT는 그 판단과 근거를 해설한다.
    try:
        pred = predictor.payload()
    except Exception:  # noqa: BLE001
        pred = {}
    model_calls = {p["symbol"].replace("USDT", ""): p for p in (pred.get("today") or [])}
    pm = pred.get("model") or {}
    ho = pm.get("holdout") or {}
    live = pred.get("live") or {}
    if model_calls:
        call_lines = "\n".join(
            f"- {s}: {p['decision']} (오늘 상승확률 {p['prob_up'] * 100:.1f}%, 확신도 {p['confidence']}) "
            f"주요 근거: " + ", ".join(f"{d['label']}({d['direction']} 쪽)" for d in p["drivers"])
            for s, p in model_calls.items()
        )
        reliability = (
            f"검증(최근 180일) 정확도 {ho.get('acc', 0) * 100:.1f}% vs '항상 롱' {ho.get('long_acc', 0) * 100:.1f}%, "
            f"실전 누적 {live.get('acc', '-')}% ({live.get('n', 0)}건) vs 항상 롱 {live.get('always_long', '-')}%"
        )
        model_section = f"""
[예측 모델의 오늘(UTC 하루) 판단 - 차트지표·펀딩비·미국증시·뉴스점수로 학습하고 매일 채점되는 모델]
{call_lines}
모델 신뢰도: {reliability}
"""
        view_rule = ("각 코인의 view는 반드시 위 예측 모델의 판단(롱/숏)을 그대로 쓰고, reasoning/detail에서 "
                     "모델이 그렇게 판단한 근거(주요 근거 항목)를 시세·뉴스와 함께 해설하라. 모델 신뢰도가 "
                     "기준선('항상 롱')보다 낮거나 비슷하면 그 사실과 '참고 수준의 신호'라는 점을 분명히 밝혀라.")
    else:
        model_section = ""
        view_rule = "예측 모델 판단이 아직 없으므로 view는 '중립'으로 두라."

    prompt = f"""아래는 지금 시점의 바이낸스 선물 주요 종목 시세와 최근 크립토 뉴스 헤드라인이다.

[시세 (24시간 변동률·레인지·거래대금)]
{market_lines}

[최근 크립토 뉴스 헤드라인]
{news_lines}

[최근 미국 증시·거시 뉴스 헤드라인 (원문)]
{us_news_lines}

[지난 24시간 주요 이벤트 (코인/미국증시에 영향이 큰 것으로 분류된 뉴스)]
{major_lines}
{model_section}
이 정보를 바탕으로 BTC, ETH, SOL, XRP 각각에 대해 지금 시점 기준 시황 의견을 한국어로,
전문 트레이더가 데스크 동료에게 공유하는 리서치 노트 톤으로 작성해라. 위에 주어진 가격·
24h 레인지·거래대금·뉴스 헤드라인·예측 모델 정보 이외의 수치는 절대 지어내지 마라 - 모르면
"판단 근거 부족"이라고 써라. {view_rule}

각 항목마다 "짧은 요약"과 "상세 리포트" 두 버전을 만들어라. 상세 리포트는 화면에서
클릭해야 펼쳐지는 영역이라 분량 제약이 없으니, 문단을 나눠 충분히 상세하게 써라.

다음 JSON 스키마로만 답하라(다른 텍스트 없이):
{{
  "market_summary": "전체 시장 2~3문장 요약 (카드가 접혀있을 때 보이는 짧은 버전)",
  "market_report": "전체 시장 상세 리포트. 다음 흐름으로 3~5개 문단: (1)시장 개요 - 지금 24h 가격대·거래대금으로 본 전반적 분위기 (2)주요 동인 - 주요 이벤트·미국 증시/거시 뉴스·크립토 뉴스에서 확인되는 촉매·이슈 (3)자산간 상대적 흐름 - BTC 대비 알트 강약, 레인지 내 위치 비교 (4)리스크 요인 (5)전망 - 단기적으로 주시할 포인트. 각 문단은 3~5문장.",
  "coins": [
    {{
      "symbol": "BTC", "view": "롱|숏|중립", "confidence": "높음|중간|낮음",
      "reasoning": "1~2문장 근거 (카드가 접혀있을 때 보이는 짧은 버전)",
      "detail": "해당 종목 상세 리포트 2~4문단: 24h 레인지 내 포지션과 거래대금이 시사하는 것, 뉴스에서 이 종목과 관련된 촉매, BTC 대비 상대강도, 지금 시점의 리스크 요인과 무효화 조건(이 의견이 틀렸다고 판단할 기준), 짧은 결론"
    }},
    ... (ETH, SOL, XRP도 같은 형식)
  ],
  "caveat": "이 의견의 한계에 대한 짧은 한 문장 (예: 표본이 뉴스 헤드라인 수준이라 근거가 얕다는 점 등)"
}}"""

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": AI_OPINION_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "너는 헤지펀드 데스크의 크립토 담당 애널리스트다. 확정적인 예측이 아니라 "
                        "가능성 기반의 의견을 제시하고, 과장하지 않는다. 주어지지 않은 수치나 "
                        "지표는 절대 지어내지 않는다. 전문적이고 구체적인 문장으로 쓰되 불필요한 "
                        "수식어는 배제한다. 반드시 요청된 JSON 스키마로만 응답한다."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # 2026-09-21: json_object 모드에서 가끔 coins 배열 구조를 안 지키고
            # (ETH/SOL/XRP가 배열 원소가 아니라 BTC 객체 안에 중첩되는 등) 스키마를
            # 이탈해서, 화면에 비트코인 하나만 남는 문제가 있었다. strict json_schema
            # 로 바꿔서 이 구조 자체를 모델이 어길 수 없게 강제한다.
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "market_opinion",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "market_summary": {"type": "string"},
                            "market_report": {"type": "string"},
                            "coins": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "symbol": {"type": "string"},
                                        "view": {"type": "string", "enum": ["롱", "숏", "중립"]},
                                        "confidence": {"type": "string", "enum": ["높음", "중간", "낮음"]},
                                        "reasoning": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["symbol", "view", "confidence", "reasoning", "detail"],
                                    "additionalProperties": False,
                                },
                            },
                            "caveat": {"type": "string"},
                        },
                        "required": ["market_summary", "market_report", "coins", "caveat"],
                        "additionalProperties": False,
                    },
                },
            },
            "temperature": 0.3,
            "max_tokens": 3000,
        },
        timeout=45,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    # strict 스키마로도 혹시 모를 이상 응답을 한 번 더 막는다 - coins가 배열이
    # 아니거나 원소에 symbol이 없으면 캐시를 덮어쓰지 않고 직전 값을 유지한다
    # (비트코인 하나만 남는 문제를 다시 겪느니 갱신을 건너뛰는 게 낫다).
    coins = parsed.get("coins")
    if not isinstance(coins, list) or not coins or not all(
        isinstance(c, dict) and c.get("symbol") for c in coins
    ):
        raise ValueError(f"AI 응답의 coins 구조가 예상과 다름: {coins!r}")

    # 롱/숏과 확신도는 GPT가 바꿔 쓰지 못하게 모델 값으로 확정한다.
    for c in coins:
        call = model_calls.get(str(c.get("symbol", "")).upper())
        if call:
            c["view"] = call["decision"]
            c["confidence"] = call["confidence"]
            c["prob_up"] = call["prob_up"]

    with _ai_opinion_lock:
        _ai_opinion_cache.update(
            {
                "ts": time.time(),
                "data": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "model": AI_OPINION_MODEL,
                    # 주요 뉴스 때문에 앞당겨 다시 쓴 경우 그 뉴스 (화면에 표시)
                    "trigger": trigger,
                    **parsed,
                },
            }
        )
    _save_ai_opinion_cache()


def _ai_opinion_refresh_loop() -> None:
    """뉴스 스레드와 달리 '시작하자마자 한 번' 무조건 부르지 않는다 - 재배포로
    막 재시작됐을 때 파일 캐시가 아직 TTL 안에 있으면 그대로 두고, 진짜
    만료됐을 때만 유료 API를 호출한다."""
    while True:
        try:
            age = time.time() - _ai_opinion_cache.get("ts", 0)
            event = _ai_opinion_event["pending"]
            if event and age >= AI_OPINION_EVENT_MIN_GAP:
                _ai_opinion_event["pending"] = None
                _refresh_ai_opinion_once(trigger=event)
            elif age >= AI_OPINION_REFRESH_SEC:
                _ai_opinion_event["pending"] = None
                _refresh_ai_opinion_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


@app.get("/api/ai_prediction")
def api_ai_prediction():
    """예측 모델의 오늘 판단, 실전 채점 성적, 모델 검증 성적 (참고용)."""
    try:
        return jsonify(predictor.payload())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 200


def _news_headlines_for_predictor() -> list[str]:
    with _news_lock:
        return [n["title"] for n in _news_cache["data"][:25]]


@app.get("/api/ai_opinion")
def api_ai_opinion():
    if not OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY가 설정되지 않았습니다.", "data": None}), 200
    with _ai_opinion_lock:
        data = _ai_opinion_cache.get("data")
    if data is None:
        return jsonify({"error": None, "data": None, "pending": True}), 200
    return jsonify({"error": None, "data": data})


if __name__ == "__main__":
    # 로컬 개발 시엔 기본값(127.0.0.1)만 노출됨. 서버에 배포해서 외부 접속을
    # 받아야 할 때만 DASHBOARD_HOST=0.0.0.0 을 명시적으로 지정해서 실행할 것
    # (보안그룹 등 방화벽에서 이미 접근을 제한하고 있어야 안전함)
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")

    # 뉴스는 백그라운드 스레드가 계속 갱신한다. _news_refresh_loop 자체가
    # 시작하자마자 한 번 채우고 도니, 스레드를 하나만 띄운다(이걸 놓치고
    # _refresh_news_once() 를 따로 한 번 더 부르면 두 스레드가 동시에 같은
    # RSS/번역 API를 이중으로 호출하게 된다). 알림 백필도 같은 이유로
    # _backfill_notifications_loop 하나만 띄운다(시작하자마자 한 번 돌고
    # 이후 주기적으로 재실행함 - 예전엔 시작할 때 한 번만 돌아서 수동 청산이
    # 다음 재배포 때까지 텔레그램을 못 받았다).
    threading.Thread(target=_news_refresh_loop, daemon=True).start()
    threading.Thread(target=_ai_opinion_refresh_loop, daemon=True).start()
    threading.Thread(target=_backfill_notifications_loop, daemon=True).start()
    threading.Thread(target=_bot_health_check_loop, daemon=True).start()
    threading.Thread(target=_position_watch_loop, daemon=True).start()
    threading.Thread(target=predictor.loop, args=(_news_headlines_for_predictor, OPENAI_API_KEY),
                     daemon=True).start()

    app.run(host=host, port=5000, debug=False, threaded=True)
