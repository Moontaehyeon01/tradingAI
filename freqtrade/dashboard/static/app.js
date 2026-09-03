const REFRESH_MS = 4000;

const COIN_COLORS = {
  BTC: "#f59e0b",
  ETH: "#8b5cf6",
  BNB: "#f0b90b",
  SOL: "#14f195",
};

let equityChart = null;
let sideDonut = null;
let lastSummary = null;
let lastSeenAlertTime = null; // 새로 도착한 알림만 구분해서 소리 재생하기 위한 기준점
let audioCtx = null;

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
      const color = COIN_COLORS[symbol] || "#5b8def";
      const cls = pnlClass(t.change_pct);
      return `
        <tr>
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

  const openCount = connectedBots.reduce((sum, b) => sum + b.open_trades.length, 0);
  document.getElementById("statOpenCount").textContent = openCount;

  const totalTrades = connectedBots.reduce((sum, b) => sum + (b.trade_count || 0), 0);
  const avgWinrate =
    connectedBots.length > 0
      ? connectedBots.reduce((sum, b) => sum + (b.winrate || 0), 0) / connectedBots.length
      : 0;
  document.getElementById("statWinrate").textContent = `${(avgWinrate * 100).toFixed(1)}%`;
  document.getElementById("statTradeCount").textContent = `${totalTrades} 거래`;
}

/* ---------------- Equity chart ---------------- */

// 봇별 그래프 색상. 봇이 추가/교체돼도 순서대로 배정된다.
const BOT_COLORS = ["#5b8def", "#f59e0b", "#8b5cf6", "#10b981", "#f43f5e"];

function hexToRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function renderEquityChart(summary) {
  const ctx = document.getElementById("equityChart");

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

  // 봇 목록을 그대로 따라가도록 동적으로 구성한다.
  // (예전엔 bots[0]="추세추종", bots[1]="평균회귀"로 하드코딩돼 있어서,
  //  전략을 교체하면 새 전략이 옛 이름으로 표시되고 없는 봇은 빈 선으로 남았다)
  const series = summary.bots
    .filter((b) => b.connected)
    .map((b, i) => ({
      label: b.name.replace(/\s*\(.*\)\s*/, "") || b.id,
      color: BOT_COLORS[i % BOT_COLORS.length],
      daily: b.daily || [],
    }));

  const longest = series.reduce(
    (best, s) => (s.daily.length > best.length ? s.daily : best),
    []
  );
  const labels = longest.map((d) => d.date.slice(5));

  // 범례도 실제 봇에 맞춰 다시 그림
  const legendEl = document.getElementById("equityLegend");
  if (legendEl) {
    legendEl.innerHTML = series
      .map(
        (s) =>
          `<span class="legend-item"><i class="dotcolor" style="background:${s.color}"></i>${s.label}</span>`
      )
      .join("");
  }

  const datasets = series.map((s) => {
    const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 240);
    grad.addColorStop(0, hexToRgba(s.color, 0.33));
    grad.addColorStop(1, hexToRgba(s.color, 0));
    return {
      label: s.label,
      data: cumulative(s.daily),
      pctData: cumulativePct(s.daily),
      borderColor: s.color,
      backgroundColor: grad,
      fill: true,
      tension: 0.4,
      pointRadius: 0,
      pointHoverRadius: 4,
      borderWidth: 2,
    };
  });

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
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1b1e27",
          borderColor: "#262a35",
          borderWidth: 1,
          titleColor: "#8b90a0",
          bodyColor: "#eef0f4",
          bodyFont: { family: "JetBrains Mono" },
          padding: 10,
          callbacks: {
            // 그래프 선 자체는 금액만 그리지만, 마우스를 올리면(또는 탭하면)
            // 그 시점의 수익률(%)과 수익금($)을 한 줄에 같이 보여줌
            label: (context) => {
              const ds = context.dataset;
              const amt = context.parsed.y;
              const pct = ds.pctData ? ds.pctData[context.dataIndex] : null;
              const amtStr = `${amt >= 0 ? "+" : ""}${amt.toFixed(2)} USDT`;
              const pctStr = pct === null ? "" : ` (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
              return `${ds.label}: ${amtStr}${pctStr}`;
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#565b6b", font: { size: 10 } } },
        y: {
          grid: { color: "#1e222c" },
          ticks: { color: "#565b6b", font: { size: 10, family: "JetBrains Mono" } },
        },
      },
    },
  });
}

/* ---------------- Side donut ---------------- */

function renderDonut(summary) {
  const ctx = document.getElementById("sideDonut");
  let longs = 0;
  let shorts = 0;
  summary.bots.forEach((b) => {
    if (!b.connected) return;
    b.open_trades.forEach((t) => (t.is_short ? shorts++ : longs++));
  });

  const total = longs + shorts;
  const legend = document.getElementById("donutLegend");
  setHTMLIfChanged(
    legend,
    `
    <div class="donut-legend-item">
      <span class="donut-legend-left"><i class="dotcolor" style="background:#10b981"></i>롱</span>
      <span class="donut-legend-val">${longs}</span>
    </div>
    <div class="donut-legend-item">
      <span class="donut-legend-left"><i class="dotcolor" style="background:#f43f5e"></i>숏</span>
      <span class="donut-legend-val">${shorts}</span>
    </div>`
  );

  const data = total === 0 ? [1] : [longs, shorts];
  const colors = total === 0 ? ["#262a35"] : ["#10b981", "#f43f5e"];

  if (sideDonut) {
    sideDonut.data.datasets[0].data = data;
    sideDonut.data.datasets[0].backgroundColor = colors;
    sideDonut.update("none");
    return;
  }

  sideDonut = new Chart(ctx, {
    type: "doughnut",
    data: { datasets: [{ data, backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "72%",
      plugins: { legend: { display: false }, tooltip: { enabled: total > 0 } },
    },
  });
}

/* ---------------- Position cards ---------------- */

function renderOpenTrades(trades, filter) {
  const filtered = filter
    ? trades.filter((t) => t.pair.toLowerCase().includes(filter))
    : trades;
  if (!filtered.length) {
    return `<div class="empty-row">${filter ? "일치하는 포지션 없음" : "보유 포지션 없음"}</div>`;
  }
  const rows = filtered
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
      const remTxt =
        rem === null || rem === undefined
          ? "–"
          : rem >= 1
          ? `${Math.floor(rem)}시간`
          : `${Math.round(rem * 60)}분`;
      const remCls = rem !== null && rem !== undefined && rem < 6 ? "warn" : "";
      return `
        <tr>
          <td class="pair-cell">${t.pair}</td>
          <td><span class="side-pill ${sideClass}">${sideLabel}</span></td>
          <td>${t.leverage}x</td>
          <td>${fmtNum(t.open_rate)}</td>
          <td>${fmtNum(t.current_rate)}</td>
          <td class="lvl neg">${fmtNum(sl)}${slD === null ? "" : `<span class="lvl-d">${slD > 0 ? "+" : ""}${slD.toFixed(1)}%</span>`}</td>
          <td class="lvl pos">${fmtNum(tp)}${tpD === null ? "" : `<span class="lvl-d">${tpD > 0 ? "+" : ""}${tpD.toFixed(1)}%</span>`}</td>
          <td class="${remCls}">${remTxt}</td>
          <td class="${pnlClass(t.profit_pct)}">${fmtPct(t.profit_pct)}</td>
          <td class="${pnlClass(t.profit_abs)}">$${fmtUsd(t.profit_abs)}</td>
        </tr>`;
    })
    .join("");
  return `
    <div class="pos-table-wrap">
    <table class="pos-table">
      <thead>
        <tr><th>페어</th><th>방향</th><th>배율</th><th>진입가</th><th>현재가</th>
            <th>손절</th><th>익절</th><th>청산까지</th><th>손익%</th><th>손익$</th></tr>
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
      window.alert(`정지했습니다.\n\n주의: ${data.warning}`);
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

function renderBotCard(bot, filter) {
  if (!bot.connected) {
    return `
      <div class="bot-card">
        <div class="bot-card-head">
          <div class="bot-title"><span class="dot err"></span>${bot.name}</div>
        </div>
        <div class="card-error">봇에 연결할 수 없습니다${bot.error ? ` (${bot.error})` : ""}</div>
      </div>`;
  }

  return `
    <div class="bot-card">
      <div class="bot-card-head">
        <div class="bot-title"><span class="dot ok"></span>${bot.name}</div>
        <div class="badge-row">
          <span class="lev-badge">${bot.leverage}x</span>
          ${renderBotPower(bot)}
        </div>
      </div>
      <div class="mini-stats">
        <div class="mini-stat"><div class="l">잔고</div><div class="v">$${bot.balance_total.toFixed(2)}</div></div>
        <div class="mini-stat"><div class="l">총손익</div><div class="v ${pnlClass(bot.profit_all_abs)}">${fmtPct(bot.profit_all_pct)}<span class="v-sub">${fmtUsd(bot.profit_all_abs)} USDT</span></div></div>
        <div class="mini-stat"><div class="l">승률</div><div class="v">${(bot.winrate * 100).toFixed(1)}%</div></div>
        <div class="mini-stat"><div class="l">MDD</div><div class="v neg">${(bot.max_drawdown * 100).toFixed(2)}%</div></div>
      </div>
      ${renderOpenTrades(bot.open_trades, filter)}
    </div>`;
}

/* ---------------- History table ---------------- */

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
  if (!rows.length) {
    setHTMLIfChanged(tbody, `<tr><td colspan="7" class="empty-row">청산된 거래가 아직 없습니다</td></tr>`);
    return;
  }
  const html = rows
    .slice(0, 15)
    .map((t) => {
      // 수동 청산은 체결 기록에서 방향을 못 찾으면 is_short 가 null 로 온다 -
      // 이때 LONG으로 단정하면 틀릴 수 있으므로 "모름" 배지를 따로 둔다.
      let sidePill;
      if (t.is_short === true) sidePill = `<span class="side-pill side-short">SHORT</span>`;
      else if (t.is_short === false) sidePill = `<span class="side-pill side-long">LONG</span>`;
      else sidePill = `<span class="neutral">–</span>`;
      const strategyCls = t.manual ? "ord-owner manual" : "neutral";
      return `
        <tr>
          <td><span class="${strategyCls}">${t.strategy}</span></td>
          <td class="pair-cell">${t.pair}</td>
          <td>${sidePill}</td>
          <td class="${pnlClass(t.close_profit_pct)}">${fmtPct(t.close_profit_pct)}</td>
          <td class="${pnlClass(t.close_profit_abs)}">${fmtUsd(t.close_profit_abs)}</td>
          <td class="neutral">${t.exit_reason_ko ?? t.exit_reason ?? "–"}</td>
          <td class="neutral">${timeAgo(t.close_date)}</td>
        </tr>`;
    })
    .join("");
  setHTMLIfChanged(tbody, html);
}

/* ---------------- Alerts feed ---------------- */

// alerts는 최신순 정렬(맨 앞이 가장 최근). 마지막으로 확인한 시각보다 새로운
// 알림이 있으면 진입/청산에 맞는 소리를 재생함. 첫 로드 때는(과거 알림 다 재생 방지)
// 소리 없이 기준점만 세팅.
function checkNewAlertsAndPlaySound(alerts) {
  if (!alerts.length) return;

  if (lastSeenAlertTime === null) {
    lastSeenAlertTime = alerts[0].time;
    return;
  }

  const newOnes = alerts.filter((a) => a.time > lastSeenAlertTime);
  if (newOnes.length === 0) return;

  // 오래된 것부터 순서대로 소리 재생
  newOnes
    .slice()
    .reverse()
    .forEach((a) => {
      if (a.event === "entry_fill") playEntrySound();
      else if (a.event === "exit_fill") playExitSound((a.profit_ratio_pct ?? 0) >= 0);
    });

  lastSeenAlertTime = alerts[0].time;
}

function renderAlerts(alerts) {
  const el = document.getElementById("alertsList");
  if (!alerts.length) {
    setHTMLIfChanged(el, `<div class="empty-row">아직 알림이 없습니다</div>`);
    return;
  }
  const html = alerts
    .map((a) => {
      if (a.event === "entry_fill") {
        return `
          <div class="alert-item">
            <div class="alert-icon entry">${a.is_short ? "S" : "L"}</div>
            <div class="alert-body">
              <div class="alert-title">
                <span class="alert-bot-tag">${a.bot_name}</span>
                진입 체결 · ${a.pair} · <span class="${a.is_short ? "neg" : "pos"}">${a.side_ko}</span>
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
                청산 완료 · ${a.pair} · <span class="${cls}">${fmtPct(a.profit_ratio_pct)}</span>
              </div>
              <div class="alert-sub">${fmtUsd(a.profit_amount)} ${a.stake_currency} · ${a.exit_reason_ko}</div>
            </div>
            <div class="alert-time">${timeAgo(a.time.replace("T", " ").slice(0, 19))}</div>
          </div>`;
      }
      return "";
    })
    .join("");
  setHTMLIfChanged(el, html);
}

/* ---------------- Nav smooth scroll ---------------- */

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((i) => i.classList.remove("active"));
    // 사이드바와 모바일 하단바에 같은 항목이 하나씩 있으므로 둘 다 활성 표시
    document
      .querySelectorAll(`.nav-item[data-target="${item.dataset.target}"]`)
      .forEach((i) => i.classList.add("active"));
    const target = document.getElementById(item.dataset.target);
    if (!target) return;
    target.scrollIntoView({ behavior: "auto", block: "start" });
    // 클릭한 섹션이 잠깐 반짝여서 "여기로 이동했다"는 게 눈에 보이게 함
    target.classList.remove("nav-flash");
    // reflow를 강제해서 같은 섹션을 연달아 클릭해도 애니메이션이 다시 재생되게 함
    void target.offsetWidth;
    target.classList.add("nav-flash");
    target.addEventListener("animationend", () => target.classList.remove("nav-flash"), { once: true });
  });
});

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

document.getElementById("pairFilter").addEventListener("input", (e) => {
  if (lastSummary) renderPositions(lastSummary, e.target.value.trim().toLowerCase());
});

function renderPositions(summary, filter = "") {
  const html = summary.bots.map((b) => renderBotCard(b, filter)).join("");
  setHTMLIfChanged(document.getElementById("botGrid"), html);
}

/* ---------------- Main refresh loop ---------------- */

async function refresh() {
  const statusText = document.getElementById("statusText");
  const alertPing = document.getElementById("alertPing");

  // Promise.all은 하나만 실패해도 전부 실패 처리돼서, 예를 들어 시세 API 하나만
  // 잠깐 흔들려도 이미 잘 받아온 봇 데이터까지 화면에 반영을 못 함.
  // allSettled로 바꿔서 성공한 것만이라도 반영하고, 실패한 것만 원인을 구분해서 알려줌.
  const [summaryRes, tickersRes, alertsRes] = await Promise.allSettled([
    fetchJSON("/api/summary"),
    fetchJSON("/api/tickers"),
    fetchJSON("/api/notifications"),
  ]);

  const failures = [summaryRes, tickersRes, alertsRes].filter((r) => r.status === "rejected");
  // 조회용 API는 이제 로그인 없이도 열리므로, 여기서 401이 나면 서버 설정 문제다.
  const authFailed = failures.some((r) => r.reason?.status === 401);

  await refreshAuthState();

  if (summaryRes.status === "fulfilled") {
    const summary = summaryRes.value;
    lastSummary = summary;
    renderMode(summary);
    renderStats(summary);
    renderEquityChart(summary);
    renderDonut(summary);
    renderPositions(summary, document.getElementById("pairFilter").value.trim().toLowerCase());
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

refresh();
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
