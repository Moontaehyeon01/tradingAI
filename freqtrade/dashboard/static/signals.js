/* ==========================================================================
   진입 조건 현황

   "봇이 왜 지금 진입을 안 하는가"를 화면에서 바로 답하기 위한 패널.
   이게 없으면 봇이 고장난 건지, 조건이 안 맞아서 기다리는 건지 구분이 안 된다.

   봇마다 진입 조건의 성격이 달라서 표 구조 자체가 다르다.
     - 박스 돌파(kind: "box"): 직전 N봉의 고가-저가 폭이 기준 이하인지(박스권),
       종가가 그 박스를 벗어났는지(돌파). 페어 하나만 보고 판단한다.
     - 횡단면 모멘텀(kind: "xsect_momentum"): 감시 페어 전체를 수익률로 줄 세워
       상위/하위가 롱/숏 후보가 된다. 페어 하나만 봐서는 답이 안 나오고 전체
       순위가 있어야 한다.
   server.py 의 fetch_signals() 가 지금 가동 중인 봇의 신호만 보내주므로,
   정지된 봇은 화면에서 자동으로 빠진다 - 이 파일은 kind 별로 표를 나눠 그릴
   뿐, "어느 봇을 보여줄지"는 신경 쓰지 않는다.
   ========================================================================== */

const SIGNAL_REFRESH_MS = 30000;
// 진입 조건 현황 탭: 선택된 봇(kind|bot 키)과 마지막 응답(탭 클릭 시 다시 그리기용)
let selectedSignalKey = null;
let lastSignalList = null;

function sigFmt(v) {
  if (v === null || v === undefined) return "–";
  return v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 0 : 4 });
}

/* ---------------- 박스 돌파 ---------------- */

function renderBoxGroup(items) {
  // 조건에 가까운 것부터: 박스권 충족분을 위로, 그 안에서 돌파에 가까운 순
  const sorted = [...items].sort((a, b) => {
    if (a.is_box !== b.is_box) return a.is_box ? -1 : 1;
    const na = Math.min(Math.abs(a.to_high_pct), Math.abs(a.to_low_pct));
    const nb = Math.min(Math.abs(b.to_high_pct), Math.abs(b.to_low_pct));
    return na - nb;
  });

  const rows = sorted
    .map((s) => {
      const sym = s.pair.split("/")[0];
      const widthPct = s.box_width * 100;
      const limitPct = s.limit * 100;
      const ok = s.is_box;

      let status;
      if (s.enter_long) status = '<span class="sig-badge go">돌파 · 롱</span>';
      else if (s.enter_short) status = '<span class="sig-badge go short">이탈 · 숏</span>';
      else if (!ok) status = '<span class="sig-badge off">폭 초과</span>';
      else status = '<span class="sig-badge wait">대기</span>';

      // 돌파까지 남은 거리. 음수면 이미 그 방향으로 나가 있다는 뜻.
      const near = Math.min(Math.abs(s.to_high_pct), Math.abs(s.to_low_pct));
      const dir = Math.abs(s.to_high_pct) <= Math.abs(s.to_low_pct) ? "상단" : "하단";
      const distTxt = ok ? `${dir}까지 ${near.toFixed(1)}%` : "–";
      const distCls = ok && near < 0.5 ? "warn" : "";

      return `
        <tr class="chart-row" data-chart-base="${sym}" title="누르면 오른쪽에 ${sym} 차트">
          <td class="pair-cell">${sym}</td>
          <td>${sigFmt(s.box_low)} ~ ${sigFmt(s.box_high)}</td>
          <td class="${ok ? "pos" : "neg"}">${widthPct.toFixed(2)}%
            <span class="sig-limit">/ ${limitPct.toFixed(0)}%</span></td>
          <td>${sigFmt(s.close)}</td>
          <td class="${distCls}">${distTxt}</td>
          <td>${status}</td>
        </tr>`;
    })
    .join("");

  const nBox = items.filter((s) => s.is_box).length;
  const note =
    nBox === 0
      ? `${items.length}개 모두 박스 폭이 기준을 넘어 대기 중입니다. 변동성이 줄어야 진입합니다.`
      : `${items.length}개 중 ${nBox}개가 박스권 조건 충족. 박스를 벗어나면 진입합니다.`;

  const table = `
    <table class="history-table">
      <thead>
        <tr><th>페어</th><th>박스권</th><th>박스폭</th><th>현재가</th><th>돌파까지</th><th>상태</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  return { table, note };
}

/* ---------------- 횡단면 모멘텀 ---------------- */

function renderXsectGroup(items) {
  // 순위 점수 높은 순으로 - 위가 롱 후보, 아래가 숏 후보.
  // 점수는 봇마다 다르다: 기본은 14일 수익률, RSI 봇은 RSI(14) (score_kind).
  const isRsi = items[0]?.score_kind === "rsi";
  const sorted = [...items].sort((a, b) => (b.score ?? b.ret) - (a.score ?? a.ret));

  const rows = sorted
    .map((s) => {
      const sym = s.pair.split("/")[0];
      let status;
      if (s.status === "long") status = '<span class="sig-badge go">롱 후보</span>';
      else if (s.status === "short") status = '<span class="sig-badge go short">숏 후보</span>';
      else status = '<span class="sig-badge wait">순위 밖</span>';
      // 순위로는 후보지만 청산 후 재진입 대기 중이라 건너뛴 종목 - 다음 순위가 대신 후보가 된다
      if (s.lock_until) {
        const d = new Date(s.lock_until);
        const until = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
        status = `<span class="sig-badge wait">재진입 대기 ~${until}</span>`;
      } else if (s.foreign && s.status === "wait") {
        // 계좌에 다른 봇/수동 포지션이 있어 건너뛴 종목 (RSI 봇) - 다음 순위가 대신 후보
        status = '<span class="sig-badge wait">다른 포지션 보유 중</span>';
      }
      const retCls = s.ret > 0 ? "pos" : s.ret < 0 ? "neg" : "";

      return `
        <tr class="chart-row" data-chart-base="${sym}" title="누르면 오른쪽에 ${sym} 차트">
          <td class="pair-cell">${sym}</td>
          <td>${s.rank} / ${s.total}</td>
          ${isRsi ? `<td>${s.score.toFixed(1)}</td>` : ""}
          <td class="${retCls}">${(s.ret * 100).toFixed(2)}%</td>
          <td>${sigFmt(s.close)}</td>
          <td>${status}</td>
        </tr>`;
    })
    .join("");

  const longs = items.filter((s) => s.status === "long").length;
  const shorts = items.filter((s) => s.status === "short").length;
  const scoreName = isRsi ? "RSI(14)" : "14일 수익률";
  const note =
    `${items[0]?.bot_name ?? "Momentum"}${items[0]?.stopped ? " [정지 중 · 켜면 이 순위로 진입]" : ""}: ` +
    `${scoreName} 기준 감시 페어 ${items.length}개 중 ` +
    `상위 ${longs}개 롱 후보, 하위 ${shorts}개 숏 후보 (다음 일봉 마감 시 리밸런싱)`;
  const caption = `<div class="panel-sub" style="margin:8px 0 4px">${note}</div>`;

  const table = `${caption}
    <table class="history-table">
      <thead>
        <tr><th>페어</th><th>순위</th>${isRsi ? "<th>RSI(14)</th>" : ""}<th>14일 수익률</th><th>현재가</th><th>상태</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
  // 상단 요약(접혔을 때 보이는 줄)은 짧게 - 자세한 설명은 표 위 캡션에 있다
  const shortNote = `${items[0]?.bot_name ?? "Momentum"}${items[0]?.stopped ? " (정지 중)" : ""}`;
  return { table, note: shortNote };
}

/* ---------------- 조립 ---------------- */

function renderSignals(list) {
  lastSignalList = list;
  const container = document.getElementById("signalGroups");
  const note = document.getElementById("signalNote");
  if (!container) return;

  if (!list || !list.length) {
    setHTMLIfChanged(
      container,
      `<div class="table-scroll"><table class="history-table"><tbody>
        <tr><td class="empty-row">가동 중인 봇이 없어 표시할 신호가 없습니다</td></tr>
      </tbody></table></div>`
    );
    if (note) note.textContent = "";
    return;
  }

  // kind + bot 별로 묶는다 - 박스 돌파와 순위 기반은 표 구성 자체가 다르다.
  const groups = new Map();
  for (const s of list) {
    const key = `${s.kind}|${s.bot}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }

  // 봇이 여러 개면 표를 세로로 다 늘어놓지 않고 탭으로 하나씩 보여준다.
  // 선택은 기억해 두고(새로고침 후에도), 없으면 가동 중인 봇을 먼저 보여준다.
  const keys = [...groups.keys()];
  if (!groups.has(selectedSignalKey)) {
    const saved = loadSignalTab();
    selectedSignalKey = groups.has(saved)
      ? saved
      : keys.find((k) => !groups.get(k)[0].stopped) ?? keys[0];
  }

  const notes = [];
  let tabs = "";
  let html = "";
  for (const [key, items] of groups) {
    const { table, note: groupNote } =
      items[0].kind === "xsect_momentum" ? renderXsectGroup(items) : renderBoxGroup(items);
    notes.push(groupNote);
    const active = key === selectedSignalKey ? "active" : "";
    const dot = items[0].stopped ? "err" : "ok";
    tabs += `<button type="button" class="bot-tab sig-tab ${active}" data-sig="${key}">
        <span class="dot ${dot}"></span>${items[0].bot_name}${items[0].stopped ? " · 정지" : ""}
      </button>`;
    if (key === selectedSignalKey) html = `<div class="table-scroll">${table}</div>`;
  }
  if (keys.length > 1) html = `<div class="bot-title-tabs sig-tabs">${tabs}</div>` + html;

  setHTMLIfChanged(container, html);
  if (note) note.textContent = notes.join("  ·  ");
}


function loadSignalTab() {
  try {
    return localStorage.getItem("signalTab");
  } catch {
    return null;
  }
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".sig-tab");
  if (!tab || tab.dataset.sig === selectedSignalKey) return;
  selectedSignalKey = tab.dataset.sig;
  try {
    localStorage.setItem("signalTab", selectedSignalKey);
  } catch {
    /* 저장 안 돼도 이번 화면에선 동작 */
  }
  renderSignals(lastSignalList);
});

async function refreshSignals() {
  try {
    const data = await fetchJSON("/api/signals");
    renderSignals(data.signals);
  } catch {
    /* 조회 실패 시 이전 표시를 유지한다 */
  }
}

refreshSignals();
setInterval(refreshSignals, SIGNAL_REFRESH_MS);
