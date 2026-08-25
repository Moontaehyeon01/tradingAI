# 클로드 코드에게 전달할 프롬프트

아래 내용을 그대로 복사해서 Claude Code에 붙여넣으세요.
이 zip 안의 모든 파일이 첨부/참조 코드입니다. Claude Code가 프로젝트 폴더를
열람할 수 있는 상태에서 시작하세요 (zip 압축 해제 후 그 폴더에서 실행).

---

## 프롬프트 시작

나는 바이낸스(Binance) 현물 기준 가상화폐 자동매매 봇 프로젝트를 진행 중이야.
이 폴더(binance-trading-bot/) 안에 이미 작성된 참고 코드가 다 들어있어.
이 구조와 코드를 그대로 기준으로 삼아서, 실제로 설치·실행 가능한 상태로
프로젝트를 세팅해줘. 아래 순서와 역할을 지켜서 진행해줘.

### 핵심 원칙
- 실전 매매를 담당하는 메인 봇은 `freqtrade/` 폴더의 Freqtrade 기반 전략이야.
- 나머지(`tools/`, `experimental/`)는 검증·보조·실험용 도구고, 동시에 실전 운용하는 게 아니야.
- 백테스트 → hyperopt(walk-forward 검증) → dry-run → 소액 실전 순서를 반드시 지켜.
- 손절 로직 없는 전략은 절대 실전에 투입하지 마.

### 폴더 구조 및 역할

```
binance-trading-bot/
├── docs/
│   └── 트레이딩봇_개발기획서.md      # 전체 로드맵 (Phase 0~5)
├── freqtrade/                       # 메인 실전 봇 (Freqtrade 프레임워크)
│   ├── config_binance.json          # 바이낸스 dry-run 설정
│   ├── strategies/
│   │   ├── MaRsiAdxStrategy.py      # 1차: MA크로스+RSI+ADX 국면필터+ATR 손절/사이징
│   │   ├── MultiConfluenceStrategy.py  # 2차: MACD+Stochastic+볼린저밴드 컨플루언스 (업그레이드판, 실전 메인 후보)
│   │   └── MeanReversionStrategy.py    # 3차: 횡보장용 평균회귀 전략 (포트폴리오 2번째 전략)
│   └── walk_forward.py              # 워크포워드 최적화 자동화 스크립트 (hyperopt 반복실행+CSV결과저장)
├── tools/                           # 검증/보조 도구
│   ├── ccxt_binance_toolkit.py      # CCXT 직접 활용 (잔고/시세/OHLCV/주문 유틸)
│   ├── vectorbt_param_scan.py       # VectorBT 대량 파라미터 빠른 스캔
│   └── backtrader_strategy.py       # Backtrader 독립 백테스트 (교차검증용)
└── experimental/                    # 실험용 (실전 미채택, 각자 따로 테스트)
    ├── hummingbot_simple_pmm_script.py   # Hummingbot 마켓메이킹 (다른 목적의 봇)
    ├── nautilus_ma_rsi_strategy.py       # NautilusTrader 고성능 엔진 버전
    ├── lumibot_ma_rsi_strategy.py        # Lumibot 백테스트~배포 통합 버전
    ├── finrl_train_agent.py              # FinRL 강화학습 에이전트 학습
    └── tradingagents_signal_research.py  # TradingAgents LLM 멀티에이전트 정성분석 보조
```

### 진행 순서

1. **환경 세팅**
   - Freqtrade를 Docker로 설치 (`freqtrade/` 기준)
   - `config_binance.json`에 바이낸스 API 키 입력할 자리 확인, `dry_run: true` 유지
   - 나머지 프레임워크(VectorBT, Backtrader, CCXT)는 pip로 설치, requirements 정리해줘

2. **데이터 확보**
   - `freqtrade download-data`로 BTC/USDT, ETH/USDT, BNB/USDT, SOL/USDT 1시간봉 과거 데이터 다운로드

3. **백테스트 검증**
   - `MultiConfluenceStrategy`를 우선 검증 대상으로 백테스트 실행
   - `tools/backtrader_strategy.py`와 `tools/vectorbt_param_scan.py`로 교차검증
   - 세 엔진(Freqtrade/Backtrader/VectorBT) 결과가 방향성이 비슷한지 확인

4. **파라미터 최적화**
   - `tools/vectorbt_param_scan.py`로 먼저 넓은 범위 빠르게 스캔해서 유망 구간 좁히기
   - 좁힌 범위를 `freqtrade/walk_forward.py`로 정밀 검증 (구간별 수익률 편차 확인 → 과최적화 여부 판단)

5. **드라이런**
   - 검증된 전략으로 `dry_run: true` 상태에서 최소 2~4주 실시간 시뮬레이션
   - 텔레그램 알림 연동 (선택)

6. **포트폴리오 구성 (선택, 여유될 때)**
   - `MultiConfluenceStrategy`(추세추종) + `MeanReversionStrategy`(평균회귀)를 각각 별도 봇 인스턴스로 동시 운용할 수 있게 config 분리 (`config_trend.json`, `config_meanreversion.json` 등으로)

7. **실험용 도구는 별도 트랙으로**
   - `experimental/` 폴더의 코드들은 각각 독립적으로 설치·테스트 가능하게만 세팅해줘 (지금 당장 실전 연결 X)
   - 특히 `finrl_train_agent.py`(강화학습)와 `tradingagents_signal_research.py`(LLM 에이전트, API 비용 발생)는 실행 전 필요한 패키지/키 설정을 안내만 해주고, 내가 명시적으로 실행하라고 할 때까지 자동 실행하지 마

### 요청사항
- 각 파일을 그대로 실행 가능한 상태로 만들어줘 (의존성 설치, 폴더 경로 정리, 필요하면 requirements.txt / .env.example 파일도 만들어줘)
- API 키, 시크릿 등은 절대 코드에 하드코딩하지 말고 환경변수(.env)로 분리해줘
- 진행하면서 각 단계마다 뭘 했는지 요약해서 알려줘

## 프롬프트 끝
