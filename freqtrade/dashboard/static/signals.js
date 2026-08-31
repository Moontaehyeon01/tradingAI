/* ==========================================================================
   진입 조건 현황

   "봇이 왜 지금 진입을 안 하는가"를 화면에서 바로 답하기 위한 패널.
   이게 없으면 봇이 고장난 건지, 조건이 안 맞아서 기다리는 건지 구분이 안 된다.

   박스 돌파 전략의 진입 조건은 두 단계다.
     1) 직전 N봉의 고가-저가 폭이 기준(4%) 이하  -> '박스권'으로 인정
     2) 종가가 그 박스를 위/아래로 벗어남        -> 진입
   그래서 화면에는 '폭이 기준 안인지'와 '돌파선까지 얼마 남았는지'를 같이 보여준다.
   ========================================================================== */

const SIGNAL_REFRESH_MS = 30000;

function sigFmt(v) {
  if (v === null || v === undefined) return "–";
  return v.toLocaleString(undefined, { maximumFractionDigits: v >= 100 ? 0 : 4 });
}

function renderSignals(list) {
  const body = document.getElementById("signalBody");
  const note = document.getElementById("signalNote");
  if (!body) return;

  if (!list || !list.length) {
    setHTMLIfChanged(body, '<tr><td colspan="6" class="empty-row">지표를 불러오는 중…</td></tr>');
    if (note) note.textContent = "";
    return;
  }

  // 조건에 가까운 것부터: 박스권 충족분을 위로, 그 안에서 돌파에 가까운 순
  const sorted = [...list].sort((a, b) => {
    if (a.is_box !== b.is_box) return a.is_box ? -1 : 1;
    const na = Math.min(Math.abs(a.to_high_pct), Math.abs(a.to_low_pct));
    const nb = Math.min(Math.abs(b.to_high_pct), Math.abs(b.to_low_pct));
    return na - nb;
  });

  const html = sorted
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
        <tr>
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

  setHTMLIfChanged(body, html);

  if (note) {
    const nBox = list.filter((s) => s.is_box).length;
    note.textContent =
      nBox === 0
        ? `${list.length}개 모두 박스 폭이 기준을 넘어 대기 중입니다. 변동성이 줄어야 진입합니다.`
        : `${list.length}개 중 ${nBox}개가 박스권 조건 충족. 박스를 벗어나면 진입합니다.`;
  }
}

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
