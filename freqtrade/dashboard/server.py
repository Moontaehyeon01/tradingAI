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
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, Response, send_from_directory
from dotenv import load_dotenv

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


BOTS = [
    # max_hold_h / box_max_width 는 전략 파라미터라 freqtrade API로는 안 나온다.
    # 화면에 "시간청산까지 남은 시간", "박스 조건 충족 여부"를 표시하려고 여기 적어둔다.
    # 전략 파라미터를 바꾸면 이 값도 같이 맞춰야 한다.
    {"id": "boxbreakoutv2", "name": "BoxBreakoutV2 (박스돌파 V2)",
     "url": "http://127.0.0.1:8086", "leverage": 3,
     "max_hold_h": 48, "box_max_width": 0.04},
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


def record_notification(entry: dict):
    notifications.appendleft(entry)
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


def exit_targets(trade: dict, bot: dict, config: dict) -> dict:
    """
    포지션이 '어디서 / 언제' 끝나는지를 계산한다.

    익절가: minimal_roi 는 '레버리지 적용 후 계좌 수익률' 단위라서 가격으로 바꾸려면
            레버리지로 나눠야 한다. ROI 35% + 레버리지 3배 -> 가격 11.7% 이동.
    시간청산: 전략의 custom_exit(max_hold_candles)이 담당하는데 freqtrade API로는
            노출되지 않아 BOTS 설정의 max_hold_h 를 쓴다.
    """
    out = {"take_profit_abs": None, "hold_remaining_h": None, "hold_total_h": None}

    lev = trade.get("leverage") or bot.get("leverage") or 1
    roi = config.get("minimal_roi") or {}
    roi0 = roi.get("0")
    open_rate = trade.get("open_rate")
    if roi0 and open_rate and lev:
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
        except (ValueError, TypeError):
            pass
    return out


# 지표(박스 상태) 캐시. 대시보드는 4초마다 갱신되는데 박스는 1시간봉이라
# 매번 봇에 물어보면 불필요한 부하만 생긴다.
_signal_cache = {"ts": 0.0, "data": None}
SIGNAL_TTL = 60


def fetch_signals():
    """감시 중인 페어별 박스 상태. '지금 왜 진입을 안 하는지'를 화면에서 답하기 위한 것."""
    now = time.time()
    if _signal_cache["data"] is not None and now - _signal_cache["ts"] < SIGNAL_TTL:
        return _signal_cache["data"]

    out = []
    for bot in BOTS:
        try:
            wl = call_bot(bot["url"], "/api/v1/whitelist").get("whitelist", [])
        except Exception:  # noqa: BLE001
            continue
        limit = bot.get("box_max_width", 0.04)
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


def fetch_bot_summary(bot: dict) -> dict:
    out = {"id": bot["id"], "name": bot["name"], "leverage": bot.get("leverage", 1), "connected": False}
    try:
        balance = call_bot(bot["url"], "/api/v1/balance")
        profit = call_bot(bot["url"], "/api/v1/profit")
        open_trades = call_bot(bot["url"], "/api/v1/status")
        recent = call_bot(bot["url"], "/api/v1/trades", {"limit": 10})
        daily = call_bot(bot["url"], "/api/v1/daily", {"timescale": 14})
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
                "profit_all_abs": profit.get("profit_all_coin", 0),
                "profit_all_pct": profit.get("profit_all_percent", 0),
                "trade_count": profit.get("trade_count", 0),
                "winrate": profit.get("winrate", 0),
                "max_drawdown": profit.get("max_drawdown", 0),
                "open_trades": [
                    {
                        "pair": t["pair"],
                        "is_short": t["is_short"],
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
                        "close_date": t.get("close_date"),
                        "exit_reason": t.get("exit_reason"),
                    }
                    for t in recent.get("trades", [])
                    if not t.get("is_open")
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


@app.get("/api/summary")
def api_summary():
    bots = [fetch_bot_summary(b) for b in BOTS]
    connected = [b for b in bots if b.get("connected")]

    # 총자산: 모든 봇이 같은 바이낸스 계좌를 공유하므로 합산하면 중복 계산이 된다.
    # 계좌 전체 잔고를 한 번만 취한다(봇마다 같은 값을 보고함).
    total_equity = max((b.get("account_total", 0) for b in connected), default=0)

    # 손익은 봇별로 각자 관리하는 거래에서 나오므로 합산이 맞다.
    total_starting = sum(b.get("starting_capital", 0) for b in connected)
    total_profit_abs = sum(b.get("profit_all_abs", 0) for b in connected)

    # 봇이 관리하지 않는 포지션(직접 잡은 것)을 페어 기준으로 중복 제거
    unmanaged = {}
    for b in connected:
        for p in b.get("unmanaged_positions", []):
            unmanaged[p["pair"]] = p

    return jsonify(
        {
            "bots": bots,
            "combined": {
                "total_equity": total_equity,
                "total_starting": total_starting,
                "total_profit_abs": total_profit_abs,
                "total_profit_pct": (total_profit_abs / total_starting * 100) if total_starting else 0,
                "unmanaged_positions": list(unmanaged.values()),
            },
            "server_time": int(time.time()),
        }
    )


@app.get("/api/tickers")
def api_tickers():
    try:
        # 봇이 실제로 거래하는 시세와 맞춰서 스팟이 아니라 선물(USDT-M) 가격을 보여줌.
        # 선물 24hr 티커 API는 스팟과 달리 "symbols" 배열 파라미터를 지원하지 않아서
        # 전체를 받아온 뒤 여기서 원하는 심볼만 골라냄.
        resp = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=4)
        resp.raise_for_status()
        data = resp.json()
        wanted = set(TICKER_SYMBOLS)
        return jsonify(
            [
                {
                    "symbol": t["symbol"],
                    "price": float(t["lastPrice"]),
                    "change_pct": float(t["priceChangePercent"]),
                }
                for t in data
                if t["symbol"] in wanted
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


EXIT_REASON_KO = {
    "roi": "목표 수익 도달",
    "stop_loss": "손절",
    "trailing_stop_loss": "트레일링 스탑",
    "exit_signal": "전략 청산 신호",
    "force_exit": "수동 청산",
    "emergency_exit": "긴급 청산",
    "liquidation": "강제 청산(청산가 도달)",
    "partial_exit": "부분 청산",
}

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
    bot_name = data.get("bot_name", "봇")
    pair = data.get("pair", "?")
    direction = data.get("direction", "")  # "Long" or "Short"
    is_short = direction == "Short"

    now_iso = datetime.now(timezone.utc).isoformat()

    if event == "entry_fill":
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
                "time": now_iso,
                "event": "entry_fill",
                "bot_name": bot_name,
                "pair": pair,
                "side_ko": side_ko,
                "is_short": is_short,
            }
        )

    elif event == "exit_fill":
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
        exit_reason_ko = EXIT_REASON_KO.get(exit_reason_raw, exit_reason_raw)
        text = (
            f"[{bot_name}] [선물 청산]\n"
            f"종목: {pair}\n"
            f"방향: {side_ko}\n"
            f"상태: 청산 완료\n"
            f"실현 수익률: {profit_ratio_pct:.4f}%\n"
            f"실현 손익: {profit_amount:.4f} {stake_currency}\n"
            f"사유: {exit_reason_ko}\n"
            f"====================="
        )
        send_telegram(text)
        record_notification(
            {
                "time": now_iso,
                "event": "exit_fill",
                "bot_name": bot_name,
                "pair": pair,
                "side_ko": side_ko,
                "is_short": is_short,
                "profit_ratio_pct": profit_ratio_pct,
                "profit_amount": profit_amount,
                "stake_currency": stake_currency,
                "exit_reason_ko": exit_reason_ko,
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
    return jsonify(list(notifications))



@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    # 로컬 개발 시엔 기본값(127.0.0.1)만 노출됨. 서버에 배포해서 외부 접속을
    # 받아야 할 때만 DASHBOARD_HOST=0.0.0.0 을 명시적으로 지정해서 실행할 것
    # (보안그룹 등 방화벽에서 이미 접근을 제한하고 있어야 안전함)
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=5000, debug=False)
