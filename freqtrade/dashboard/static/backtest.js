/* ==========================================================================
   백테스트 페이지

   조회(GET)는 로그인 없이 되지만 실행(POST)은 로그인이 필요하다.
   app.js 의 isAuthed / goLogin 을 그대로 재사용한다.
   ========================================================================== */

let btMeta = null;
let btPollTimer = null;

const $bt = (id) => document.getElementById(id);

const btPct = (v) =>
  v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
const btCls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

async function btLoadMeta() {
  try {
    const res = await fetch("/api/backtest/meta");
    btMeta = await res.json();
  } catch {
    return;
  }
  const sel = $bt("btStrategy");
  if (sel) {
    sel.innerHTML = (btMeta.strategies || [])
      .map(
        (s) =>
          `<option value="${s}"${s === "BoxBreakoutV2Strategy" ? " selected" : ""}>${s}</option>`
      )
      .join("");
  }
  // 데이터가 있는 페어들의 공통 구간을 기본 기간으로 제안
  if (btMeta.suggested_range) {
    const a = $bt("btStart");
    const b = $bt("btEnd");
    if (a && !a.value) a.value = btMeta.suggested_range.start;
    if (b && !b.value) b.value = btMeta.suggested_range.end;
  }
  btRenderPairs();
}

const BT_MAJOR = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"];

function btRenderPairs() {
  const box = $bt("btPairs");
  if (!box || !btMeta) return;
  if (!btMeta.pairs || !btMeta.pairs.length) {
    box.innerHTML =
      '<div class="empty-row">시세 데이터가 없습니다. 먼저 데이터를 내려받아야 합니다.</div>';
    return;
  }
  box.innerHTML = btMeta.pairs
    .map((p) => {
      const r = btMeta.ranges[p];
      const span = r ? `${r.start.slice(2)}~${r.end.slice(2)}` : "범위 불명";
      const on = BT_MAJOR.includes(p) ? " checked" : "";
      return (
        `<label class="bt-pair"><input type="checkbox" value="${p}"${on}>` +
        `<span class="bt-pair-name">${p.split("/")[0]}</span>` +
        `<span class="bt-pair-range">${span}</span></label>`
      );
    })
    .join("");
  btUpdatePairCount();
}

function btUpdatePairCount() {
  const n = document.querySelectorAll("#btPairs input:checked").length;
  const el = $bt("btPairCount");
  if (el) el.textContent = n;
}

function btSelectedPairs() {
  return [...document.querySelectorAll("#btPairs input:checked")].map((i) => i.value);
}

function btOpts() {
  const pct = (id, d) => (parseFloat(($bt(id) || {}).value) || d) / 100;
  const num = (id, d) => parseFloat(($bt(id) || {}).value) || d;
  const start = (($bt("btStart") || {}).value || "").replace(/-/g, "");
  const end = (($bt("btEnd") || {}).value || "").replace(/-/g, "");
  return {
    strategy: ($bt("btStrategy") || {}).value,
    pairs: btSelectedPairs(),
    timerange: `${start}-${end}`,
    leverage: num("btLeverage", 3),
    max_open_trades: num("btMaxTrades", 5),
    params: {
      buy: {
        box_period: Math.round(num("btBoxPeriod", 24)),
        box_max_width: pct("btBoxWidth", 3),
      },
      sell: {
        max_stop_pct: pct("btMaxStop", 5),
        max_hold_candles: Math.round(num("btMaxHold", 0)),
      },
      roi: { 0: pct("btRoi", 20) },
      stoploss: { stoploss: -0.5 },
    },
  };
}

function btSetBusy(busy, label) {
  const btn = $bt("btRun");
  if (btn) {
    btn.disabled = busy;
    btn.textContent = busy ? label || "실행 중…" : "백테스트 실행";
  }
  const badge = $bt("btEngineState");
  if (badge) {
    badge.textContent = busy ? "실행 중" : "준비";
    badge.className = "state-badge " + (busy ? "on" : "off");
  }
}

function btNote(msg, isError) {
  const el = $bt("btNote");
  if (el) {
    el.textContent = msg || "";
    el.className = "bt-note" + (isError ? " err" : "");
  }
}

async function btRun() {
  if (typeof isAuthed !== "undefined" && !isAuthed) {
    if (window.confirm("백테스트 실행은 로그인이 필요합니다.\n로그인하시겠습니까?")) goLogin();
    return;
  }
  const opts = btOpts();
  if (!opts.pairs.length) {
    btNote("페어를 하나 이상 선택하세요.", true);
    return;
  }
  if (opts.timerange.length !== 17) {
    btNote("시작일과 종료일을 지정하세요.", true);
    return;
  }

  btSetBusy(true);
  btNote("");
  const box = $bt("btResult");
  if (box) box.hidden = true;

  try {
    const res = await fetch("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    });
    const data = await res.json();
    if (res.status === 401) {
      isAuthed = false;
      btSetBusy(false);
      if (window.confirm("로그인이 필요합니다.\n로그인하시겠습니까?")) goLogin();
      return;
    }
    if (!data.ok) {
      btSetBusy(false);
      btNote(data.error || "실행에 실패했습니다.", true);
      return;
    }
    btPoll(data.id);
  } catch (err) {
    btSetBusy(false);
    btNote(String(err), true);
  }
}

function btPoll(id) {
  clearInterval(btPollTimer);
  btPollTimer = setInterval(async () => {
    let job;
    try {
      const res = await fetch(`/api/backtest/status?id=${id}`);
      job = await res.json();
    } catch {
      return;
    }
    if (!job) return;
    if (job.status === "running") {
      btSetBusy(true, `실행 중… ${Math.round(job.elapsed || 0)}초`);
      return;
    }
    clearInterval(btPollTimer);
    btSetBusy(false);
    if (job.status === "error") {
      btNote(job.error || "실패", true);
      const box = $bt("btResult");
      if (box) box.hidden = true;
    } else {
      btNote(`완료 · ${job.elapsed}초 소요`);
      btRenderResult(job);
    }
    btLoadHistory();
  }, 2000);
}

function btTable(title, headers, rows) {
  let h = `<div class="bt-subtitle">${title}</div>`;
  h += '<div class="table-scroll"><table class="history-table"><thead><tr>';
  h += headers.map((x) => `<th>${x}</th>`).join("");
  h += "</tr></thead><tbody>";
  h += rows.join("");
  h += "</tbody></table></div>";
  return h;
}

function btRenderResult(job) {
  const r = job.result || {};
  const el = $bt("btResult");
  if (!el) return;

  const cards = [
    ["총 수익률", btPct(r.profit_pct), btCls(r.profit_pct)],
    ["최대 낙폭", r.max_drawdown != null ? `${r.max_drawdown.toFixed(2)}%` : "–", "neg"],
    ["CAGR", btPct(r.cagr), btCls(r.cagr)],
    ["거래 수", r.trades == null ? "–" : r.trades, ""],
    ["승률", r.winrate != null ? `${r.winrate}%` : "–", ""],
    ["Profit Factor", r.profit_factor == null ? "–" : r.profit_factor, ""],
  ];
  let html =
    '<div class="bt-cards">' +
    cards
      .map(
        ([k, v, c]) =>
          `<div class="bt-card"><div class="l">${k}</div><div class="v ${c}">${v}</div></div>`
      )
      .join("") +
    "</div>";

  if (r.period) {
    html +=
      `<div class="bt-meta">기간 ${r.period} · 레버리지 ${job.opts.leverage}배 · ` +
      `페어 ${job.opts.pairs.length}개 · 익절 ${(job.opts.params.roi["0"] * 100).toFixed(0)}%</div>`;
  }
  if (job.warning) html += `<div class="bt-warn">${job.warning}</div>`;

  if (r.exits && r.exits.length) {
    html += btTable(
      "청산 사유",
      ["사유", "건수", "평균 손익%", "합계%", "승률"],
      r.exits.map(
        (e) =>
          `<tr><td>${e.reason}</td><td>${e.count}</td>` +
          `<td class="${btCls(e.avg_pct)}">${btPct(e.avg_pct)}</td>` +
          `<td class="${btCls(e.total_pct)}">${btPct(e.total_pct)}</td>` +
          `<td>${e.winrate}%</td></tr>`
      )
    );
  }

  if (r.pairs && r.pairs.length) {
    html += btTable(
      "페어별",
      ["페어", "거래", "평균 손익%", "합계%", "승률"],
      r.pairs.map(
        (p) =>
          `<tr><td>${p.pair.split("/")[0]}</td><td>${p.trades}</td>` +
          `<td class="${btCls(p.avg_pct)}">${btPct(p.avg_pct)}</td>` +
          `<td class="${btCls(p.total_pct)}">${btPct(p.total_pct)}</td>` +
          `<td>${p.winrate}%</td></tr>`
      )
    );
  }

  el.innerHTML = html;
  el.hidden = false;
}

async function btLoadHistory() {
  let data;
  try {
    const res = await fetch("/api/backtest/status");
    data = await res.json();
  } catch {
    return;
  }
  const done = (data.history || []).filter((j) => j.status === "done");
  const wrap = $bt("btHistoryWrap");
  const tb = document.querySelector("#btHistory tbody");
  if (!wrap || !tb) return;
  if (!done.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  tb.innerHTML = done
    .map((j) => {
      const r = j.result || {};
      return (
        `<tr><td>${j.opts.strategy.replace("Strategy", "")}</td>` +
        `<td>${r.period || j.opts.timerange}</td><td>${j.opts.leverage}x</td>` +
        `<td class="${btCls(r.profit_pct)}">${btPct(r.profit_pct)}</td>` +
        `<td class="neg">${r.max_drawdown != null ? r.max_drawdown.toFixed(1) + "%" : "–"}</td>` +
        `<td>${r.trades == null ? "–" : r.trades}</td>` +
        `<td>${r.winrate != null ? r.winrate + "%" : "–"}</td></tr>`
      );
    })
    .join("");
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#btRun")) btRun();
  if (e.target.closest("#btPairAll")) {
    document.querySelectorAll("#btPairs input").forEach((i) => (i.checked = true));
    btUpdatePairCount();
  }
  if (e.target.closest("#btPairNone")) {
    document.querySelectorAll("#btPairs input").forEach((i) => (i.checked = false));
    btUpdatePairCount();
  }
  if (e.target.closest("#btPairMajor")) {
    document
      .querySelectorAll("#btPairs input")
      .forEach((i) => (i.checked = BT_MAJOR.includes(i.value)));
    btUpdatePairCount();
  }
});

document.addEventListener("change", (e) => {
  if (e.target.closest("#btPairs")) btUpdatePairCount();
});

btLoadMeta();
btLoadHistory();
