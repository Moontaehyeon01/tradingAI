# -*- coding: utf-8 -*-
"""
AI 방향 예측 (참고용 - 자동매매와 연결되지 않음).

매일 UTC 00:10 에 BTC/ETH/SOL/XRP 각각 "오늘(UTC) 일봉이 오를지/내릴지"를 확률로
예측하고, 다음날 실제 결과로 채점한다. 매주 재학습하면서 어떤 신호 조합이 실제로
맞히는 데 도움이 되는지 데이터로 고르고, 최근 구간에서 기존 모델보다 확실히 나을
때만 교체한다(학습하다 오히려 나빠지는 것 방지).

모델은 L2 정규화 로지스틱 회귀(numpy만 사용) - 서버가 메모리 1GB짜리라 무거운
라이브러리를 못 올리고, 데이터도 하루 1행 x 4종목 규모라 복잡한 모델은 과최적화만
한다. 코인 하루 방향 예측은 잘해야 52~55% 수준이라는 걸 전제로, 성적은 항상
"동전 던지기(50%)"와 "항상 롱" 기준선과 나란히 보여준다.

입력 신호:
  - 차트: 1/3/7/14/30일 수익률, 7/30일 변동성, 20/50일 이평 대비, RSI, 거래대금 증감,
          14일 고저 범위 내 위치, BTC 1/7일 수익률(시장 요인)
  - 펀딩비: 직전 1일/7일 평균
  - 미국 증시: 나스닥 직전 거래일/5거래일 수익률, S&P500 직전 거래일 수익률
  - 뉴스: GPT가 헤드라인을 읽고 매긴 종목별/시장 호재-악재 점수(-1~1).
          과거 뉴스 데이터가 없으므로 실제로 90일 이상 쌓인 뒤에야 비교 후보에 들어간다.
  미결제약정(OI)은 바이낸스가 최근 30일치만 줘서 과거로 학습할 수 없어 뺐다.
"""
from __future__ import annotations

import json
import math
import threading
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "predictor_state.json"
PRED_LOG = ROOT / "predictions.jsonl"
NEWS_FILE = ROOT / "news_scores.json"

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
HISTORY_START = "2020-01-01"
PREDICT_AFTER = timedelta(minutes=10)      # 일봉 마감(00:00 UTC) 후 데이터가 다 들어올 시간
RETRAIN_EVERY = timedelta(days=7)
HOLDOUT_DAYS = 180
NEWS_MIN_DAYS = 90
PROMOTE_MARGIN = 0.002                      # 홀드아웃 로그손실이 이만큼은 좋아져야 교체

FEATURE_LABELS = {
    "r1": "1일 수익률", "r3": "3일 수익률", "r7": "7일 수익률", "r14": "14일 수익률",
    "r30": "30일 수익률", "vol7": "7일 변동성", "vol30": "30일 변동성",
    "ma20": "20일 이평 대비", "ma50": "50일 이평 대비", "rsi": "RSI(14)",
    "volratio": "거래대금 증감(7일/30일)", "range_pos": "14일 고저 범위 내 위치",
    "btc_r1": "BTC 1일 수익률", "btc_r7": "BTC 7일 수익률",
    "fund1": "펀딩비(직전 1일)", "fund7": "펀딩비(7일 평균)",
    "ndx_r1": "나스닥 직전 거래일", "ndx_r5": "나스닥 5거래일", "spx_r1": "S&P500 직전 거래일",
    "news_coin": "뉴스 점수(종목)", "news_market": "뉴스 점수(시장)",
}
PRICE = ["r1", "r3", "r7", "r14", "r30", "vol7", "vol30", "ma20", "ma50", "rsi",
         "volratio", "range_pos", "btc_r1", "btc_r7"]
FUND = ["fund1", "fund7"]
US = ["ndx_r1", "ndx_r5", "spx_r1"]
NEWS = ["news_coin", "news_market"]
DUMMIES = ["is_ETH", "is_SOL", "is_XRP"]
UNSCALED = set(NEWS) | set(DUMMIES)        # 이미 -1~1/0~1 범위라 표준화하지 않음
FEATURE_SETS = {
    "차트": PRICE,
    "차트+펀딩비": PRICE + FUND,
    "차트+펀딩비+미국증시": PRICE + FUND + US,
    "차트+펀딩비+미국증시+뉴스": PRICE + FUND + US + NEWS,
}
L2_GRID = [1.0, 10.0]

_lock = threading.Lock()
_state: dict = {}
_data_cache = {"ts": 0.0, "data": None}


# ---------------------------------------------------------------- 저장 ----
def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load_preds() -> list[dict]:
    out = []
    try:
        with open(PRED_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _save_preds(rows: list[dict]) -> None:
    tmp = PRED_LOG.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(PRED_LOG)


# --------------------------------------------------------------- 데이터 ----
def _ms(date_str: str) -> int:
    return int(datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _fetch_klines(symbol: str) -> dict:
    """date -> (close, quote_volume). 진행 중인 오늘 봉은 뺀다."""
    out, start = {}, _ms(HISTORY_START)
    now_ms = int(time.time() * 1000)
    for _ in range(10):
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": symbol, "interval": "1d", "startTime": start, "limit": 1500},
                         timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for k in rows:
            if k[6] >= now_ms:      # close_time 이 미래면 진행 중인 봉
                continue
            d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).date().isoformat()
            out[d] = (float(k[4]), float(k[7]))
        if len(rows) < 1500:
            break
        start = rows[-1][0] + 1
    return out


def _fetch_funding(symbol: str) -> dict:
    """date -> 그날 펀딩비 평균."""
    acc: dict = {}
    start = _ms(HISTORY_START)
    for _ in range(40):
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": symbol, "startTime": start, "limit": 1000}, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for f in rows:
            d = datetime.fromtimestamp(f["fundingTime"] / 1000, timezone.utc).date().isoformat()
            acc.setdefault(d, []).append(float(f["fundingRate"]))
        if len(rows) < 1000:
            break
        start = rows[-1]["fundingTime"] + 1
    return {d: sum(v) / len(v) for d, v in acc.items()}


def _fetch_index(ticker: str) -> list[tuple[str, float]]:
    """[(미국 거래일, 종가)] 오름차순."""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"range": "10y", "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = []
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), float(c)))
    return out


def _get_data() -> dict:
    if _data_cache["data"] is not None and time.time() - _data_cache["ts"] < 3 * 3600:
        return _data_cache["data"]
    data = {
        "klines": {s: _fetch_klines(s) for s in COINS},
        "funding": {s: _fetch_funding(s) for s in COINS},
        "ndx": _fetch_index("%5EIXIC"),
        "spx": _fetch_index("%5EGSPC"),
    }
    _data_cache.update({"ts": time.time(), "data": data})
    return data


# ---------------------------------------------------------------- 피처 ----
def _rsi(closes: list[float], n: int = 14) -> float:
    diffs = [closes[i] - closes[i - 1] for i in range(len(closes) - n, len(closes))]
    gain = sum(d for d in diffs if d > 0) / n
    loss = sum(-d for d in diffs if d < 0) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _prep(data: dict) -> dict:
    """날짜 정렬/배열화를 한 번만 해둔다 - 매 날짜마다 다시 정렬하면 7년치에서 너무 느리다."""
    if "_prep" in data:
        return data["_prep"]
    pre = {}
    for s in COINS:
        ds = sorted(data["klines"][s])
        pre[s] = {"dates": ds, "close": [data["klines"][s][d][0] for d in ds],
                  "qv": [data["klines"][s][d][1] for d in ds]}
    for k in ("ndx", "spx"):
        pre[k] = {"dates": [d for d, _ in data[k]], "close": [c for _, c in data[k]]}
    data["_prep"] = pre
    return pre


def _index_feats(series: dict, before_date: str):
    """before_date(크립토 예측일) 전날까지 끝난 미국 세션 기준 수익률."""
    i = bisect_left(series["dates"], before_date)   # dates[:i] < before_date
    if i < 6:
        return None
    cl = series["close"]
    return (math.log(cl[i - 1] / cl[i - 2]), math.log(cl[i - 1] / cl[i - 6]))


def _features_for(symbol: str, day: str, data: dict, news: dict) -> dict | None:
    """day(UTC 날짜) 일봉을 예측하기 위한 피처 - day 전날 마감까지의 정보만 쓴다."""
    pre = _prep(data)
    ps = pre[symbol]
    i = bisect_left(ps["dates"], day)                # ps["dates"][:i] < day
    if i < 51:
        return None
    prev = ps["dates"][i - 1]
    if (datetime.fromisoformat(day) - datetime.fromisoformat(prev)).days != 1:
        return None             # 전날 봉이 없으면(데이터 구멍) 건너뜀
    closes = ps["close"][i - 51:i]
    qv = ps["qv"][i - 30:i]
    lr = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes))]
    c = closes[-1]
    lo14, hi14 = min(closes[-14:]), max(closes[-14:])

    pb = pre["BTCUSDT"]
    bi = bisect_left(pb["dates"], day)
    if bi < 8:
        return None
    bc = pb["close"][bi - 8:bi]

    fund = data["funding"][symbol]
    f7 = [fund[d] for d in ps["dates"][i - 7:i] if d in fund]
    ndx = _index_feats(pre["ndx"], day)
    spx = _index_feats(pre["spx"], day)
    if ndx is None or spx is None:
        return None

    nd = news.get(day) or {}
    coin = symbol.replace("USDT", "")
    return {
        "r1": lr[-1], "r3": math.log(c / closes[-4]), "r7": math.log(c / closes[-8]),
        "r14": math.log(c / closes[-15]), "r30": math.log(c / closes[-31]),
        "vol7": float(np.std(lr[-7:])),
        "vol30": float(np.std(lr[-30:])),
        "ma20": c / (sum(closes[-20:]) / 20) - 1, "ma50": c / (sum(closes[-50:]) / 50) - 1,
        "rsi": (_rsi(closes) - 50) / 50,
        "volratio": math.log((sum(qv[-7:]) / 7) / (sum(qv) / 30)) if sum(qv) > 0 else 0.0,
        "range_pos": (c - lo14) / (hi14 - lo14) - 0.5 if hi14 > lo14 else 0.0,
        "btc_r1": math.log(bc[-1] / bc[-2]), "btc_r7": math.log(bc[-1] / bc[-8]),
        "fund1": fund.get(prev, 0.0) * 1000, "fund7": (sum(f7) / len(f7) * 1000) if f7 else 0.0,
        "ndx_r1": ndx[0], "ndx_r5": ndx[1], "spx_r1": spx[0],
        "news_coin": float(nd.get(coin, 0.0)), "news_market": float(nd.get("market", 0.0)),
        "is_ETH": 1.0 if coin == "ETH" else 0.0, "is_SOL": 1.0 if coin == "SOL" else 0.0,
        "is_XRP": 1.0 if coin == "XRP" else 0.0,
    }


def _label(symbol: str, day: str, data: dict):
    kl = data["klines"][symbol]
    prev = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    if day not in kl or prev not in kl:
        return None
    return 1 if kl[day][0] > kl[prev][0] else 0, math.log(kl[day][0] / kl[prev][0])


def _build_dataset(data: dict, news: dict) -> list[dict]:
    rows = []
    for s in COINS:
        for day in _prep(data)[s]["dates"]:
            f = _features_for(s, day, data, news)
            lab = _label(s, day, data)
            if f is None or lab is None:
                continue
            rows.append({"date": day, "symbol": s, "y": lab[0], "ret": lab[1], "f": f,
                         "has_news": day in news})
    return rows


# ---------------------------------------------------------------- 모델 ----
def _matrix(rows, feats, mean=None, std=None):
    X = np.array([[r["f"][k] for k in feats] for r in rows], dtype=float)
    if mean is None:
        mean = X.mean(axis=0)
        std = X.std(axis=0)
        for i, k in enumerate(feats):
            if k in UNSCALED or std[i] < 1e-12:
                mean[i], std[i] = 0.0, 1.0
    Xs = (X - mean) / std
    return np.hstack([np.ones((len(rows), 1)), Xs]), mean, std


def _fit(X, y, l2: float, iters: int = 25):
    """뉴턴법(IRLS) L2 로지스틱 회귀. 절편은 규제하지 않는다."""
    w = np.zeros(X.shape[1])
    reg = np.full(X.shape[1], l2)
    reg[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + reg * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(reg) + 1e-9 * np.eye(X.shape[1])
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def _predict(X, w):
    return 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))


def _logloss(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _evaluate_config(rows, feats, l2, cutoff):
    tr = [r for r in rows if r["date"] < cutoff]
    te = [r for r in rows if r["date"] >= cutoff]
    if len(tr) < 500 or len(te) < 50:
        return None
    Xtr, m, s = _matrix(tr, feats)
    w = _fit(Xtr, np.array([r["y"] for r in tr], float), l2)
    Xte, _, _ = _matrix(te, feats, m, s)
    yte = np.array([r["y"] for r in te], float)
    p = _predict(Xte, w)
    return {"logloss": round(_logloss(yte, p), 5),
            "acc": round(float(np.mean((p >= 0.5) == (yte == 1))), 4),
            "long_acc": round(float(np.mean(yte)), 4), "n": len(te)}


def _retrain(rows, news_days: int, current: dict | None) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HOLDOUT_DAYS)).date().isoformat()
    results = []
    for name, feats in FEATURE_SETS.items():
        if "news_coin" in feats and news_days < NEWS_MIN_DAYS:
            continue
        for l2 in L2_GRID:
            ev = _evaluate_config(rows, feats, l2, cutoff)
            if ev:
                results.append({"set": name, "l2": l2, **ev})
    if not results:
        raise RuntimeError("학습 데이터 부족")
    best = min(results, key=lambda r: (r["logloss"], -r["acc"]))
    chosen, promoted = best, True
    if current:
        cur = next((r for r in results if r["set"] == current["set"] and r["l2"] == current["l2"]), None)
        if cur and best["logloss"] > cur["logloss"] - PROMOTE_MARGIN:
            chosen, promoted = cur, False       # 확실히 낫지 않으면 기존 조합 유지(데이터만 갱신)
    feats = FEATURE_SETS[chosen["set"]]
    X, m, s = _matrix(rows, feats)
    w = _fit(X, np.array([r["y"] for r in rows], float), chosen["l2"])
    version = (current or {}).get("version", 0) + 1
    return {
        "version": version, "trained_at": datetime.now(timezone.utc).isoformat(),
        "set": chosen["set"], "l2": chosen["l2"], "features": feats,
        "weights": w.tolist(), "mean": m.tolist(), "std": s.tolist(),
        "holdout": {k: chosen[k] for k in ("logloss", "acc", "long_acc", "n")},
        "holdout_from": cutoff, "n_train": len(rows),
        "switched_set": promoted and bool(current) and (current["set"], current["l2"]) != (chosen["set"], chosen["l2"]),
        "candidates": sorted(results, key=lambda r: r["logloss"]),
    }


# ---------------------------------------------------------------- 뉴스 ----
def _score_news(headlines: list[str], api_key: str) -> dict | None:
    if not api_key or not headlines:
        return None
    prompt = (
        "아래는 최근 크립토 뉴스 헤드라인이다. 각 헤드라인이 향후 24시간 가격에 줄 영향을 "
        "종합해서 시장 전체와 BTC/ETH/SOL/XRP 각각에 대해 -1(강한 악재)~0(무관/중립)~1(강한 호재) "
        "점수를 매겨라. 관련 뉴스가 없는 종목은 0.\n\n" + "\n".join(f"- {h}" for h in headlines[:25])
    )
    schema = {
        "type": "object",
        "properties": {k: {"type": "number"} for k in ("market", "BTC", "ETH", "SOL", "XRP")},
        "required": ["market", "BTC", "ETH", "SOL", "XRP"], "additionalProperties": False,
    }
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "temperature": 0,
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_schema",
                                  "json_schema": {"name": "news_scores", "strict": True, "schema": schema}}},
        timeout=45,
    )
    r.raise_for_status()
    out = json.loads(r.json()["choices"][0]["message"]["content"])
    return {k: max(-1.0, min(1.0, float(v))) for k, v in out.items()}


# --------------------------------------------------------------- 하루 주기 ----
def _confidence(p: float) -> str:
    d = abs(p - 0.5)
    return "높음" if d >= 0.08 else "중간" if d >= 0.03 else "낮음"


def _explain(model: dict, f: dict) -> list[dict]:
    feats = model["features"]
    w = np.array(model["weights"][1:])
    z = (np.array([f[k] for k in feats]) - np.array(model["mean"])) / np.array(model["std"])
    contrib = [(feats[i], float(w[i] * z[i])) for i in range(len(feats)) if feats[i] not in DUMMIES]
    contrib.sort(key=lambda t: -abs(t[1]))
    return [{"feature": k, "label": FEATURE_LABELS.get(k, k), "effect": round(v, 3),
             "direction": "상승" if v > 0 else "하락"} for k, v in contrib[:4]]


def run_daily(get_headlines, api_key: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    state = _load_json(STATE_FILE, {})
    news = _load_json(NEWS_FILE, {})
    preds = _load_preds()
    data = _get_data()

    # 1) 채점 - 결과가 나온(일봉이 마감된) 예측만
    for p in preds:
        if p.get("graded"):
            continue
        lab = _label(p["symbol"], p["date"], data)
        if lab is None:
            continue
        up = lab[0] == 1
        p.update({"graded": True, "actual": "상승" if up else "하락",
                  "correct": (p["decision"] == "롱") == up,
                  "ret_pct": round(lab[1] * 100, 3),
                  "follow_ret_pct": round((lab[1] if p["decision"] == "롱" else -lab[1]) * 100, 3)})

    # 2) 뉴스 점수 (하루 1번)
    if today not in news:
        try:
            sc = _score_news(get_headlines(), api_key)
            if sc:
                news[today] = sc
                _save_json(NEWS_FILE, news)
        except Exception as exc:  # noqa: BLE001
            state["last_news_error"] = str(exc)[:200]

    # 3) 재학습 (주 1회, 모델이 없으면 즉시)
    model = state.get("model")
    if not model or now - datetime.fromisoformat(model["trained_at"]) >= RETRAIN_EVERY:
        rows = _build_dataset(data, news)
        model = _retrain(rows, len(news), model)
        state["model"] = model
        state.setdefault("history", []).append(
            {k: model[k] for k in ("version", "trained_at", "set", "l2", "holdout", "switched_set", "n_train")})
        state["history"] = state["history"][-30:]

    # 4) 오늘 예측
    if not any(p["date"] == today for p in preds):
        for s in COINS:
            f = _features_for(s, today, data, news)
            if f is None:
                continue
            X, _, _ = _matrix([{"f": f}], model["features"], np.array(model["mean"]), np.array(model["std"]))
            prob = float(_predict(X, np.array(model["weights"]))[0])
            preds.append({
                "date": today, "symbol": s, "prob_up": round(prob, 4),
                "decision": "롱" if prob >= 0.5 else "숏", "confidence": _confidence(prob),
                "drivers": _explain(model, f), "model_version": model["version"],
                "made_at": now.isoformat(), "graded": False,
            })

    _save_preds(preds)
    state["last_run"] = now.isoformat()
    state["last_run_date"] = today
    state.pop("last_error", None)
    _save_json(STATE_FILE, state)
    with _lock:
        _state.clear()
        _state.update(state)


def loop(get_headlines, api_key: str) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            st = _load_json(STATE_FILE, {})
            due = now - now.replace(hour=0, minute=0, second=0, microsecond=0) >= PREDICT_AFTER
            if due and st.get("last_run_date") != now.date().isoformat():
                run_daily(get_headlines, api_key, now)
        except Exception as exc:  # noqa: BLE001
            st = _load_json(STATE_FILE, {})
            st["last_error"] = f"{datetime.now(timezone.utc).isoformat()} {str(exc)[:300]}"
            _save_json(STATE_FILE, st)
            time.sleep(600)        # 실패하면 10분 뒤 재시도
        time.sleep(60)


# --------------------------------------------------------------- 화면용 ----
def _scoreboard(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    n = len(rows)
    acc = sum(1 for r in rows if r["correct"]) / n
    long_acc = sum(1 for r in rows if r["actual"] == "상승") / n
    return {"n": n, "acc": round(acc * 100, 1), "always_long": round(long_acc * 100, 1),
            "coin_flip": 50.0, "follow_ret_pct": round(sum(r["follow_ret_pct"] for r in rows), 2),
            "beats_baseline": acc > max(0.5, long_acc)}


def payload() -> dict:
    state = _load_json(STATE_FILE, {})
    preds = _load_preds()
    graded = [p for p in preds if p.get("graded")]
    cutoff30 = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    latest_date = max((p["date"] for p in preds), default=None)
    model = state.get("model") or {}
    return {
        "today": [p for p in preds if p["date"] == latest_date],
        "recent": sorted(graded, key=lambda p: (p["date"], p["symbol"]), reverse=True)[:20],
        "live": _scoreboard(graded),
        "live_30d": _scoreboard([p for p in graded if p["date"] >= cutoff30]),
        "by_coin": {s.replace("USDT", ""): _scoreboard([p for p in graded if p["symbol"] == s]) for s in COINS},
        "model": {k: model.get(k) for k in ("version", "trained_at", "set", "l2", "holdout",
                                             "holdout_from", "n_train", "switched_set")} if model else None,
        "candidates": (model.get("candidates") or [])[:8],
        "news_today": _load_json(NEWS_FILE, {}).get(latest_date) if latest_date else None,
        "last_run": state.get("last_run"), "last_error": state.get("last_error"),
    }
