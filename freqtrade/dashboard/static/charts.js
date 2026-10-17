/* ==========================================================================
   오른쪽 차트 (TradingView 무료 위젯)

   기본은 BTC / QQQ 두 개. 보유 종목(봇 포지션·수동 포지션) 줄을 누르면 그
   코인 차트로 바뀌고, 차트 위 버튼으로 BTC·QQQ로 돌아간다(2026-10-06).
   오른쪽 패널이 숨겨지는 좁은 화면에선 같은 패널을 전체 화면으로 띄운다.
   ========================================================================== */

const TV_COMMON = {
  autosize: true,
  interval: "1",
  timezone: "Asia/Seoul",
  theme: "dark",
  style: "1",
  locale: "kr",
  toolbar_bg: "#14161d",
  hide_top_toolbar: true,
  hide_legend: false,
  save_image: false,
  backgroundColor: "#14161d",
  gridColor: "rgba(255,255,255,0.06)",
};

function tvWidget(symbol, containerId, extra = {}) {
  if (!window.TradingView) return;
  new TradingView.widget({ ...TV_COMMON, symbol, container_id: containerId, ...extra });
}

tvWidget("BINANCE:BTCUSDT.P", "tvChartBtc");
// 바이낸스 QQQ 무기한선물은 TradingView 표기로 끝에 ".P" 를 붙여야 한다
tvWidget("BINANCE:QQQUSDT.P", "tvChartQqq");

let coinChartBase = null;

// 종목 차트는 TradingView 가 자기 데이터를 받아 그리는 데 1~2초가 걸린다. 한 번 띄운
// 종목은 지우지 않고 숨겨 두었다가 다시 누르면 바로 보여주고, 마우스를 줄에 올리는
// 순간 미리 만들기 시작해 누를 때쯤이면 거의 다 그려져 있게 한다(2026-10-17).
// 위젯마다 메모리를 꽤 쓰므로 최근 COIN_CACHE_MAX 개만 남긴다.
const COIN_CACHE_MAX = 4;
const coinSlots = new Map(); // base -> 위젯을 담은 div (Map 순서 = 최근 사용 순)

function coinSlot(base) {
  const box = document.getElementById("tvChartCoin");
  let el = coinSlots.get(base);
  if (el) {
    coinSlots.delete(base);
    coinSlots.set(base, el);
    return el;
  }
  el = document.createElement("div");
  el.className = "tv-coin-slot";
  el.id = `tvCoin_${base}`;
  box.appendChild(el);
  coinSlots.set(base, el);
  // 보유 기간이 며칠 단위라 1시간봉으로 시작하고, 위쪽 도구막대로 봉 간격을 바꿀 수 있게 한다
  tvWidget(`BINANCE:${base}USDT.P`, el.id, { interval: "60", hide_top_toolbar: false });
  for (const [b, slot] of coinSlots) {
    if (coinSlots.size <= COIN_CACHE_MAX) break;
    if (b === base || b === coinChartBase) continue;
    slot.remove();
    coinSlots.delete(b);
  }
  return el;
}

function cleanBase(rawBase) {
  return String(rawBase || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

// 선택된 종목 줄 강조. 포지션 표는 4초마다 통째로 다시 그려져서 줄에 class 를
// 붙여도 사라진다 - 대신 스타일 규칙 하나를 바꿔 끼운다.
function markSelectedChartRows(base) {
  let st = document.getElementById("chartSelStyle");
  if (!st) {
    st = document.createElement("style");
    st.id = "chartSelStyle";
    document.head.appendChild(st);
  }
  st.textContent = base
    ? `tr[data-chart-base="${base}"] td { background: rgba(91, 141, 239, 0.16) !important; }`
    : "";
}

function showCoinChart(rawBase) {
  const base = cleanBase(rawBase);
  if (!base) return;
  document.getElementById("defaultCharts").hidden = true;
  document.getElementById("coinChartCard").hidden = false;
  document.getElementById("coinChartTitle").textContent = `${base}/USDT · 무기한선물`;
  document.body.classList.add("coin-chart-mode");
  const active = coinSlot(base);
  for (const slot of coinSlots.values()) slot.classList.toggle("active", slot === active);
  coinChartBase = base;
  markSelectedChartRows(base);
}

function restoreDefaultCharts() {
  document.getElementById("coinChartCard").hidden = true;
  document.getElementById("defaultCharts").hidden = false;
  document.body.classList.remove("coin-chart-mode");
  coinChartBase = null; // 위젯은 지우지 않고 숨겨 둔다(다시 누르면 바로 보이게)
  markSelectedChartRows(null);
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#coinChartBack")) {
    restoreDefaultCharts();
    return;
  }
  const row = e.target.closest("tr[data-chart-base]");
  if (row) showCoinChart(row.dataset.chartBase);
});

// 줄에 마우스를 잠깐 올리면 미리 만들어 둔다
let preloadTimer = null;
document.addEventListener("mouseover", (e) => {
  const row = e.target.closest("tr[data-chart-base]");
  clearTimeout(preloadTimer);
  if (!row) return;
  const base = cleanBase(row.dataset.chartBase);
  if (!base || coinSlots.has(base)) return;
  preloadTimer = setTimeout(() => coinSlot(base), 250); // 훑고 지나가기만 하면 안 만든다
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && coinChartBase) restoreDefaultCharts();
});
