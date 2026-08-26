# tradingAI — Binance 선물 자동매매 봇 & 실시간 대시보드

Freqtrade 기반으로 두 개의 독립 전략(추세추종 / 평균회귀)을 동시에 운용하는
바이낸스 선물 자동매매 시스템입니다. 백테스트 → 워크포워드 최적화 → 드라이런 →
소액 실전(5x 레버리지)까지 전체 파이프라인을 거쳤고, AWS EC2에 24/7 배포되어
Telegram 실시간 알림 + 자체 제작 웹 대시보드로 모니터링합니다.

> ⚠️ 이 저장소는 포트폴리오/학습 목적입니다. 실제 API 키, 서버 접속 정보,
> 거래 DB 등 민감 정보는 전부 제외했습니다 (`.gitignore`, `.env.example` 참고).
> 코드를 그대로 실행한다고 해서 수익이 보장되지 않으며, 실전 투입 전 반드시
> 본인 책임 하에 충분한 드라이런 검증을 거쳐야 합니다.

---

## 1. 전체 구조

```
tradingAI/
├── freqtrade/                  # 메인 실전 봇 (Freqtrade)
│   ├── strategies/
│   │   ├── MultiConfluenceStrategy.py   # 추세추종 (MACD+Stochastic+Bollinger, EMA100 추세필터)
│   │   ├── MeanReversionStrategy.py     # 평균회귀 (RSI+Bollinger+ADX, EMA100+1%여유폭 필터)
│   │   └── *StrategyV2.py, *StrategyV3.py  # 위 필터 개선 과정의 로컬 실험 버전(검증 후 본 파일에 병합)
│   ├── config_*_futures.json            # 선물(롱/숏, 5x 레버리지) 실전 설정
│   ├── walk_forward.py                  # 워크포워드 최적화 자동화 (Docker hyperopt 반복실행)
│   ├── docker-compose.yml               # 봇 2개 + 대시보드 컨테이너 오케스트레이션
│   └── dashboard/                       # 자체 제작 Flask 웹 대시보드
├── tools/                      # 교차검증용 보조 엔진 (VectorBT, Backtrader, CCXT)
├── experimental/                # 실험 트랙 (FinRL, Hummingbot, NautilusTrader 등)
├── aws/                         # EC2 배포용 cloud-init 스크립트
└── docs/                        # 기획 문서
```

## 2. 개발 과정 (진행 순서)

### Phase 1 — 환경 세팅 & 검증 파이프라인
- Freqtrade를 Docker 기반으로 세팅, `.env`로 API 키/시크릿 분리 (하드코딩 금지)
- VectorBT(대량 파라미터 스캔) → Freqtrade hyperopt(정밀 검증) → Backtrader(교차검증)
  세 엔진으로 결과 방향성을 상호 검증하는 구조로 실험 스크립트 정리

### Phase 2 — 전략 개발 & 워크포워드 최적화
- **MultiConfluenceStrategy** (추세추종): MACD + Stochastic + Bollinger Band 컨플루언스
- **MeanReversionStrategy** (평균회귀): RSI + Bollinger + ADX 국면 필터
- 자체 제작 `walk_forward.py`로 90일 학습 / 30일 검증 롤링 윈도우 최적화 자동화
  (Docker로 hyperopt 반복 실행 후 CSV 결과 축적)
- 5x 레버리지 선물 기준 워크포워드 결과 (21개 구간 평균, `freqtrade/walk_forward_results_*_5x.csv`):

  | 전략 | 검증 구간 수 | 구간당 평균 수익률 |
  |---|---|---|
  | MultiConfluenceStrategy (추세추종) | 21 | +3.90% |
  | MeanReversionStrategy (평균회귀) | 21 | +6.48% |

### Phase 3 — 고위험/고수익 확장 (선물 롱숏 + 레버리지)
- 두 전략 모두 현물 롱 전용 → **선물 롱/숏 양방향 + 설정 가능한 레버리지**로 확장
- `can_short`를 클래스 속성이 아닌 `@property`로 구현
  (`trading_mode == "futures"`일 때만 동적으로 True — freqtrade는 spot 설정에서
  `can_short=True`를 클래스 속성으로 두면 시작 시점에 하드 에러를 냄)
- 리스크 수준(레버리지)을 사용자와 함께 단계적으로 검증 후 5배로 확정

### Phase 4 — 알림 & 대시보드
- Freqtrade 내장 webhook은 `.format()` 문자열 템플릿만 지원(조건문 불가)이라,
  자체 Flask `/webhook` 릴레이 서버를 만들어 완전한 한글 커스텀 메시지로
  가공한 뒤 Telegram Bot API로 전송하는 구조로 우회 구현
- 실시간 웹 대시보드(Flask + Chart.js, 자체 디자인)
  - 봇별 잔고/손익/승률/MDD, 누적 손익 추이 차트(호버 시 수익률+수익금 동시 표시)
  - 실시간 시세, 포지션/청산 이력 테이블, 최근 알림 모달
  - HTTP Basic Auth로 대시보드 자체 접근 제어
  - 신규 진입/청산 발생 시 Web Audio API로 합성한 알림음 재생 (외부 음원 파일 없음)
  - 멀티 봇이 계좌를 공유할 때 발생하는 잔고 이중계산(다른 봇의 포지션/타 자산 dust
    혼입) 문제를 `is_bot_managed` 필터링으로 해결

### Phase 5 — AWS 배포 (24/7 무중단)
- PC를 꺼도 봇과 대시보드가 계속 동작하도록 AWS EC2(t3.micro, 서울 리전)에 배포
- cloud-init user-data 스크립트로 스왑 메모리 생성 + Docker 자동 설치
- systemd 서비스로 대시보드 자동 재시작, 보안 그룹으로 SSH는 특정 IP만 허용
- DuckDNS 무료 동적 DNS로 고정 도메인 연결

### Phase 6 — 실전 전환 (소액 실계좌)
- 드라이런 충분히 검증 후 소액 실자금으로 전환 (`dry_run: false`)
- 바이낸스 API 키는 출금 권한 비활성화 + IP 화이트리스트 적용 후 발급
- 봇 전환 시점에 드라이런 기록을 별도 DB로 분리해 실계좌 수익률이 드라이런
  데이터와 섞이지 않도록 구성

### Phase 7 — 4번째 종목(XRP) 추가 & 전략 구조 개선
- **페어 확장 실험**: XRP를 페어에 추가했더니 BTC/ETH/SOL 위주로 튜닝된 공유
  파라미터로는 XRP가 거의/전혀 거래되지 않는 걸 발견 (2년 백테스트 XRP 거래 0건)
- **페어별 파라미터 분기**: 별도 봇 인스턴스를 늘리는 대신(잔고 공유가 안 됨),
  전략 클래스 안에서 `metadata["pair"]`를 보고 XRP일 때만 별도 하이퍼옵트
  파라미터 세트를 쓰도록 분기 처리 — 한 프로세스, 한 잔고 풀을 유지하면서
  종목별로 다른 파라미터를 쓸 수 있게 함
- **구조적 취약점 발견 및 개선**: 실전 배포 직후 백테스트로 재확인하는 과정에서
  평균회귀 전략이 진짜 하락추세에서도 "횡보장"으로 오판해 눌림목을 계속 매수하다
  손절을 반복하는 문제를 발견 (8개월 구간 백테스트 -47.71%, 손절 23건이 -984 USDT로
  나머지 이익을 전부 상쇄). 장기 EMA(100) 방향 필터를 추가해 대세와 반대 방향
  진입을 차단하는 방식으로 개선 (평균회귀는 1% 여유폭 추가)
- **5.8년 워크포워드 + 완전 홀드아웃 검증**: 2020.10~2026.8 전체를 30일 단위로
  재최적화 없이 고정 파라미터로 슬라이딩 검증 + 파라미터 탐색에 전혀 안 쓰인
  2020.8~2021.8 구간을 별도 홀드아웃으로 검증해 과최적화 여부 교차 확인
  (두 전략 모두 70개 구간 중 마이너스 구간 0개, 총 519건 실거래 기준)
- **멀티 봇 계좌 공유의 또 다른 함정 발견**: 두 전략 봇이 같은 종목을 반대
  방향으로 동시에 진입하면, 바이낸스 원웨이 포지션 모드에서 주문이 서로
  넷팅되면서 각 봇의 로컬 거래 기록이 실제 계좌 상태와 어긋나는 현상을 발견
  (`/trades/{id}/reload` API로 재동기화, 안 되면 `DELETE /trades/{id}`로 정리)
- **하이퍼옵트 자체 평가와 실제 백테스트 결과가 크게 어긋나는 현상 확인** —
  하이퍼옵트가 자체 보고한 수익률(예: +16.78%, +4.43%)이 동일 파라미터로 다시
  돌린 독립 백테스트에서는 완전히 다르게(-6.73%, -30.28%) 나오는 경우가 반복
  확인됨 → 이후로는 하이퍼옵트 결과를 그대로 믿지 않고 반드시 별도
  backtesting으로 재검증하는 절차를 필수화

## 3. 기술 스택

| 영역 | 기술 |
|---|---|
| 트레이딩 엔진 | Freqtrade (Docker) |
| 보조/교차검증 | VectorBT, Backtrader, CCXT |
| 실험 트랙 | FinRL(강화학습), Hummingbot, NautilusTrader, Lumibot, TradingAgents(LLM) |
| 백엔드 | Python, Flask REST 릴레이 서버 |
| 프론트엔드 | Vanilla JS, Chart.js, Web Audio API |
| 인프라 | AWS EC2, Docker Compose, systemd, DuckDNS |
| 알림 | Telegram Bot API (커스텀 한글 포맷) |

## 4. 배운 점 / 트러블슈팅 하이라이트
- freqtrade의 `can_short`는 정적 클래스 속성이 아니라 동적 프로퍼티로 둬야
  spot/futures 설정을 한 코드베이스에서 공유할 수 있음
- 멀티 봇이 하나의 거래소 계좌를 공유할 때는 `balance` API의 `bot_owned` 필드만
  믿으면 안 되고, 포지션 단위로 `is_bot_managed` 필터링이 필요함
- 수수료를 특정 코인(BNB)으로 지불하도록 설정된 상태에서 그 코인 자체를
  거래 페어로 쓰면, 소액 오차로 인해 freqtrade가 포지션 종료 동기화를
  거부하는 알려진 엣지케이스가 있음 → 해당 페어를 화이트리스트에서 제외로 대응
- CSS `[hidden]` 속성은 브라우저 기본 스타일이라 우선순위가 낮음 — 직접
  정의한 `.class { display: flex }` 같은 규칙에 쉽게 덮어씌워지므로,
  숨김 처리가 확실히 되어야 하는 요소는 `.class[hidden] { display: none }`
  형태로 명시적 우선순위를 줘야 함
- Windows + Git Bash에서 `docker run -v host:container` 처럼 콜론으로 host/container
  경로를 같이 넘기면, MSYS 경로 자동변환이 컨테이너 쪽 경로까지 Windows 경로로
  잘못 바꿔버려서 볼륨 마운트가 조용히 실패할 수 있음 (`MSYS_NO_PATHCONV=1`로 방지,
  또는 `subprocess.run(["docker", ...])`처럼 셸을 거치지 않고 직접 실행하면 회피됨)
- freqtrade 워크포워드처럼 반복 실행되는 스크립트에서 실패 로그를 그대로
  `print()`하면, Windows 콘솔 인코딩(cp949)이 못 다루는 문자에서 스크립트 전체가
  죽어 나머지 구간이 통째로 스킵됨 — 에러 로그 출력은 항상
  `errors="replace"`로 감싸야 특정 구간 하나의 실패가 전체 실행을 막지 않음
- 하이퍼옵트가 자체적으로 보고하는 "Best result"의 수익률은 실제로 그 파라미터를
  독립 backtesting에 넣었을 때와 다르게 나올 수 있음 — 배포 전 반드시 별도
  backtesting 명령으로 재검증할 것

---

## 실행 방법 (참고용)

```bash
cp .env.example .env   # 본인 API 키/시크릿 채우기
cd freqtrade
docker compose --profile futures up -d
```

대시보드:
```bash
cd freqtrade/dashboard
pip install -r requirements.txt
python server.py
```

자세한 최초 설정 가이드는 [CLAUDE_CODE_PROMPT.md](CLAUDE_CODE_PROMPT.md) 참고.
