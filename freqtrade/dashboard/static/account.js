/* ==========================================================================
   수동 포지션 · 미체결 주문

   freqtrade는 "자기가 낸 주문"만 안다. 그래서 사용자가 바이낸스 앱이나 웹에서
   직접 잡은 포지션, 직접 걸어둔 지정가 주문은 봇 카드에 전혀 나오지 않았다.
   서버의 /api/account 가 거래소 계좌를 직접 조회해서 그 부분을 채워준다.

   /api/account 는 조회라도 로그인이 필요하다. 아직 체결되지 않은 지정가 주문의
   가격과 수량이 그대로 드러나기 때문에, 다른 조회 API와 달리 열람 자체를 막아뒀다.
   로그인 전에는 두 패널 모두 잠금 안내만 보여준다.

   미체결 주문은 그리드 매매처럼 같은 종목에 수십 건이 한 번에 걸리는 경우가 많아서
   (종목 + 방향 + 주문종류)로 묶어서 한 줄로 보여주고, 클릭하면 펼쳐지게 했다.
   50건을 그대로 나열하면 화면을 다 잡아먹는다.
   ========================================================================== */

const ACCOUNT_REFRESH_MS = 10000;

// 펼쳐 놓은 주문 그룹. setHTMLIfChanged 로 표를 통째로 다시 그리기 때문에
// 펼침 상태를 DOM 밖에 따로 들고 있어야 갱신될 때마다 접히지 않는다.
const expandedOrderGroups = new Set();

// 마지막으로 받아온 계좌 상태. 펼치기 클릭 시 서버를 다시 부르지 않고 바로 다시 그린다.
let lastAccount = null;

function acctNum(v, digits) {
  if (v === null || v === undefined) return "–";
  const d = digits !== undefined ? digits : v >= 100 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString(undefined, { maximumFractionDigits: d });
}

// 표가 비어 있을 때는 min-width를 풀어야 한다. 안 그러면 "포지션 없습니다" 같은
// 안내 문구가 좁은 화면에서 표 밖(가로 스크롤 영역)으로 밀려나 안 보인다.
function setTableEmpty(tableId, empty) {
  document.getElementById(tableId)?.classList.toggle("is-empty", empty);
}

function elapsedFrom(ms) {
  if (!ms) return "–";
  const sec = Math.floor((Date.now() - ms) / 1000);
  if (sec < 60) return `${sec}초`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간`;
  return `${Math.floor(sec / 86400)}일`;
}

/* ---------------- 수동 포지션 ---------------- */

function renderManualPositions(acct) {
  const body = document.getElementById("manualBody");
  const note = document.getElementById("manualNote");
  if (!body) return;

  if (!acct.ok) {
    setHTMLIfChanged(body,
      `<tr><td colspan="10" class="empty-row">계좌 조회 실패 — 거래소 API 키를 확인해주세요</td></tr>`);
    setTableEmpty("manualTable", true);
    if (note) note.textContent = "";
    return;
  }

  // 봇이 관리하는 포지션은 위 봇 카드에 이미 나오므로 여기서는 제외한다.
  const manual = (acct.positions || []).filter((p) => !p.managed);

  if (!manual.length) {
    const botCount = (acct.positions || []).length;
    setHTMLIfChanged(body,
      `<tr><td colspan="10" class="empty-row">직접 잡은 포지션이 없습니다</td></tr>`);
    setTableEmpty("manualTable", true);
    if (note) {
      note.textContent = botCount
        ? `계좌의 포지션 ${botCount}건은 모두 봇이 관리 중입니다`
        : "바이낸스에서 직접 잡은 포지션이 여기 표시됩니다";
    }
    return;
  }

  const html = manual
    .map((p) => {
      const sideCls = p.side === "short" ? "side-short" : "side-long";
      const sideTxt = p.side === "short" ? "SHORT" : "LONG";
      return `
        <tr>
          <td class="pair-cell">${p.base}</td>
          <td><span class="side-pill ${sideCls}">${sideTxt}</span></td>
          <td>${p.leverage}x</td>
          <td>${acctNum(p.amount)}</td>
          <td>${acctNum(p.entry_price)}</td>
          <td>${acctNum(p.mark_price)}</td>
          <td class="lvl neg">${acctNum(p.liquidation_price)}</td>
          <td>$${acctNum(p.margin, 2)}</td>
          <td class="${pnlClass(p.pnl_pct)}">${fmtPct(p.pnl_pct)}</td>
          <td class="${pnlClass(p.pnl)}">$${fmtUsd(p.pnl)}</td>
        </tr>`;
    })
    .join("");
  setHTMLIfChanged(body, html);
  setTableEmpty("manualTable", false);

  if (note) {
    const sum = manual.reduce((a, p) => a + p.pnl, 0);
    const margin = manual.reduce((a, p) => a + p.margin, 0);
    note.textContent =
      `직접 잡은 포지션 ${manual.length}건 · 증거금 $${margin.toFixed(2)} · ` +
      `평가손익 ${sum >= 0 ? "+" : ""}$${sum.toFixed(2)} (봇이 관리하지 않음)`;
  }
}

/* ---------------- 미체결 주문 ---------------- */

function groupOrders(orders) {
  const map = new Map();
  for (const o of orders) {
    const key = `${o.symbol}|${o.side}|${o.type}|${o.reduce_only ? 1 : 0}|${o.manual ? 1 : 0}`;
    if (!map.has(key)) map.set(key, { key, items: [], sample: o });
    map.get(key).items.push(o);
  }
  // 건수가 많은 그룹을 위로 (그리드 주문처럼 눈에 띄어야 하는 것부터)
  return [...map.values()].sort((a, b) => b.items.length - a.items.length);
}

function orderRow(o, isSub) {
  const buy = o.side === "BUY";
  const sideTxt = buy ? "매수" : "매도";
  const sideCls = buy ? "pos" : "neg";
  const priceTxt = o.stop_price
    ? `트리거 ${acctNum(o.stop_price)}`
    : acctNum(o.price);
  const remain = o.qty - o.filled;
  const filledTxt =
    o.filled > 0 ? `${acctNum(o.filled)} / ${acctNum(o.qty)}` : "0";
  return `
    <tr class="${isSub ? "ord-sub" : ""}">
      <td class="pair-cell">${isSub ? "" : o.base}</td>
      <td><span class="${sideCls}">${sideTxt}</span> · ${o.type_ko}${
        o.reduce_only ? ' <span class="ord-tag">청산전용</span>' : ""
      }</td>
      <td>${priceTxt}</td>
      <td>${acctNum(remain)}</td>
      <td>${filledTxt}</td>
      <td>${elapsedFrom(o.time)}</td>
      <td><span class="ord-owner ${o.manual ? "manual" : ""}">${o.owner}</span></td>
    </tr>`;
}

function groupRow(g) {
  const o = g.sample;
  const n = g.items.length;
  const open = expandedOrderGroups.has(g.key);
  const buy = o.side === "BUY";
  const prices = g.items.map((x) => x.stop_price || x.price).filter(Boolean);
  const lo = Math.min(...prices);
  const hi = Math.max(...prices);
  const qty = g.items.reduce((a, x) => a + (x.qty - x.filled), 0);
  const oldest = Math.min(...g.items.map((x) => x.time).filter(Boolean));
  return `
    <tr class="ord-group" data-key="${g.key}">
      <td class="pair-cell">
        <span class="ord-caret">${open ? "▾" : "▸"}</span> ${o.base}
      </td>
      <td><span class="${buy ? "pos" : "neg"}">${buy ? "매수" : "매도"}</span> · ${o.type_ko}
        <span class="ord-count">${n}건</span></td>
      <td>${lo === hi ? acctNum(lo) : `${acctNum(lo)} ~ ${acctNum(hi)}`}</td>
      <td>${acctNum(qty)}</td>
      <td>–</td>
      <td>${elapsedFrom(oldest)}</td>
      <td><span class="ord-owner ${o.manual ? "manual" : ""}">${o.owner}</span></td>
    </tr>`;
}

function renderOpenOrders(acct) {
  const body = document.getElementById("orderBody");
  const note = document.getElementById("orderNote");
  if (!body) return;

  if (!acct.ok) {
    setHTMLIfChanged(body,
      `<tr><td colspan="7" class="empty-row">계좌 조회 실패 — 거래소 API 키를 확인해주세요</td></tr>`);
    setTableEmpty("orderTable", true);
    if (note) note.textContent = "";
    return;
  }

  const orders = acct.orders || [];
  if (!orders.length) {
    setHTMLIfChanged(body,
      `<tr><td colspan="7" class="empty-row">체결 대기 중인 주문이 없습니다</td></tr>`);
    setTableEmpty("orderTable", true);
    if (note) note.textContent = "아직 체결되지 않고 걸려 있는 주문이 여기 표시됩니다";
    return;
  }

  const groups = groupOrders(orders);
  const html = groups
    .map((g) => {
      // 한 건짜리 그룹은 묶을 이유가 없으니 그냥 한 줄로 보여준다.
      if (g.items.length === 1) return orderRow(g.items[0], false);
      let out = groupRow(g);
      if (expandedOrderGroups.has(g.key)) {
        out += g.items.map((o) => orderRow(o, true)).join("");
      }
      return out;
    })
    .join("");
  setHTMLIfChanged(body, html);
  setTableEmpty("orderTable", false);

  if (note) {
    const manualCount = orders.filter((o) => o.manual).length;
    const symbols = [...new Set(orders.map((o) => o.base))].join(", ");
    note.textContent =
      `${orders.length}건 대기 중 (${symbols})` +
      (manualCount ? ` · 이 중 ${manualCount}건은 직접 넣은 주문` : "");
  }
}

// 그룹 펼치기/접기. 표는 매번 다시 그려지므로 tbody에 위임해서 붙인다.
document.getElementById("orderBody")?.addEventListener("click", (e) => {
  const row = e.target.closest("tr.ord-group");
  if (!row) return;
  const key = row.dataset.key;
  if (expandedOrderGroups.has(key)) expandedOrderGroups.delete(key);
  else expandedOrderGroups.add(key);
  if (lastAccount) renderOpenOrders(lastAccount);
});

/* ---------------- 잠금 상태 ---------------- */

function renderLocked() {
  const msg = (cols) =>
    `<tr><td colspan="${cols}" class="empty-row">` +
    `🔒 내 주문 내역이라 <a href="/login" class="lock-link">로그인</a> 후에 표시됩니다` +
    `</td></tr>`;
  setHTMLIfChanged(document.getElementById("manualBody"), msg(10));
  setHTMLIfChanged(document.getElementById("orderBody"), msg(7));
  setTableEmpty("manualTable", true);
  setTableEmpty("orderTable", true);
  const mn = document.getElementById("manualNote");
  const on = document.getElementById("orderNote");
  if (mn) mn.textContent = "";
  if (on) on.textContent = "";
}

/* ---------------- 갱신 ---------------- */

async function refreshAccount() {
  try {
    const data = await fetchJSON("/api/account");
    lastAccount = data;
    renderManualPositions(data);
    renderOpenOrders(data);
  } catch (err) {
    // 걸어둔 주문이 그대로 노출되는 데이터라 이 API만 로그인을 요구한다.
    // 로그인하면 다음 주기(10초)에 알아서 채워진다.
    if (err && err.status === 401) {
      lastAccount = null;
      renderLocked();
    }
    /* 그 외 조회 실패는 직전 화면을 그대로 둔다 */
  }
}

refreshAccount();
setInterval(refreshAccount, ACCOUNT_REFRESH_MS);
