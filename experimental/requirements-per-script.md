# experimental/ 설치 가이드

각 스크립트는 서로 다른 프레임워크를 쓰기 때문에 하나의 requirements.txt로
묶지 않고 개별적으로 설치합니다. 아래는 각 파일별 설치 명령과 실행 전 필요한
환경변수입니다. **지금 자동으로 설치/실행하지 않습니다** — 필요할 때 직접 실행하세요.

| 파일 | 설치 | 필요 환경변수 | 비고 |
|---|---|---|---|
| `nautilus_ma_rsi_strategy.py` | `pip install nautilus_trader` | - | 백테스트 엔진, 버전별 API 차이 큼 |
| `lumibot_ma_rsi_strategy.py` | `pip install lumibot` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` (실전/드라이런 연동 시) | 백테스트는 키 없이도 실행 가능 |
| `finrl_train_agent.py` | `pip install finrl stable-baselines3 ccxt pandas numpy gymnasium` | - | 강화학습, 학습에 시간 소요. **직접 실행 지시 전까지 실행 금지** |
| `tradingagents_signal_research.py` | `git clone https://github.com/TauricResearch/TradingAgents.git` 후 해당 폴더에서 `pip install -r requirements.txt` | `OPENAI_API_KEY` | LLM API 호출당 비용 발생. **직접 실행 지시 전까지 실행 금지** |
| `hummingbot_simple_pmm_script.py` | Hummingbot 자체 설치 필요 (Docker 권장, https://hummingbot.org) | - | pip 단독 설치 아님, hummingbot/scripts/ 에 복사 후 사용 |

실행 예:
```bash
pip install nautilus_trader
python experimental/nautilus_ma_rsi_strategy.py
```
