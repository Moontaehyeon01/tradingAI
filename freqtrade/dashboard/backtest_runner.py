# -*- coding: utf-8 -*-
"""
대시보드에서 백테스트를 실행하고 결과를 파싱하는 모듈.

설계 메모
  - freqtrade 를 Docker로 실행한다. 대시보드가 도는 호스트의 docker를 쓴다.
  - 한 번에 한 작업만 돌린다. 백테스트는 CPU·메모리를 많이 먹는데, 이 대시보드는
    실전 봇과 같은 장비에서 돌 수 있다. 동시에 여러 개를 돌리면 봇이 OOM으로
    죽을 수 있어서 큐가 아니라 아예 단일 슬롯으로 막았다.
  - VolumePairList 는 백테스트에서 동작하지 않으므로 항상 StaticPairList 로
    바꿔서 실행한다. 페어는 호출자가 명시적으로 넘긴다.
  - 거래소 키·웹훅·API서버는 전부 꺼서, 백테스트가 실계좌를 건드릴 여지를 없앤다.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

DOCKER_IMAGE = "freqtradeorg/freqtrade:stable"

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
FT_ROOT = os.path.dirname(DASHBOARD_DIR)                 # .../freqtrade
STRATEGY_DIR = os.path.join(FT_ROOT, "strategies")
USER_DATA = os.path.join(FT_ROOT, "user_data")
DATA_DIR = os.path.join(USER_DATA, "data", "binance", "futures")

BASE_CONFIG = os.path.join(FT_ROOT, "config_boxbreakout_v2_futures.json")

# 안전 한도 — 서버(t3.micro 급)에서 실전 봇을 죽이지 않기 위한 상한
MAX_PAIRS = 20
MAX_RUNTIME_SEC = 1800
HISTORY_LIMIT = 20

_lock = threading.Lock()
_current = None          # 실행 중인 job dict
_history = []            # 완료된 job dict 목록(최신 우선)


# ---------------------------------------------------------------- 유틸
def available_pairs():
    """로컬에 1시간봉 데이터가 있는 페어 목록."""
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for f in os.listdir(DATA_DIR):
        if f.endswith("-1h-futures.feather"):
            sym = f.replace("-1h-futures.feather", "")
            parts = sym.split("_")
            if len(parts) >= 3:
                out.append(f"{parts[0]}/{parts[1]}:{parts[2]}")
    return sorted(out)


def available_strategies():
    if not os.path.isdir(STRATEGY_DIR):
        return []
    return sorted(
        f[:-3] for f in os.listdir(STRATEGY_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )


def data_range(pair):
    """해당 페어 데이터의 시작/끝 날짜. pandas 가 없으면 None."""
    try:
        import pandas as pd
    except ImportError:
        return None
    fn = pair.replace("/", "_").replace(":", "_") + "-1h-futures.feather"
    p = os.path.join(DATA_DIR, fn)
    if not os.path.exists(p):
        return None
    try:
        d = pd.read_feather(p, columns=["date"])
        return {"start": str(d["date"].iloc[0])[:10], "end": str(d["date"].iloc[-1])[:10],
                "candles": len(d)}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 결과 파싱
NUM = r"(-?[\d.]+)"


def _find(pat, text, cast=float):
    m = re.search(pat, text)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except (TypeError, ValueError):
        return None


def parse_output(out):
    """freqtrade backtesting 표준출력에서 지표를 뽑는다.

    표가 ASCII '|' 가 아니라 박스드로잉 '│' 라서 [^\\d-]* 로 건너뛴다.
    \\D* 를 쓰면 음수 부호까지 먹어서 손실이 이익으로 뒤집혀 보인다.
    """
    res = {
        "profit_pct": _find(r"Total profit %[^\d\-]*" + NUM + r"%", out),
        "profit_abs": _find(r"Absolute profit[^\d\-]*" + NUM, out),
        "cagr": _find(r"CAGR %[^\d\-]*" + NUM + r"%", out),
        "max_drawdown": _find(r"Max % of account underwater[^\d\-]*" + NUM + r"%", out),
        "sharpe": _find(r"Sharpe[^\d\-]*" + NUM, out),
        "sortino": _find(r"Sortino[^\d\-]*" + NUM, out),
        "profit_factor": _find(r"Profit factor[^\d\-]*" + NUM, out),
        "trades": _find(r"Total/Daily Avg Trades[^\d\-]*(\d+)\s*/", out, int),
        "period": None,
        "exits": [],
        "pairs": [],
    }
    m = re.search(r"Backtested\s+(\S+\s\S+)\s*->\s*(\S+\s\S+)", out)
    if m:
        res["period"] = f"{m.group(1)[:10]} ~ {m.group(2)[:10]}"

    # 승률: TOTAL 행의 마지막 숫자
    m = re.search(r"│\s*TOTAL\s*│\s*\d+\s*│[^│]*│[^│]*│[^│]*│[^│]*│\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)", out)
    if m:
        res["wins"], res["draws"], res["losses"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
        res["winrate"] = float(m.group(4))

    # 청산 사유 (EXIT REASON STATS 블록만)
    blk = re.search(r"EXIT REASON STATS(.*?)(?:MIXED TAG STATS|SUMMARY METRICS|\Z)", out, re.S)
    if blk:
        for row in re.finditer(
                r"│\s*([a-z_]+)\s*│\s*(\d+)\s*│\s*" + NUM + r"\s*│\s*" + NUM
                + r"\s*│\s*" + NUM + r"\s*│[^│]*│\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)",
                blk.group(1)):
            if row.group(1) == "TOTAL":
                continue
            res["exits"].append({
                "reason": row.group(1), "count": int(row.group(2)),
                "avg_pct": float(row.group(3)), "abs": float(row.group(4)),
                "total_pct": float(row.group(5)), "winrate": float(row.group(9)),
            })

    # 페어별 (BACKTESTING REPORT 블록)
    blk = re.search(r"BACKTESTING REPORT(.*?)(?:LEFT OPEN TRADES|ENTER TAG|EXIT REASON|\Z)", out, re.S)
    if blk:
        for row in re.finditer(
                r"│\s*([A-Z0-9]+/[A-Z]+:[A-Z]+)\s*│\s*(\d+)\s*│\s*" + NUM
                + r"\s*│\s*" + NUM + r"\s*│\s*" + NUM + r"\s*│[^│]*│\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)",
                blk.group(1)):
            res["pairs"].append({
                "pair": row.group(1), "trades": int(row.group(2)),
                "avg_pct": float(row.group(3)), "abs": float(row.group(4)),
                "total_pct": float(row.group(5)), "winrate": float(row.group(9)),
            })
    return res


# ---------------------------------------------------------------- 실행
def _build_config(workdir, opts):
    with open(BASE_CONFIG, encoding="utf-8") as f:
        c = json.load(f)
    c["dry_run"] = True
    c["leverage"] = float(opts["leverage"])
    c["max_open_trades"] = int(opts["max_open_trades"])
    c["pairlists"] = [{"method": "StaticPairList"}]   # VolumePairList는 백테스트 불가
    c["exchange"]["pair_whitelist"] = opts["pairs"]
    c["exchange"]["key"] = ""
    c["exchange"]["secret"] = ""
    c["webhook"] = {"enabled": False}
    c["api_server"] = dict(c.get("api_server", {}))
    c["api_server"]["enabled"] = False
    c["api_server"]["jwt_secret_key"] = secrets.token_hex(24)
    c["db_url"] = "sqlite:///user_data/backtest_scratch.sqlite"
    p = os.path.join(workdir, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    return p


def _build_strategy_dir(workdir, opts):
    """전략 .py 를 복사하고, 화면에서 조정한 파라미터를 JSON으로 심는다."""
    d = os.path.join(workdir, "strategies")
    os.makedirs(d, exist_ok=True)
    strat = opts["strategy"]
    shutil.copy(os.path.join(STRATEGY_DIR, strat + ".py"), d)

    params = opts.get("params") or {}
    if params:
        payload = {
            "strategy_name": strat,
            "params": {
                "buy": params.get("buy", {}),
                "sell": params.get("sell", {}),
                "roi": params.get("roi", {}),
                "stoploss": params.get("stoploss", {}),
                "trailing": {"trailing_stop": False, "trailing_stop_positive": None,
                             "trailing_stop_positive_offset": 0.0,
                             "trailing_only_offset_is_reached": False},
            },
            "ft_stratparam_v": 1,
        }
        with open(os.path.join(d, strat + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    return d


def _run_job(job, opts):
    workdir = tempfile.mkdtemp(prefix="bt_")
    try:
        cfg = _build_config(workdir, opts)
        sdir = _build_strategy_dir(workdir, opts)
        cmd = [
            "docker", "run", "--rm",
            "--memory", "1g",           # 실전 봇을 밀어내지 않도록 상한
            "-v", f"{cfg}:/freqtrade/user_data/config.json",
            "-v", f"{USER_DATA}:/freqtrade/user_data",
            "-v", f"{sdir}:/freqtrade/user_data/strategies",
            DOCKER_IMAGE, "backtesting",
            "--config", "/freqtrade/user_data/config.json",
            "--strategy", opts["strategy"],
            "--timerange", opts["timerange"],
            "--cache", "none",
        ]
        job["cmd"] = " ".join(cmd[-8:])
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=MAX_RUNTIME_SEC)
        out = r.stdout or ""
        job["returncode"] = r.returncode
        if r.returncode != 0:
            job["status"] = "error"
            err = (r.stderr or "").strip().splitlines()
            job["error"] = "\n".join(err[-6:]) or "백테스트 실행 실패"
        else:
            job["status"] = "done"
            job["result"] = parse_output(out)
            if job["result"].get("trades") in (None, 0):
                job["warning"] = ("거래가 0건입니다. 기간에 데이터가 없거나 "
                                  "조건을 만족한 구간이 없습니다.")
        job["log_tail"] = "\n".join(out.splitlines()[-60:])
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = f"제한 시간({MAX_RUNTIME_SEC//60}분)을 초과했습니다. 기간이나 페어 수를 줄여보세요."
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["elapsed"] = round(time.time() - job["started_ts"], 1)
        shutil.rmtree(workdir, ignore_errors=True)
        global _current
        with _lock:
            _history.insert(0, job)
            del _history[HISTORY_LIMIT:]
            _current = None


def validate(opts):
    """입력 검증. 문제가 있으면 사람이 읽을 수 있는 메시지를 돌려준다."""
    strat = opts.get("strategy", "")
    if strat not in available_strategies():
        return f"알 수 없는 전략입니다: {strat}"
    pairs = opts.get("pairs") or []
    if not pairs:
        return "페어를 하나 이상 선택하세요."
    if len(pairs) > MAX_PAIRS:
        return f"페어는 최대 {MAX_PAIRS}개까지 가능합니다."
    have = set(available_pairs())
    missing = [p for p in pairs if p not in have]
    if missing:
        return "시세 데이터가 없는 페어입니다: " + ", ".join(missing[:5])
    if not re.fullmatch(r"\d{8}-\d{8}", opts.get("timerange", "")):
        return "기간 형식이 올바르지 않습니다 (YYYYMMDD-YYYYMMDD)."
    try:
        lev = float(opts.get("leverage", 1))
        if not 1 <= lev <= 20:
            return "레버리지는 1~20 사이여야 합니다."
    except (TypeError, ValueError):
        return "레버리지 값이 올바르지 않습니다."
    return None


def start(opts):
    """백테스트 시작. (job, error) 를 돌려준다."""
    global _current
    err = validate(opts)
    if err:
        return None, err
    with _lock:
        if _current is not None:
            return None, "이미 백테스트가 실행 중입니다. 끝난 뒤 다시 시도하세요."
        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "opts": opts,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "started_ts": time.time(),
        }
        _current = job
    threading.Thread(target=_run_job, args=(job, opts), daemon=True).start()
    return job, None


def _public(job):
    if not job:
        return None
    out = {k: v for k, v in job.items() if k != "started_ts"}
    if job["status"] == "running":
        out["elapsed"] = round(time.time() - job["started_ts"], 1)
    return out


def status(job_id=None):
    with _lock:
        if job_id:
            if _current and _current["id"] == job_id:
                return _public(_current)
            for j in _history:
                if j["id"] == job_id:
                    return _public(j)
            return None
        return {
            "current": _public(_current),
            "history": [_public(j) for j in _history],
        }
