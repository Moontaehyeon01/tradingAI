#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
walk_forward.py
----------------
Freqtrade에는 walk-forward 최적화가 기본 내장되어 있지 않아서,
전체 기간을 여러 구간(window)으로 나눠 다음을 자동 반복하는 스크립트입니다:

  1. Train 구간에서 hyperopt로 파라미터 최적화
  2. 방금 찾은 파라미터를 그대로 다음 Test(out-of-sample) 구간에서 backtesting
  3. Train 구간을 한 칸씩 뒤로 밀면서(rolling window) 반복
  4. 각 구간별 결과를 CSV로 저장 → 특정 구간에서만 성과가 좋은 "과최적화" 여부 판별

사용법 (freqtrade/ 디렉터리에서, Docker Desktop이 실행 중이어야 함):
    python walk_forward.py \\
        --config config_binance.json \\
        --strategy MultiConfluenceStrategy \\
        --timerange-start 20230101 \\
        --timerange-end 20241231 \\
        --train-days 90 \\
        --test-days 30 \\
        --epochs 100

결과: walk_forward_results.csv 파일에 구간별 backtesting 성과 저장
      (구간마다 수익률 편차가 크면 과최적화 의심 신호)

참고: 로컬에 freqtrade CLI를 pip로 설치하지 않았다면 --docker (기본값)로
      docker-compose.yml과 동일한 이미지(freqtradeorg/freqtrade:stable)를
      매번 docker run으로 띄워서 실행합니다. 로컬에 freqtrade를 pip로 설치했다면
      --docker false 로 로컬 CLI를 직접 호출할 수 있습니다.
"""

import argparse
import csv
import json
import os
import subprocess
import re
import sys
from datetime import datetime, timedelta

DOCKER_IMAGE = "freqtradeorg/freqtrade:stable"


def parse_args():
    parser = argparse.ArgumentParser(description="Freqtrade Walk-Forward Optimization Runner")
    parser.add_argument("--config", required=True, help="freqtrade config.json 경로 (freqtrade/ 폴더 기준 상대경로)")
    parser.add_argument("--strategy", required=True, help="전략 클래스명")
    parser.add_argument("--timerange-start", required=True, help="예: 20230101")
    parser.add_argument("--timerange-end", required=True, help="예: 20241231")
    parser.add_argument("--train-days", type=int, default=90, help="학습 구간 일수")
    parser.add_argument("--test-days", type=int, default=30, help="검증(out-of-sample) 구간 일수")
    parser.add_argument("--epochs", type=int, default=100, help="hyperopt epoch 수")
    parser.add_argument(
        "--hyperopt-loss",
        default="SharpeHyperOptLoss",
        help="hyperopt 손실함수 (SharpeHyperOptLoss, SortinoHyperOptLoss 등)",
    )
    parser.add_argument("--output", default="walk_forward_results.csv", help="결과 저장 파일명")
    parser.add_argument(
        "--docker",
        type=lambda s: s.lower() != "false",
        default=True,
        help="true(기본값): docker run으로 freqtrade 실행 / false: 로컬 pip 설치된 freqtrade CLI 사용",
    )
    return parser.parse_args()


def build_cmd(args, freqtrade_args):
    """--docker 여부에 따라 freqtrade를 docker run으로 감싸거나 로컬 CLI를 그대로 호출"""
    if not args.docker:
        return ["freqtrade"] + freqtrade_args

    root = os.getcwd()
    # 주의: /freqtrade/user_data (부모)를 먼저 마운트하고, 그 위에
    # /freqtrade/user_data/strategies (자식)를 나중에 마운트해야 함.
    # 순서가 반대이면 나중에 마운트되는 부모 경로가 먼저 마운트된 자식 경로를
    # 통째로 덮어써서 컨테이너 안에서 전략 파일을 아예 못 찾게 됨.
    return [
        "docker", "run", "--rm",
        "-v", f"{root}/{args.config}:/freqtrade/user_data/config.json",
        "-v", f"{root}/user_data:/freqtrade/user_data",
        "-v", f"{root}/strategies:/freqtrade/user_data/strategies",
        DOCKER_IMAGE,
    ] + freqtrade_args


def daterange_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def safe_print(text: str):
    """Windows 콘솔(cp949 등)이 못 다루는 문자가 섞인 로그를 출력할 때
    UnicodeEncodeError로 스크립트 전체가 죽는 걸 방지 (한 구간 실패는
    그 구간만 건너뛰고 나머지 윈도우는 계속 진행되어야 함)."""
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def run_hyperopt(args, train_start, train_end):
    """Train 구간에서 hyperopt 실행 후 best 파라미터를 반환"""
    timerange = f"{daterange_str(train_start)}-{daterange_str(train_end)}"
    config_path = "/freqtrade/user_data/config.json" if args.docker else args.config
    cmd = build_cmd(args, [
        "hyperopt",
        "--config", config_path,
        "--strategy", args.strategy,
        "--hyperopt-loss", args.hyperopt_loss,
        "--timerange", timerange,
        "--epochs", str(args.epochs),
        "--print-json",
    ])
    print(f"\n[HYPEROPT] Train 구간: {timerange}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if result.returncode != 0:
        print("[경고] hyperopt 실행 실패:")
        safe_print(result.stderr[-2000:])
        return None

    # 마지막에 출력되는 JSON 형태의 best result 파싱 시도
    match = re.findall(r"\{.*\}", result.stdout)
    if match:
        try:
            return json.loads(match[-1])
        except json.JSONDecodeError:
            print("[경고] hyperopt 결과 JSON 파싱 실패 - 로그를 직접 확인하세요.")
            return None
    return None


def run_backtest(args, test_start, test_end):
    """Test(out-of-sample) 구간에서 backtesting 실행 후 요약 결과 반환"""
    timerange = f"{daterange_str(test_start)}-{daterange_str(test_end)}"
    config_path = "/freqtrade/user_data/config.json" if args.docker else args.config
    cmd = build_cmd(args, [
        "backtesting",
        "--config", config_path,
        "--strategy", args.strategy,
        "--timerange", timerange,
    ])
    print(f"[BACKTEST] Test 구간: {timerange}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    if result.returncode != 0:
        print("[경고] backtesting 실행 실패:")
        safe_print(result.stderr[-2000:])
        return {"timerange": timerange, "total_profit_pct": None, "raw_log": result.stderr[-500:]}

    # 콘솔 출력(Rich 테이블, │ 구분자 포함)에서 "Total profit %" 행의 값을 파싱
    # -> 정확한 파싱이 안 되면 결과 폴더의 .json 리포트를 직접 열어 확인 권장
    profit_match = re.search(r"Total profit %\D*([-\d.]+)%", result.stdout)
    total_profit_pct = float(profit_match.group(1)) if profit_match else None

    return {"timerange": timerange, "total_profit_pct": total_profit_pct}


def main():
    args = parse_args()

    start = datetime.strptime(args.timerange_start, "%Y%m%d")
    end = datetime.strptime(args.timerange_end, "%Y%m%d")

    train_delta = timedelta(days=args.train_days)
    test_delta = timedelta(days=args.test_days)

    rows = []
    window_start = start

    while True:
        train_start = window_start
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta

        if test_end > end:
            break

        best_params = run_hyperopt(args, train_start, train_end)
        backtest_result = run_backtest(args, test_start, test_end)

        rows.append(
            {
                "train_range": f"{daterange_str(train_start)}-{daterange_str(train_end)}",
                "test_range": backtest_result["timerange"],
                "test_total_profit_pct": backtest_result.get("total_profit_pct"),
                "best_params": json.dumps(best_params, ensure_ascii=False) if best_params else "",
            }
        )

        # 다음 윈도우로 이동 (test 구간만큼 rolling)
        window_start = window_start + test_delta

    # 결과 저장
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["train_range", "test_range", "test_total_profit_pct", "best_params"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n완료. 결과 저장: {args.output}")
    print("구간별 test_total_profit_pct 편차가 크면 과최적화(overfitting) 가능성이 높습니다.")
    print("각 구간의 수익률이 평균적으로 양수이고 편차가 작을수록 신뢰할 수 있는 전략입니다.")


if __name__ == "__main__":
    main()
