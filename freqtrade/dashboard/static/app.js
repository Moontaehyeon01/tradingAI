const REFRESH_MS = 4000;

const COIN_COLORS = {
  BTC: "#f59e0b",
  ETH: "#8b5cf6",
  BNB: "#f0b90b",
  SOL: "#14f195",
};

let equityChart = null;
let lastSummary = null;
// 기간(14/30/60일)별로 마지막으로 받은 요약. 기간 버튼을 누르면 서버 응답을 기다리지
// 않고 이걸로 그래프를 바로 바꾼 뒤, 새 데이터가 오면 다시 그린다(2026-10-17 - 버튼을
// 눌러도 그래프가 한참 뒤에 바뀌던 문제).
const summaryByDays = {};
// 폴링(4초)과 버튼 클릭으로 요청이 겹칠 때, 늦게 도착한 옛 응답이 새 화면을 덮지 않게
let refreshSeq = 0;
let appliedSeq = 0;
// 봇 두 개를 좌우로 나눠 보여주면 표가 좁아져서 탭으로 하나씩 전체 폭으로
// 보여준다. 기본값은 지금 주력으로 굴리는 RSI 봇(2026-10-17 Momentum 에서 변경).
let selectedBotId = "xsectrsi";
let lastSeenAlertTime = null; // 새로 도착한 알림만 구분해서 소리 재생하기 위한 기준점
let audioCtx = null;
// 누적손익추이 그래프 기간. 서버가 7/14/30/60 만 허용하므로(api_summary 참고)
// 여기서도 이 세 값만 쓴다. refresh() 가 이 값을 그대로 쿼리스트링에 싣는다.
let equityDays = 14;

// 브라우저는 사용자가 페이지를 한 번이라도 클릭/터치하기 전엔 소리 재생을 막음(자동재생 정책)
// -> 첫 클릭 때 AudioContext를 만들어두고 이후 알림음은 그걸 재사용
document.addEventListener(
  "click",
  () => {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
  },
  { once: true }
);

// 8비트 게임풍 "코인 획득음" — 사각파 두 음을 빠르게 이어붙임
function playCoinTone(startTime, freqStart, freqSwitchAt, freqEnd, duration = 0.28, gainPeak = 0.06) {
  if (!audioCtx) return;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = "square";
  gain.gain.setValueAtTime(gainPeak, startTime);
  gain.gain.exponentialRampToValueAtTime(0.001, startTime + duration);
  osc.connect(gain).connect(audioCtx.destination);
  osc.frequency.setValueAtTime(freqStart, startTime);
  osc.frequency.setValueAtTime(freqEnd, startTime + freqSwitchAt);
  osc.start(startTime);
  osc.stop(startTime + duration);
}

function playEntrySound() {
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  playCoinTone(t, 988, 0.08, 1319); // 진입: 코인 1개 (B5 -> E6)
}

function playExitSound(isProfit) {
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  if (isProfit) {
    // 청산(이익): 코인 2개 연속 획득하는 느낌, 두번째가 살짝 더 높음
    playCoinTone(t, 988, 0.07, 1319, 0.22, 0.055);
    playCoinTone(t + 0.14, 1175, 0.07, 1568, 0.24, 0.055);
  } else {
    // 청산(손실): 코인 소리를 거꾸로 — 높은 음에서 낮은 음으로 떨어짐
    playCoinTone(t, 1319, 0.09, 830, 0.3, 0.06);
  }
}

function fmtUsd(n, digits = 2) {
  // null/undefined 판정은 Number() 변환 전에 해야 한다 - Number(null) === 0 이라
  // 변환 후에 비교하면 "값 없음"이 "0" 으로 둔갑한다(수동 청산에서 방향을 못 찾아
  // pct 가 null 로 오는 경우 실제로 걸렸던 문제).
  if (n === null || n === undefined) return "–";
  // 서버에서 문자열로 오는 값도 있어서(webhook 페이로드 등) 항상 숫자로 변환 후 처리
  n = Number(n);
  if (Number.isNaN(n)) return "–";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}`;
}

function fmtPct(n, digits = 2) {
  if (n === null || n === undefined) return "–";
  n = Number(n);
  if (Number.isNaN(n)) return "–";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(digits)}%`;
}

// freqtrade 선물 페어는 "BTC/USDT:USDT"처럼 온다(정산통화 USDT + 결제통화
// USDT가 겹쳐 보임). 어차피 이 대시보드는 전부 USDT-M이라 결제통화 표시가
// 의미 없으니 기초자산 심볼만 남긴다.
function fmtPair(pair) {
  return pair ? pair.split("/")[0] : pair;
}

function pnlClass(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "neutral";
  return n > 0 ? "pos" : n < 0 ? "neg" : "neutral";
}

function chipClass(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "neutral-chip";
  return n > 0 ? "pos" : n < 0 ? "neg" : "neutral-chip";
}

function timeAgo(iso) {
  if (!iso) return "–";
  const d = new Date(iso.replace(" ", "T") + "Z");
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return `${diffSec}초 전`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}분 전`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}시간 전`;
  return `${Math.floor(diffSec / 86400)}일 전`;
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// innerHTML을 매 polling마다 무조건 다시 쓰면 내용이 그대로여도 브라우저가
// 매번 다시 그려서 화면이 깜빡이고, 사용자 눈엔 "자꾸 새로고침되는" 것처럼 보임.
// 실제로 문자열이 달라졌을 때만 DOM을 건드리도록 캐시해서 비교.
const _htmlCache = new WeakMap();
function setHTMLIfChanged(el, html) {
  if (!el) return;
  if (_htmlCache.get(el) === html) return;
  _htmlCache.set(el, html);
  el.innerHTML = html;
}

/* ---------------- Market table ---------------- */

function renderTickers(tickers) {
  const body = document.getElementById("tickerBody");
  if (tickers.error) {
    setHTMLIfChanged(body, `<tr><td colspan="3" class="empty-row">시세 조회 실패</td></tr>`);
    return;
  }
  const html = tickers
    .map((t) => {
      const symbol = t.symbol.replace("USDT", "");
      const color = COIN_COLORS[symbol] || "#7f9fb3";
      const cls = pnlClass(t.change_pct);
      return `
        <tr class="chart-row" data-chart-base="${symbol}" title="누르면 오른쪽에 ${symbol} 차트">
          <td>
            <div class="coin-cell">
              <div class="coin-badge" style="background:${color}">${symbol.slice(0, 1)}</div>
              ${symbol}/USDT
            </div>
          </td>
          <td>$${t.price.toLocaleString(undefined, { maximumFractionDigits: t.price < 10 ? 4 : 2 })}</td>
          <td class="${cls}">${fmtPct(t.change_pct)}</td>
        </tr>`;
    })
    .join("");
  setHTMLIfChanged(body, html);
}

/* ---------------- Dry-run / Live 모드 표시 ---------------- */
// 봇이 실제로 보고하는 dry_run 값을 그대로 반영 -> 실전 전환/롤백해도 코드 수정 없이 자동 반영됨

function renderMode(summary) {
  const connectedBots = summary.bots.filter((b) => b.connected);
  const isLive = connectedBots.length > 0 && connectedBots.every((b) => b.dry_run === false);

  const modePill = document.getElementById("modePill");
  const modeText = document.getElementById("modeText");
  const footerModeText = document.getElementById("footerModeText");

  if (isLive) {
    modePill.classList.add("live");
    modeText.textContent = "LIVE";
    footerModeText.textContent = "⚠ 실전 거래 중 · 실제 자금이 체결됩니다";
  } else {
    modePill.classList.remove("live");
    modeText.textContent = "DRY-RUN";
    footerModeText.textContent = "DRY-RUN 시뮬레이션 · 실제 자금 미체결";
  }
}

/* ---------------- Stat cards ---------------- */

function renderStats(summary) {
  const connectedBots = summary.bots.filter((b) => b.connected);

  document.getElementById("statEquity").textContent =
    `$${summary.combined.total_equity.toFixed(2)}`;
  const eqChip = document.getElementById("statEquityChip");
  eqChip.textContent = fmtPct(summary.combined.total_profit_pct);
  eqChip.className = `stat-chip ${chipClass(summary.combined.total_profit_pct)}`;

  // 봇이 관리하지 않는 포지션(직접 잡은 것)이 있으면 총자산 아래에 표시.
  // 계좌 잔고 대부분이 거기 묶여 있을 수 있어서, 안 보여주면 "봇 잔고가 왜 이렇게
  // 적지?" 하고 헷갈리게 된다.
  const unm = summary.combined.unmanaged_positions || [];
  const unmEl = document.getElementById("statUnmanaged");
  if (unmEl) {
    if (unm.length) {
      const txt = unm
        .map((p) => `${p.pair.split("/")[0]} ${p.side === "short" ? "숏" : "롱"} $${(p.est_stake || 0).toFixed(0)}`)
        .join(", ");
      unmEl.textContent = `봇 미관리 포지션: ${txt}`;
      unmEl.hidden = false;
    } else {
      unmEl.hidden = true;
    }
  }

  document.getElementById("statPnl").textContent =
    `$${fmtUsd(summary.combined.total_profit_abs)}`;
  const pnlChip = document.getElementById("statPnlChip");
  pnlChip.textContent = fmtPct(summary.combined.total_profit_pct);
  pnlChip.className = `stat-chip ${chipClass(summary.combined.total_profit_pct)}`;

  // 총손익 = 봇 손익 + 수동 매매 손익. 아래 밝혀서 뭐가 얼마나 기여했는지 보여준다.
  // manual_profit_abs 가 null 이면 조회 실패(계좌 접근 오류 등)라 값이 아니라
  // 그 사실 자체를 알려야 한다 - fmtUsd(null) 이 "–" 를 주는 걸 그대로 쓴다.
  //
  // 수동 손익 집계 기간(8월~)을 같이 보여준다 - 예전엔 롤링 1년을 썼는데, 그
  // 범위에 이 프로젝트와 무관한 훨씬 오래된 손실이 섞여 들어가 "요즘은 번
  // 것밖에 없는데 왜 마이너스냐"는 착시를 만들었다(server.py MANUAL_PNL_START
  // 참고). 기간을 못 박아 고쳤으니, 화면에도 그 기간을 명시해야 다음에 또
  // 이 혼란이 반복되지 않는다.
  const pnlNote = document.getElementById("statPnlBreakdown");
  if (pnlNote) {
    const botTxt = `봇 $${fmtUsd(summary.combined.bot_profit_abs)}`;
    const manualTxt =
      summary.combined.manual_profit_abs === null
        ? "수동 조회 실패"
        : `수동 $${fmtUsd(summary.combined.manual_profit_abs)}`;
    pnlNote.textContent = `${botTxt} · ${manualTxt} (8/14~ 집계)`;
  }

  // 보유 포지션은 봇이 여는 것뿐 아니라 계좌에 실제로 떠 있는 수동 포지션도
  // 세야 정확하다 - manual_position_count 는 바이낸스를 직접 봐서 화이트리스트
  // 밖 페어(QQQ 등)도 잡는다(server.py 참고). 이게 빠지면 수동 포지션이 있어도
  // "보유 포지션 0" 으로 잘못 나온다.
  const openCount =
    connectedBots.reduce((sum, b) => sum + b.open_trades.length, 0) +
    (summary.combined.manual_position_count || 0);
  // 카드 숫자는 보유 포지션의 평가손익(봇 + 수동), 개수는 옆 칩에(2026-11-10).
  // manual_unrealized 가 null 이면 계좌 조회 실패 - 봇 것만 합치고 칩에 표시한다.
  const botOpenAbs = connectedBots.reduce(
    (sum, b) => sum + b.open_trades.reduce((a, t) => a + (t.profit_abs || 0), 0),
    0
  );
  const manualOpen = summary.combined.manual_unrealized;
  const openAbs = botOpenAbs + (manualOpen || 0);
  const openEl = document.getElementById("statOpenCount");
  openEl.textContent = openCount ? `$${fmtUsd(openAbs)}` : "–";
  openEl.className = `stat-value ${openCount ? pnlClass(openAbs) : ""}`;
  document.getElementById("statOpenChip").textContent =
    manualOpen === null || manualOpen === undefined ? `${openCount}개 · 수동 제외` : `${openCount}개 보유`;

  const totalTrades = connectedBots.reduce((sum, b) => sum + (b.trade_count || 0), 0);
  // 봇별 승률을 단순 평균하면 안 된다 - 거래가 0건인 봇(예: 막 시작한 봇)도
  // 승률 0%로 평균에 끼어들어 전체 승률을 실제보다 낮게 왜곡한다. 거래수로
  // 가중평균해야 "전체 거래 중 이긴 비율"이라는 원래 의미가 맞다.
  const totalWins = connectedBots.reduce(
    (sum, b) => sum + (b.trade_count || 0) * (b.winrate || 0),
    0
  );
  const avgWinrate = totalTrades > 0 ? totalWins / totalTrades : 0;
  document.getElementById("statWinrate").textContent = `${(avgWinrate * 100).toFixed(1)}%`;
  document.getElementById("statTradeCount").textContent = `${totalTrades} 거래`;
}

/* ---------------- Equity chart ---------------- */

// 전략별 그래프 색 (2026-11-10, dataviz 스킬 기준으로 다시 정함)
// - 색은 전략에 고정한다. 예전엔 "몇 번째로 연결된 봇이냐"로 색을 줘서, 봇 하나가
//   빠지면 남은 전략 색이 바뀌었다(RSI 가 어제는 파랑, 오늘은 주황).
// - 실제 색은 style.css 의 --series-1..5 (보라 / 주황 / 청록 / 노랑 / 자홍). 라이트·다크
//   각각 색각이상 시뮬레이션까지 validate_palette.js 로 검증한 값이다. 수익=빨강,
//   손실=파랑과 헷갈리지 않게 빨강·파랑은 전략 색에서 뺐다.
const SERIES_SLOT = { xsectrsi: 1, xsectmomentum: 2, manual: 3 };

function seriesColor(key, extraIndex = 0) {
  const slot = SERIES_SLOT[key] || 4 + extraIndex;
  // 5개를 넘는 전략은 새 색을 만들지 않고 회색 하나로 묶는다(색을 돌려 쓰면 구분이 안 된다)
  return slot <= 5 ? cssVar(`--series-${slot}`) : cssVar("--text-faint");
}

function escText(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function botShortName(b) {
  return b.name.replace(/\s*\(.*\)\s*/, "") || b.id;
}

// 누적/일별 그래프에 같이 쓰는 전략별 일별 손익 시리즈
function pnlSeries(summary) {
  // 봇 목록을 그대로 따라가도록 동적으로 구성한다.
  let extra = 0;
  const series = summary.bots
    .filter((b) => b.connected)
    .map((b) => ({
      key: b.id,
      label: botShortName(b),
      color: seriesColor(b.id, SERIES_SLOT[b.id] ? 0 : extra++),
      daily: b.daily || [],
    }));
  // 수동 매매도 봇과 같은 그래프에 한 시리즈로 얹는다 - server.py 의
  // manual_daily 가 봇 daily 와 같은 창으로 맞춰서 나온다.
  // 값이 전부 0인 빈 시리즈를 만들지 않도록, 실제로 손익이 있을 때만 넣는다.
  const manualDaily = summary.combined?.manual_daily || [];
  if (manualDaily.some((d) => Math.abs(d.abs_profit) > 0.0001)) {
    series.push({ key: "manual", label: "수동", color: seriesColor("manual"), daily: manualDaily });
  }
  return series;
}

// 범례는 그래프 모양을 따른다: 선 그래프는 짧은 선, 막대는 네모
function renderPnlLegend(items, shape = "line") {
  const legendEl = document.getElementById("equityLegend");
  if (!legendEl) return;
  setHTMLIfChanged(
    legendEl,
    items
      .map(
        (s) =>
          `<span class="legend-item"><i class="${shape === "line" ? "linekey" : "swatch"}" style="background:${s.color}"></i>${escText(s.label)}</span>`
      )
      .join("")
  );
}

// ---- 그래프 공통: 툴팁·축 색은 테마 토큰에서 ----
function tooltipTheme() {
  return {
    backgroundColor: cssVar("--card"),
    borderColor: cssVar("--border"),
    borderWidth: 1,
    titleColor: cssVar("--text-faint"),
    bodyColor: cssVar("--text"),
    footerColor: cssVar("--text-dim"),
    titleFont: { family: "Pretendard Variable", weight: "500" },
    bodyFont: { family: "Pretendard Variable", weight: "600" },
    footerFont: { family: "Pretendard Variable", weight: "500" },
    padding: 10,
  };
}

function axesTheme() {
  return {
    x: { grid: { display: false }, border: { color: cssVar("--border") }, ticks: { color: cssVar("--text-faint"), font: { size: 11 } } },
    y: {
      border: { display: false },
      // 0 선만 한 단계 진하게 - 손익이 플러스인지 마이너스인지가 이 그래프의 핵심
      grid: { color: (c) => (c.tick && c.tick.value === 0 ? cssVar("--text-faint") : cssVar("--grid")) },
      ticks: { color: cssVar("--text-faint"), font: { size: 11, family: "Pretendard Variable" }, maxTicksLimit: 6 },
    },
  };
}

// 마우스 위치의 날짜에 세로 실선 하나(선을 정확히 겨누지 않아도 그날 값이 다 보인다)
const crosshairPlugin = {
  id: "crosshair",
  beforeDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements?.() || [];
    if (!active.length) return;
    const x = Math.round(active[0].element.x) + 0.5;
    const { top, bottom } = chart.chartArea;
    const c = chart.ctx;
    c.save();
    c.strokeStyle = cssVar("--text-faint");
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(x, top);
    c.lineTo(x, bottom);
    c.stroke();
    c.restore();
  },
};

// 선 끝에 점 + 전략 이름(글자는 회색 글자색, 색은 점과 선이 맡는다).
// 끝이 서로 붙어 있으면 이름을 겹쳐 쌓지 않고 범례에 맡긴다.
const endLabelPlugin = {
  id: "endLabels",
  afterDatasetsDraw(chart) {
    const items = [];
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden || !meta.data.length) return;
      const pt = meta.data[meta.data.length - 1];
      items.push({ x: pt.x, y: pt.y, label: ds.label, color: ds.borderColor });
    });
    const c = chart.ctx;
    const surface = cssVar("--card");
    c.save();
    for (const it of items) {
      c.beginPath();
      c.arc(it.x, it.y, 6, 0, Math.PI * 2);
      c.fillStyle = surface;
      c.fill();
      c.beginPath();
      c.arc(it.x, it.y, 4, 0, Math.PI * 2);
      c.fillStyle = it.color;
      c.fill();
    }
    c.font = '600 11px "Pretendard Variable", sans-serif';
    c.textBaseline = "middle";
    c.fillStyle = cssVar("--text-dim");
    let lastY = -Infinity;
    for (const it of [...items].sort((a, b) => a.y - b.y)) {
      if (it.y - lastY < 14) continue;
      c.fillText(it.label, it.x + 9, it.y);
      lastY = it.y;
    }
    c.restore();
  },
};

const signed = (v, d = 2) => `${v >= 0 ? "+" : ""}${v.toFixed(d)}`;

// 그래프 탭: "cum"(누적 손익 추이, 선) / "daily"(일별 손익 추이, 막대). 새로고침해도 유지.
let pnlView = (() => {
  try {
    return localStorage.getItem("pnlView") === "daily" ? "daily" : "cum";
  } catch {
    return "cum";
  }
})();
let dailyChart = null;

function renderEquityChart(summary) {
  // 보고 있는 탭만 그린다(기간 버튼/4초 갱신 모두 여기로 들어온다)
  if (pnlView === "daily") return renderDailyChart(summary);
  const ctx = document.getElementById("equityChart");

  const sub = document.getElementById("equitySub");
  if (sub) sub.textContent = `최근 ${equityDays}일 · 전략별`;

  const cumulative = (daily) => {
    let acc = 0;
    return daily.map((d) => (acc += d.abs_profit));
  };
  // 그래프에는 손익 "금액"만 찍지만, 툴팁에는 그 시점 기준 수익률(%)도 같이 보여주기 위해
  // 시작 잔고 대비 누적 손익 비율을 별도 배열로 계산해둠
  const cumulativePct = (daily) => {
    let acc = 0;
    return daily.map((d) => {
      acc += d.abs_profit;
      return d.starting_balance ? (acc / d.starting_balance) * 100 : 0;
    });
  };

  const series = pnlSeries(summary);

  const longest = series.reduce(
    (best, s) => (s.daily.length > best.length ? s.daily : best),
    []
  );
  const labels = longest.map((d) => d.date.slice(5));

  renderPnlLegend(series, "line");

  // 선만 그린다. 전략마다 아래를 칠하면 겹친 면이 "합계"처럼 읽힌다.
  const datasets = series.map((s) => ({
    key: s.key,
    label: s.label,
    data: cumulative(s.daily),
    pctData: cumulativePct(s.daily),
    borderColor: s.color,
    backgroundColor: s.color,
    fill: false,
    tension: 0.3,
    cubicInterpolationMode: "monotone", // 곡선이 실제 값보다 위아래로 튀지 않게
    borderWidth: 2,
    borderCapStyle: "round",
    borderJoinStyle: "round",
    pointRadius: 0,
    pointHitRadius: 12,
    pointHoverRadius: 4,
    pointHoverBorderWidth: 2,
    pointHoverBorderColor: cssVar("--card"),
    pointHoverBackgroundColor: s.color,
  }));

  // 시작 직후엔 일별 손익이 전부 0이라 그래프가 텅 빈 직선으로 보여서
  // 마치 깨진 것처럼 보임 -> 의미 있는 데이터가 없으면 안내 문구로 대체
  const hasSignal = datasets.some((d) => d.data.some((v) => Math.abs(v) > 0.0001));
  const emptyState = document.getElementById("equityEmptyState");
  emptyState.hidden = hasSignal;
  ctx.style.visibility = hasSignal ? "visible" : "hidden";
  if (!hasSignal && !equityChart) return;

  // 봇 구성이 바뀌면(개수/이름) 차트를 새로 만든다
  const sameShape =
    equityChart &&
    equityChart.data.datasets.length === datasets.length &&
    equityChart.data.datasets.every((d, i) => d.label === datasets[i].label);

  if (sameShape) {
    equityChart.data.labels = labels;
    datasets.forEach((d, i) => {
      equityChart.data.datasets[i].data = d.data;
      equityChart.data.datasets[i].pctData = d.pctData;
    });
    equityChart.update("none");
    return;
  }
  if (equityChart) {
    equityChart.destroy();
    equityChart = null;
  }

  equityChart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    plugins: [crosshairPlugin, endLabelPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { right: 78, top: 6 } }, // 선 끝 이름 자리("Momentum" 까지)
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          ...tooltipTheme(),
          usePointStyle: true,
          callbacks: {
            // 툴팁 키는 네모 대신 짧은 선, 값이 먼저 오고 전략 이름이 뒤에
            labelPointStyle: () => ({ pointStyle: "line", rotation: 0 }),
            labelColor: (c) => ({ borderColor: c.dataset.borderColor, backgroundColor: c.dataset.borderColor, borderWidth: 2 }),
            label: (context) => {
              const ds = context.dataset;
              const amt = context.parsed.y;
              const pct = ds.pctData ? ds.pctData[context.dataIndex] : null;
              const pctStr = pct === null ? "" : ` (${signed(pct)}%)`;
              return `${signed(amt)} USDT${pctStr}  ${ds.label}`;
            },
          },
        },
      },
      scales: axesTheme(),
    },
  });
}

/* ---------------- 포지션 방향 ----------------
   2조각짜리 도넛은 두 숫자를 각도로 비교하게 만들 뿐이라(dataviz: "2-slice pie" 금지),
   롱/숏 비율은 긴 막대 하나로, 숫자는 그 아래 표로 보여준다(2026-11-10).
   함수 이름은 호출하는 곳이 많아 그대로 둔다. */
function renderDonut(summary) {
  const el = document.getElementById("dirSummary");
  if (!el) return;
  const rows = [];
  summary.bots.forEach((b) => {
    if (!b.connected) return;
    let l = 0;
    let sh = 0;
    (b.open_trades || []).forEach((t) => (t.is_short ? sh++ : l++));
    if (l + sh) rows.push({ name: botShortName(b), l, s: sh });
  });
  // 봇이 모르는 수동 포지션도 계좌 기준으로 넣는다(server.py manual_longs/shorts)
  const ml = summary.combined.manual_longs || 0;
  const ms = summary.combined.manual_shorts || 0;
  if (ml + ms) rows.push({ name: "수동", l: ml, s: ms });

  const longs = rows.reduce((a, r) => a + r.l, 0);
  const shorts = rows.reduce((a, r) => a + r.s, 0);
  const total = longs + shorts;
  if (!total) {
    setHTMLIfChanged(el, `<div class="dir-empty">보유 중인 포지션이 없습니다</div>`);
    return;
  }
  const pct = (n) => Math.round((n / total) * 100);
  const seg = (cls, n, name) =>
    n ? `<div class="dir-seg ${cls}" style="flex:${n}" title="${name} ${n}개 · ${pct(n)}%">${n}</div>` : "";
  setHTMLIfChanged(
    el,
    `<div class="dir-bar" role="img" aria-label="롱 ${longs}개 ${pct(longs)}%, 숏 ${shorts}개 ${pct(shorts)}%">${seg("long", longs, "롱")}${seg("short", shorts, "숏")}</div>
    <div class="dir-legend"><span><i class="swatch" style="background:var(--green)"></i>롱</span><span><i class="swatch" style="background:var(--red)"></i>숏</span></div>
    <table class="dir-table">
      <thead><tr><th></th><th>롱</th><th>숏</th></tr></thead>
      <tbody>
        ${rows.map((r) => `<tr><td>${escText(r.name)}</td><td>${r.l}</td><td>${r.s}</td></tr>`).join("")}
        <tr class="dir-total"><td>합계</td><td>${longs}</td><td>${shorts}</td></tr>
      </tbody>
    </table>`
  );
}

/* ---------------- Position cards ---------------- */

function renderOpenTrades(trades, botId) {
  if (!trades.length) {
    return `<div class="empty-row">보유 포지션 없음</div>`;
  }
  const rows = trades
    .map((t) => {
      const sideClass = t.is_short ? "side-short" : "side-long";
      const sideLabel = t.is_short ? "SHORT" : "LONG";
      // 손절/익절은 진입가 대비 몇 % 떨어져 있는지도 같이 보여준다.
      // 이 전략은 손절선이 박스 경계라 포지션마다 다르다.
      const dist = (target) =>
        target && t.open_rate ? ((target - t.open_rate) / t.open_rate) * 100 : null;
      const sl = t.stop_loss_abs;
      const tp = t.take_profit_abs;
      const slD = dist(sl);
      const tpD = dist(tp);
      const rem = t.hold_remaining_h;
      const remBase =
        rem === null || rem === undefined
          ? "–"
          : rem >= 1
          ? `${Math.floor(rem)}시간`
          : `${Math.round(rem * 60)}분`;
      // 3일이 지나 순위 유지로 하루 연장 중이면 4일 상한까지 남은 시간
      const remTxt = t.hold_extended ? `연장 · ${remBase}` : remBase;
      const remCls = rem !== null && rem !== undefined && rem < 6 ? "warn" : "";
      // 줄을 누르면 오른쪽 차트가 이 코인으로 바뀐다 (charts.js)
      const chartBase = t.pair.split("/")[0];
      return `
        <tr class="chart-row" data-chart-base="${chartBase}" title="누르면 오른쪽에 ${chartBase} 차트">
          <td class="pair-cell">${fmtPair(t.pair)}<span class="lvl-d">${t.leverage}x</span></td>
          <td><span class="side-pill ${sideClass}">${sideLabel}</span></td>
          <td>${fmtNum(t.open_rate)}</td>
          <td>${fmtNum(t.current_rate)}</td>
          <td class="${pnlClass(t.profit_abs)}">${fmtUsd(t.profit_abs)} USDT<span class="pnl-sub">${fmtPct(t.profit_pct)}</span></td>
          <td class="lvl pos">${fmtNum(tp)}${tpD === null ? "" : `<span class="lvl-d">${tpD > 0 ? "+" : ""}${tpD.toFixed(1)}%</span>`}</td>
          <td class="lvl neg">${fmtNum(sl)}${slD === null ? "" : `<span class="lvl-d">${slD > 0 ? "+" : ""}${slD.toFixed(1)}%</span>`}</td>
          <td class="${remCls}">${remTxt}</td>
        </tr>`;
    })
    .join("");
  return `
    <div class="pos-table-wrap" data-bot="${botId ?? ""}">
    <table class="pos-table">
      <thead>
        <tr><th>페어(배율)</th><th>방향</th><th>진입가</th><th>현재가</th>
            <th>손익</th><th>익절</th><th>손절</th><th>청산까지</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    </div>`;
}

function fmtNum(v) {
  if (v === null || v === undefined) return "–";
  return v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 2 : 4 });
}

/* ---------------- 로그인 상태 ----------------
   대시보드 열람은 로그인 없이 가능하고, 봇 시작/정지 같은 조작만 로그인이 필요하다.
   서버는 HTTP Basic을 쓰므로, /login 으로 이동하면 브라우저가 인증 팝업을 띄우고
   성공하면 대시보드로 되돌아온다. 이후 브라우저가 자격증명을 계속 붙여준다. */
let isAuthed = false;

function goLogin() {
  window.location.href = "/login";
}

async function doLogout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch {
    /* 실패해도 아래에서 상태를 다시 읽어 반영한다 */
  }
  isAuthed = false;
  await refreshAuthState();
  if (typeof refresh === "function") refresh();
}

async function refreshAuthState() {
  try {
    const res = await fetch("/api/whoami", { cache: "no-store" });
    const data = await res.json();
    isAuthed = !!data.authenticated;
  } catch {
    isAuthed = false;
  }
  const el = document.getElementById("authControl");
  if (el) {
    el.innerHTML = isAuthed
      ? `<span class="auth-badge on">로그인됨</span><button class="auth-btn" id="logoutBtn">로그아웃</button>`
      : `<button class="auth-btn" id="loginBtn">로그인</button>`;
  }
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#loginBtn")) goLogin();
  if (e.target.closest("#logoutBtn")) doLogout();
});

/* 봇 시작/정지 버튼.
   freqtrade의 state는 running / stopped / paused 세 가지.
   running이 아니면 "정지됨"으로 보고 시작 버튼을 띄운다. */
function renderBotPower(bot) {
  // 컨테이너째 꺼둔 봇을 켜는 중(1~2분) - 버튼을 잠가 중복 요청을 막는다
  if (bot.state === "starting") {
    return `
    <span class="state-badge off">켜는 중…</span>
    <button class="power-btn start" disabled>켜는 중</button>`;
  }
  const running = bot.state === "running";
  const label = running ? "정지" : "시작";
  const cls = running ? "power-btn stop" : "power-btn start";
  // 로그인 안 했으면 버튼은 보이되 눌리지 않는다(열람은 누구나, 조작은 로그인)
  const locked = !isAuthed;
  const title = locked ? "로그인이 필요합니다" : "";
  return `
    <span class="state-badge ${running ? "on" : "off"}">${running ? "가동중" : "정지"}</span>
    <button class="${cls}" data-bot="${bot.id}" data-action="${running ? "stop" : "start"}"
            ${locked ? "disabled" : ""} title="${title}">${locked ? "🔒 " : ""}${label}</button>`;
}

async function onBotPowerClick(botId, action) {
  if (!isAuthed) {
    if (window.confirm("봇 조작은 로그인이 필요합니다.\n로그인하시겠습니까?")) goLogin();
    return;
  }
  const isStop = action === "stop";
  const msg = isStop
    ? "봇을 정지하시겠습니까?\n\n신규 진입이 멈춥니다. 열린 포지션이 있으면 그 포지션의 손절/익절 관리도 함께 멈추므로 직접 관리하셔야 합니다."
    : "봇을 시작하시겠습니까?\n\n실전 모드라면 즉시 실제 주문이 나갈 수 있습니다.";
  if (!window.confirm(msg)) return;

  const btns = document.querySelectorAll(`.power-btn[data-bot="${botId}"]`);
  btns.forEach((b) => { b.disabled = true; b.textContent = "..."; });
  try {
    const res = await fetch(`/api/bots/${botId}/${action}`, { method: "POST" });
    const data = await res.json();
    if (res.status === 401) {
      isAuthed = false;
      if (window.confirm("로그인이 필요합니다.\n로그인하시겠습니까?")) goLogin();
    } else if (!data.ok) {
      window.alert(`실패: ${data.error || "알 수 없는 오류"}`);
    } else if (data.warning) {
      window.alert(`정지했습니다.\n\n주의: ${data.warning}${data.message ? `\n\n${data.message}` : ""}`);
    } else if (data.state === "starting" || (isStop && data.message && data.message.includes("컨테이너"))) {
      window.alert(data.message);
    }
  } catch (err) {
    window.alert(`요청 실패: ${err}`);
  } finally {
    if (typeof refresh === "function") refresh();
  }
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".power-btn");
  if (btn && !btn.disabled) {
    onBotPowerClick(btn.dataset.bot, btn.dataset.action);
  }
});

// 봇 카드 헤더의 이름 자리 자체가 탭이다 - 전체 봇 목록을 받아 탭들을
// 그리고, selectedBotId 에 해당하는 봇의 내용만 카드 나머지에 채운다.
// 봇 탭 이름: "RSI (RSI 순위)" -> 굵은 "RSI" + 작은 "RSI 순위".
// 폰에서는 괄호 설명을 숨기고 한 줄로만 보여준다(예전엔 글자 단위로 줄이 깨졌다).
function botTabLabel(name) {
  const m = String(name || "").match(/^(.*?)\s*\((.*)\)\s*$/);
  const main = m ? m[1] : name;
  const sub = m ? `<span class="bt-sub">${escText(m[2])}</span>` : "";
  return `<span class="bt-main">${escText(main)}</span>${sub}`;
}

function renderBotTitleTabs(bots, selectedBotId) {
  return `<div class="bot-title-tabs">${bots
    .map((b) => {
      const active = b.id === selectedBotId ? "active" : "";
      // 초록 = 연결됐고 가동 중, 빨강 = 정지 중이거나 연결 안 됨 (정지된 봇이 초록이라 헷갈렸음)
      const dotCls = b.connected && b.state === "running" ? "ok" : "err";
      return `<button type="button" class="bot-tab ${active}" data-bot="${b.id}">
        <span class="dot ${dotCls}"></span>${botTabLabel(b.name)}
      </button>`;
    })
    .join("")}</div>`;
}

function renderBotCard(bots, selectedBotId) {
  const bot = bots.find((b) => b.id === selectedBotId) || bots[0];
  if (!bot) return `<div class="empty-row">표시할 봇이 없습니다</div>`;

  if (!bot.connected) {
    return `
      <div class="bot-card">
        <div class="bot-card-head">
          ${renderBotTitleTabs(bots, bot.id)}
        </div>
        <div class="card-error">봇에 연결할 수 없습니다${bot.error ? ` (${bot.error})` : ""}</div>
      </div>`;
  }

  // 지금 들고 있는 포지션의 미실현 손익 합계(2026-10-18). %는 투입 증거금 합계 대비라
  // 포지션 표의 종목별 손익 %(레버리지 반영)와 같은 기준이다.
  const openTrades = bot.open_trades || [];
  const openAbs = openTrades.reduce((a, t) => a + (t.profit_abs || 0), 0);
  const openStake = openTrades.reduce((a, t) => a + (t.stake_amount || 0), 0);
  const openPct = openStake ? (openAbs / openStake) * 100 : 0;
  const openStat = openTrades.length
    ? `<div class="v ${pnlClass(openAbs)}">${fmtPct(openPct)}<span class="v-sub">${fmtUsd(openAbs)} USDT · ${openTrades.length}개</span></div>`
    : `<div class="v neutral">–<span class="v-sub">보유 포지션 없음</span></div>`;

  return `
    <div class="bot-card">
      <div class="bot-card-head">
        ${renderBotTitleTabs(bots, bot.id)}
        <div class="badge-row">
          <span class="lev-badge">${bot.leverage}x</span>
          ${renderBotPower(bot)}
        </div>
      </div>
      <div class="mini-stats">
        <div class="mini-stat"><div class="l">잔고</div><div class="v">$${bot.balance_total.toFixed(2)}</div></div>
        <div class="mini-stat"><div class="l">총손익</div><div class="v ${pnlClass(bot.profit_all_abs)}">${fmtPct(bot.profit_all_pct)}<span class="v-sub">${fmtUsd(bot.profit_all_abs)} USDT</span></div></div>
        <div class="mini-stat"><div class="l">보유 포지션 손익</div>${openStat}</div>
        <div class="mini-stat"><div class="l">승률</div><div class="v">${(bot.winrate * 100).toFixed(1)}%</div></div>
        <div class="mini-stat"><div class="l">MDD</div><div class="v neg">${(bot.max_drawdown * 100).toFixed(2)}%</div></div>
      </div>
      ${renderOpenTrades(bot.open_trades, bot.id)}
    </div>`;
}

/* ---------------- History table ---------------- */

// 표에 몇 페이지째를 보고 있는지. renderHistory 는 4초마다 다시 호출되므로
// 여기서 들고 있지 않으면 폴링될 때마다 1페이지로 되돌아간다.
let historyPage = 0;
const HISTORY_PAGE_SIZE = 10;

function renderHistory(summary) {
  const rows = [];
  summary.bots.forEach((b) => {
    if (!b.connected) return;
    b.recent_trades.forEach((t) =>
      rows.push({
        strategy: b.name.split(" ")[0],
        ...t,
      })
    );
  });
  // 봇이 기록하지 않은 청산(앱/웹에서 직접 처리한 것). server.py 의
  // fetch_manual_trade_history() 가 계좌 실현손익에서 봇 기록과 겹치지 않는
  // 것만 걸러서 준다. TQQQ처럼 봇이 아예 모르는 페어도 여기 들어온다.
  (summary.combined?.manual_trades || []).forEach((t) =>
    rows.push({ strategy: "수동", manual: true, ...t })
  );
  rows.sort((a, b) => new Date(b.close_date) - new Date(a.close_date));

  const tbody = document.querySelector("#historyTable tbody");
  const pager = document.getElementById("historyPager");
  if (!rows.length) {
    setHTMLIfChanged(tbody, `<tr><td colspan="7" class="empty-row">청산된 거래가 아직 없습니다</td></tr>`);
    if (pager) setHTMLIfChanged(pager, "");
    return;
  }

  const totalPages = Math.max(1, Math.ceil(rows.length / HISTORY_PAGE_SIZE));
  // 폴링 사이에 거래가 새로 잡히거나 청산돼 rows.length 가 줄면 지금 보던
  // 페이지가 범위를 넘어갈 수 있다 - 마지막 페이지로 당겨준다.
  historyPage = Math.min(historyPage, totalPages - 1);
  const start = historyPage * HISTORY_PAGE_SIZE;
  const pageRows = rows.slice(start, start + HISTORY_PAGE_SIZE);

  const html = pageRows
    .map((t) => {
      // 수동 청산은 체결 기록에서 방향을 못 찾으면 is_short 가 null 로 온다 -
      // 이때 LONG으로 단정하면 틀릴 수 있으므로 "모름" 배지를 따로 둔다.
      let sidePill;
      if (t.is_short === true) sidePill = `<span class="side-pill side-short">SHORT</span>`;
      else if (t.is_short === false) sidePill = `<span class="side-pill side-long">LONG</span>`;
      else sidePill = `<span class="neutral">–</span>`;
      const strategyCls = t.manual ? "ord-owner manual" : "neutral";
      const chartBase = t.pair.split("/")[0];
      return `
        <tr class="chart-row" data-chart-base="${chartBase}" title="누르면 오른쪽에 ${chartBase} 차트">
          <td><span class="${strategyCls}">${t.strategy}</span></td>
          <td class="pair-cell">${fmtPair(t.pair)}</td>
          <td>${sidePill}</td>
          <td class="${pnlClass(t.close_profit_pct)}">${fmtPct(t.close_profit_pct)}</td>
          <td class="${pnlClass(t.close_profit_abs)}">${fmtUsd(t.close_profit_abs)}</td>
          <td class="neutral">${t.exit_reason_ko ?? t.exit_reason ?? "–"}</td>
          <td class="neutral">${timeAgo(t.close_date)}</td>
        </tr>`;
    })
    .join("");
  setHTMLIfChanged(tbody, html);

  if (pager) {
    if (totalPages <= 1) {
      setHTMLIfChanged(pager, "");
    } else {
      setHTMLIfChanged(
        pager,
        `<button class="pager-btn" id="historyPrevBtn" ${historyPage === 0 ? "disabled" : ""}>‹ 이전</button>
         <span class="pager-label">${historyPage + 1} / ${totalPages} 페이지 · 총 ${rows.length}건</span>
         <button class="pager-btn" id="historyNextBtn" ${historyPage >= totalPages - 1 ? "disabled" : ""}>다음 ›</button>`
      );
    }
  }
}

// 페이지 버튼은 renderHistory 가 setHTMLIfChanged 로 통째로 다시 그리므로,
// 버튼에 직접 리스너를 매번 다는 게 아니라 부모(historyPager)에 위임한다
// (미체결 주문 그룹 펼치기와 같은 패턴 - 안 그러면 내용이 안 바뀐 폴링에서
// 리스너가 계속 중복으로 쌓인다).
document.getElementById("historyPager")?.addEventListener("click", (e) => {
  if (e.target.closest("#historyPrevBtn")) {
    historyPage = Math.max(0, historyPage - 1);
    if (lastSummary) renderHistory(lastSummary);
  } else if (e.target.closest("#historyNextBtn")) {
    historyPage += 1;
    if (lastSummary) renderHistory(lastSummary);
  }
});

// 누적손익추이 기간 버튼. 이 버튼들은 정적 마크업(index.html)이라 다시
// 그려지지 않으므로 historyPager 처럼 위임할 필요는 없지만, 같은 형태로
// 맞춰뒀다. days 는 서버(api_summary)가 실제로 쓰는 timescale 이라 값이
// 바뀌면 다음 폴링(4초)을 기다리지 않고 바로 다시 불러온다.
document.getElementById("equityPeriodToggle")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".period-btn");
  if (!btn) return;
  const days = Number(btn.dataset.days);
  if (!days || days === equityDays) return;
  equityDays = days;
  document
    .querySelectorAll("#equityPeriodToggle .period-btn")
    .forEach((b) => b.classList.toggle("active", b === btn));
  if (summaryByDays[days]) renderEquityChart(summaryByDays[days]);
  refresh();
});

// ---------------- 일별 손익 추이 (막대) ----------------
// 날짜마다 그날 청산해서 확정된 손익의 "합계"를 막대 하나로 그린다(미실현 손익 제외).
// 예전엔 전략별로 쌓았는데, 같은 날 한 전략은 +, 다른 전략은 - 이면 막대가 0선 위아래로
// 갈라져 합계를 읽을 수 없었다. 이 그래프의 질문은 "그날 벌었나 잃었나" 하나라서
// 0선 기준 위(수익, 빨강)/아래(손실, 파랑)로만 나누고, 전략별 내역은 툴팁에 둔다.
function renderDailyChart(summary) {
  const ctx = document.getElementById("dailyChart");
  const series = pnlSeries(summary);
  renderPnlLegend(
    [
      { label: "수익일", color: "var(--green)" },
      { label: "손실일", color: "var(--red)" },
    ],
    "bar"
  );

  const longest = series.reduce((best, s) => (s.daily.length > best.length ? s.daily : best), []);
  const labels = longest.map((d) => d.date.slice(5));
  const totals = labels.map((_, i) =>
    series.reduce((a, s) => a + (s.daily[i] ? s.daily[i].abs_profit : 0), 0)
  );
  const counts = labels.map((_, i) =>
    series.reduce((a, s) => a + (s.daily[i] ? s.daily[i].trade_count || 0 : 0), 0)
  );

  const sum = totals.reduce((a, v) => a + v, 0);
  const upDays = totals.filter((v) => v > 0.0001).length;
  const downDays = totals.filter((v) => v < -0.0001).length;
  const sub = document.getElementById("equitySub");
  if (sub) {
    sub.innerHTML =
      `최근 ${equityDays}일 · 청산 확정 손익 합계 ` +
      `<span class="${pnlClass(sum)}">${fmtUsd(sum)} USDT</span> · 수익일 ${upDays} / 손실일 ${downDays}`;
  }

  const hasSignal = totals.some((v) => Math.abs(v) > 0.0001);
  const emptyState = document.getElementById("equityEmptyState");
  emptyState.hidden = hasSignal;
  ctx.style.visibility = hasSignal ? "visible" : "hidden";
  if (!hasSignal && !dailyChart) return;

  const up = cssVar("--green");
  const down = cssVar("--red");
  const colors = totals.map((v) => (v >= 0 ? up : down));

  if (dailyChart) {
    dailyChart.data.labels = labels;
    dailyChart.data.datasets[0].data = totals;
    dailyChart.data.datasets[0].backgroundColor = colors;
    dailyChart.$series = series;
    dailyChart.$counts = counts;
    dailyChart.update("none");
    return;
  }
  dailyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "하루 손익",
          data: totals,
          backgroundColor: colors,
          borderRadius: 4, // 끝만 둥글게, 0선 쪽은 각지게(borderSkipped 기본값)
          maxBarThickness: 24,
          categoryPercentage: 0.8,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          ...tooltipTheme(),
          displayColors: false,
          callbacks: {
            label: (c) => {
              const n = dailyChart.$counts[c.dataIndex];
              return `${signed(c.parsed.y)} USDT · 청산 ${n}건`;
            },
            // 전략별 내역(그날 거래 없는 전략은 생략)
            footer: (items) => {
              if (!items.length) return "";
              const i = items[0].dataIndex;
              return dailyChart.$series
                .map((s) => ({ s, d: s.daily[i] }))
                .filter(({ d }) => d && (Math.abs(d.abs_profit) > 0.0001 || d.trade_count))
                .map(({ s, d }) => `${s.label}  ${signed(d.abs_profit)} USDT (${d.trade_count || 0}건)`);
            },
          },
        },
      },
      scales: axesTheme(),
    },
  });
  dailyChart.$series = series;
  dailyChart.$counts = counts;
}

function applyPnlView() {
  document
    .querySelectorAll("#pnlChartTabs .chart-tab")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === pnlView));
  document.getElementById("equityChart").style.display = pnlView === "cum" ? "" : "none";
  document.getElementById("dailyChart").style.display = pnlView === "daily" ? "" : "none";
  const s = summaryByDays[equityDays] || lastSummary;
  if (s) renderEquityChart(s);
  // 숨겨져 있던 캔버스는 크기가 0으로 잡혀 있을 수 있어 다시 맞춘다
  if (pnlView === "daily" && dailyChart) dailyChart.resize();
  if (pnlView === "cum" && equityChart) equityChart.resize();
}

document.getElementById("pnlChartTabs")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".chart-tab");
  if (!btn || btn.dataset.view === pnlView) return;
  pnlView = btn.dataset.view;
  try {
    localStorage.setItem("pnlView", pnlView);
  } catch {
    /* 저장 못 해도 이번 화면에선 동작 */
  }
  applyPnlView();
});
applyPnlView();

// 처음 열 때 다른 기간도 미리 받아둔다 - 첫 클릭부터 바로 바뀌게
async function prefetchOtherPeriods() {
  for (const d of [7, 14, 30, 60]) {
    if (d === equityDays || summaryByDays[d]) continue;
    try {
      summaryByDays[d] = await fetchJSON(`/api/summary?days=${d}`);
    } catch {
      /* 실패하면 버튼을 눌렀을 때 받아온다 */
    }
  }
}

/* ---------------- Alerts feed ---------------- */

// alerts는 최신순 정렬(맨 앞이 가장 최근). 마지막으로 확인한 시각보다 새로운
// 알림이 있으면 진입/청산에 맞는 소리를 재생함. 첫 로드 때는(과거 알림 다 재생 방지)
// 소리 없이 기준점만 세팅.
function checkNewAlertsAndPlaySound(alerts) {
  if (!alerts.length) return;

  // alerts[0]이 항상 최신이라고 믿지 않는다 - 서버가 뒤늦게 발견한 과거
  // 이력을 채워 넣는 과정에서 한 번 이 가정이 깨진 적이 있고(맨 앞이 실제로는
  // 과거 시각이라 기준시각이 되돌아가며 이미 재생한 알림음이 반복 재생됨),
  // 그건 서버 쪽에서 고쳤지만 여기서도 실제 최댓값을 직접 구해서 한 번 더
  // 방어한다. ISO 8601 문자열은 사전식 비교가 시간순 비교와 일치한다.
  let maxTime = alerts[0].time;
  for (const a of alerts) if (a.time > maxTime) maxTime = a.time;

  if (lastSeenAlertTime === null) {
    lastSeenAlertTime = maxTime;
    return;
  }

  const newOnes = alerts.filter((a) => a.time > lastSeenAlertTime);
  if (newOnes.length === 0) return;

  // 오래된 것부터 순서대로 소리 재생
  newOnes
    .slice()
    .sort((a, b) => (a.time > b.time ? 1 : -1))
    .forEach((a) => {
      if (a.event === "entry_fill") playEntrySound();
      else if (a.event === "exit_fill") playExitSound((a.profit_ratio_pct ?? 0) >= 0);
      else if (a.event === "profit_milestone") playExitSound((a.step ?? 1) > 0);
    });

  // 기준시각은 앞으로만 움직인다 - 혹시라도 이번에 온 alerts의 최댓값이
  // 기존 기준시각보다 과거라면(있어선 안 되지만) 절대 되돌리지 않는다.
  if (maxTime > lastSeenAlertTime) lastSeenAlertTime = maxTime;
}

// 알림 창 필터: "all" | "entry_fill" | "exit_fill" | "profit_milestone"
const ALERT_KINDS = ["entry_fill", "exit_fill", "profit_milestone"];
let alertFilter = "all";
let lastAlerts = [];

function renderAlerts(alerts) {
  lastAlerts = alerts;
  const el = document.getElementById("alertsList");
  // 화면에 보여주는 종류만 센다(봇 상태 메시지 등은 목록에 안 나온다)
  const counts = { all: 0 };
  for (const a of alerts) {
    if (!ALERT_KINDS.includes(a.event)) continue;
    counts[a.event] = (counts[a.event] || 0) + 1;
    counts.all += 1;
  }
  document.querySelectorAll("#alertFilters .cnt").forEach((c) => {
    c.textContent = counts[c.dataset.cnt] || 0;
  });
  const shown = alerts.filter((a) =>
    alertFilter === "all" ? ALERT_KINDS.includes(a.event) : a.event === alertFilter
  );
  if (!shown.length) {
    setHTMLIfChanged(
      el,
      `<div class="empty-row">${alerts.length ? "이 종류의 알림이 없습니다" : "아직 알림이 없습니다"}</div>`
    );
    return;
  }
  const html = shown
    .map((a) => {
      if (a.event === "entry_fill") {
        return `
          <div class="alert-item">
            <div class="alert-icon entry">${a.is_short ? "S" : "L"}</div>
            <div class="alert-body">
              <div class="alert-title">
                <span class="alert-bot-tag">${a.bot_name}</span>
                진입 체결 · ${fmtPair(a.pair)} · <span class="${a.is_short ? "neg" : "pos"}">${a.side_ko}</span>
              </div>
            </div>
            <div class="alert-time">${timeAgo(a.time.replace("T", " ").slice(0, 19))}</div>
          </div>`;
      }
      if (a.event === "exit_fill") {
        const cls = pnlClass(a.profit_ratio_pct);
        const iconCls = a.profit_ratio_pct >= 0 ? "exit-pos" : "exit-neg";
        return `
          <div class="alert-item">
            <div class="alert-icon ${iconCls}">${a.is_short ? "S" : "L"}</div>
            <div class="alert-body">
              <div class="alert-title">
                <span class="alert-bot-tag">${a.bot_name}</span>
                청산 완료 · ${fmtPair(a.pair)} · <span class="${cls}">${fmtPct(a.profit_ratio_pct)}</span>
              </div>
              <div class="alert-sub">${fmtUsd(a.profit_amount)} ${a.stake_currency} · ${a.exit_reason_ko}</div>
            </div>
            <div class="alert-time">${timeAgo(a.time.replace("T", " ").slice(0, 19))}</div>
          </div>`;
      }
      if (a.event === "profit_milestone") {
        const up = a.step > 0;
        return `
          <div class="alert-item">
            <div class="alert-icon ${up ? "exit-pos" : "exit-neg"}">${a.is_short ? "S" : "L"}</div>
            <div class="alert-body">
              <div class="alert-title">
                <span class="alert-bot-tag">${a.bot_name}</span>
                ${up ? `수익 +${a.step}%` : `손실 ${a.step}%`} 도달 · ${fmtPair(a.pair)} · <span class="${up ? "pos" : "neg"}">${fmtPct(a.profit_ratio_pct)}</span>
              </div>
              <div class="alert-sub">${fmtUsd(a.profit_amount)} USDT · 보유 중 (직접 정리 판단용 알림)</div>
            </div>
            <div class="alert-time">${timeAgo(a.time.replace("T", " ").slice(0, 19))}</div>
          </div>`;
      }
      return "";
    })
    .join("");
  setHTMLIfChanged(el, html);
}

document.getElementById("alertFilters")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".alert-filter");
  if (!btn || btn.dataset.filter === alertFilter) return;
  alertFilter = btn.dataset.filter;
  document
    .querySelectorAll("#alertFilters .alert-filter")
    .forEach((b) => b.classList.toggle("active", b === btn));
  renderAlerts(lastAlerts);
  document.getElementById("alertModal").scrollTop = 0; // 스크롤은 모달 전체가 한다
});

/* ---------------- 화면 전환 (2026-11-10) ----------------
   상단 메뉴(모바일은 하단바)가 스크롤 이동 대신 화면을 바꾼다.
   현재 화면은 주소 끝(#signals 등)에 남겨서 새로고침·뒤로가기에도 유지된다.
   첫 화면은 index.html <head> 가 CSS 보다 먼저 정한다. */
const VIEWS = ["overview", "signals", "history", "ai"];

function setView(view, push = true) {
  if (!VIEWS.includes(view)) view = "overview";
  document.documentElement.dataset.view = view;
  document.querySelectorAll(".nav-item[data-view]").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  const content = document.querySelector(".content");
  if (content) content.scrollTop = 0;
  if (push && location.hash !== `#${view}`) history.pushState(null, "", `#${view}`);
  // 숨겨져 있던 그래프는 크기가 0으로 잡혀 있을 수 있어 다시 맞춘다
  if (view === "overview") {
    requestAnimationFrame(() => {
      equityChart?.resize();
      dailyChart?.resize();
    });
  }
}

document.querySelectorAll(".nav-item[data-view]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    setView(a.dataset.view);
  });
});
window.addEventListener("popstate", () => setView((location.hash || "").slice(1), false));
setView(document.documentElement.dataset.view, false);

// 시세표: 넓은 화면에선 오른쪽 BTC·QQQ 차트 위, 오른쪽 패널이 숨는 좁은 화면(1400px 이하)에선
// 개요 화면의 요약 카드 바로 아래로 옮긴다.
const wideMq = window.matchMedia("(min-width: 1401px)");
function placeMarket() {
  const market = document.getElementById("section-market");
  if (!market) return;
  if (wideMq.matches) {
    const side = document.querySelector(".side-panel");
    if (market.parentElement !== side) side.insertBefore(market, side.firstChild);
  } else {
    const anchor = document.getElementById("section-overview");
    if (market.previousElementSibling !== anchor) anchor.after(market);
  }
}
placeMarket();
wideMq.addEventListener?.("change", placeMarket);

const alertModal = document.getElementById("alertModal");

function toggleAlertModal(show) {
  alertModal.hidden = show === undefined ? !alertModal.hidden : !show;
}

document.getElementById("bellBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleAlertModal();
});
document.getElementById("alertModalClose").addEventListener("click", () => toggleAlertModal(false));
document.addEventListener("click", (e) => {
  if (!alertModal.hidden && !alertModal.contains(e.target) && e.target.id !== "bellBtn") {
    toggleAlertModal(false);
  }
});

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".bot-tab");
  if (tab && tab.dataset.bot && tab.dataset.bot !== selectedBotId) {
    selectedBotId = tab.dataset.bot;
    if (lastSummary) renderPositions(lastSummary);
  }
});

function renderPositions(summary) {
  const grid = document.getElementById("botGrid");

  // 손익이 4초마다 바뀌어서 setHTMLIfChanged가 매번 innerHTML을 통째로
  // 교체하는데, 그때마다 포지션 표(가로로 넓어서 스크롤해서 보는 표)의
  // 가로 스크롤 위치가 초기화돼 왼쪽으로 튕겨 보였다. 다시 그리기 전에
  // 봇별로 스크롤 위치를 기억해뒀다가 그린 뒤 복원한다.
  const scrollLeftByBot = {};
  grid.querySelectorAll(".pos-table-wrap[data-bot]").forEach((el) => {
    scrollLeftByBot[el.dataset.bot] = el.scrollLeft;
  });

  const html = renderBotCard(summary.bots, selectedBotId);
  setHTMLIfChanged(grid, html);

  grid.querySelectorAll(".pos-table-wrap[data-bot]").forEach((el) => {
    const saved = scrollLeftByBot[el.dataset.bot];
    if (saved) el.scrollLeft = saved;
  });
}

/* ---------------- Main refresh loop ---------------- */

async function refresh() {
  const statusText = document.getElementById("statusText");
  const alertPing = document.getElementById("alertPing");

  // Promise.all은 하나만 실패해도 전부 실패 처리돼서, 예를 들어 시세 API 하나만
  // 잠깐 흔들려도 이미 잘 받아온 봇 데이터까지 화면에 반영을 못 함.
  // allSettled로 바꿔서 성공한 것만이라도 반영하고, 실패한 것만 원인을 구분해서 알려줌.
  const seq = ++refreshSeq;
  const days = equityDays;
  // 로그인 상태 확인도 같이 출발시킨다(예전엔 위 세 요청이 끝난 뒤에 따로 기다렸다)
  const authP = refreshAuthState();
  const [summaryRes, tickersRes, alertsRes] = await Promise.allSettled([
    fetchJSON(`/api/summary?days=${days}`),
    fetchJSON("/api/tickers"),
    fetchJSON("/api/notifications"),
  ]);
  if (summaryRes.status === "fulfilled") summaryByDays[days] = summaryRes.value;
  if (seq < appliedSeq) return; // 이미 더 최근 요청의 결과가 화면에 반영됨
  appliedSeq = seq;

  const failures = [summaryRes, tickersRes, alertsRes].filter((r) => r.status === "rejected");
  // 조회용 API는 이제 로그인 없이도 열리므로, 여기서 401이 나면 서버 설정 문제다.
  const authFailed = failures.some((r) => r.reason?.status === 401);

  await authP;

  if (summaryRes.status === "fulfilled") {
    const summary = summaryRes.value;
    lastSummary = summary;
    renderMode(summary);
    renderStats(summary);
    // 요청 도중 기간 버튼이 바뀌었으면 그래프는 지금 선택된 기간의 데이터로 그린다
    renderEquityChart(days === equityDays ? summary : summaryByDays[equityDays] || summary);
    renderDonut(summary);
    renderPositions(summary);
    renderHistory(summary);
  }
  if (tickersRes.status === "fulfilled") renderTickers(tickersRes.value);
  if (alertsRes.status === "fulfilled") {
    checkNewAlertsAndPlaySound(alertsRes.value);
    renderAlerts(alertsRes.value);
  }

  if (authFailed) {
    // fetch()는 401을 받아도 브라우저의 로그인 팝업을 다시 띄워주지 않음(페이지 첫 로드 때만 뜸).
    // 그래서 인증이 풀리면 새로고침(재접속)해야만 다시 로그인 창이 뜸 -> 그렇게 안내.
    statusText.textContent = "로그인이 풀렸습니다 — 새로고침 해주세요";
    alertPing.className = "ping show";
  } else if (failures.length > 0) {
    statusText.textContent = `일부 데이터 로드 실패 (${failures.length}건)`;
    alertPing.className = "ping show";
  } else if (lastSummary) {
    const allConnected = lastSummary.bots.every((b) => b.connected);
    statusText.textContent = allConnected ? "모든 봇 연결됨" : "일부 봇 연결 끊김";
    alertPing.className = allConnected ? "ping" : "ping show";
  }

  document.getElementById("lastUpdate").textContent =
    `마지막 업데이트: ${new Date().toLocaleTimeString("ko-KR")}`;
}

refresh().then(prefetchOtherPeriods);
setInterval(refresh, REFRESH_MS);

/* ---------------- 접이식 패널 ----------------
   미체결 주문의 그룹 펼치기(캐럿 클릭)와 같은 방식이다. 페이지가 세로로 너무
   길어져서, 자주 안 보는 패널(진입 조건 현황·전체 청산 이력)은 접어둘 수
   있게 했다. 접힘 상태는 이 브라우저에만 저장한다 - 다른 사람이나 다른
   기기의 화면에는 영향이 없다(뷰어별 편의일 뿐, 공유 상태가 아니므로). */
function loadCollapsedPanels() {
  try {
    return new Set(JSON.parse(localStorage.getItem("collapsedPanels") || "[]"));
  } catch {
    return new Set(); // 프라이빗 모드 등에서 접근이 막혀도 기본값(펼침)으로 동작
  }
}
function saveCollapsedPanels(set) {
  try {
    localStorage.setItem("collapsedPanels", JSON.stringify([...set]));
  } catch {
    /* 저장 실패해도 화면 동작에는 지장 없다 - 다음 새로고침에서 펼침으로 돌아갈 뿐 */
  }
}

function initCollapsiblePanels() {
  const collapsed = loadCollapsedPanels();
  document.querySelectorAll(".panel-head-collapsible").forEach((head) => {
    const targetId = head.dataset.collapseTarget;
    const target = document.getElementById(targetId);
    const caret = head.querySelector(".panel-caret");
    if (!target) return;

    const apply = (isCollapsed) => {
      target.hidden = isCollapsed;
      if (caret) caret.textContent = isCollapsed ? "▸" : "▾";
      head.classList.toggle("is-collapsed", isCollapsed);
    };
    apply(collapsed.has(targetId));

    head.addEventListener("click", () => {
      const willCollapse = !target.hidden;
      apply(willCollapse);
      const set = loadCollapsedPanels();
      if (willCollapse) set.add(targetId);
      else set.delete(targetId);
      saveCollapsedPanels(set);
    });
  });
}
initCollapsiblePanels();

// 라이트/다크 전환 시 그래프를 새 테마 색으로 다시 만든다 (theme.js)
onThemeChange(() => {
  if (equityChart) equityChart.destroy();
  if (dailyChart) dailyChart.destroy();
  equityChart = null;
  dailyChart = null;
  const s = summaryByDays[equityDays] || lastSummary;
  if (s) renderEquityChart(s);
  if (lastSummary) renderDonut(lastSummary);
});
