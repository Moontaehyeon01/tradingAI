/* ==========================================================================
   주요 뉴스 (상단 한 줄 + AI 시황 섹션의 "주요 이벤트")

   예전 왼쪽 뉴스 목록은 기사가 너무 많아 잘 안 보게 돼서 없앴다(2026-10-06).
   대신 서버가 코인 매체 + 미국 증시·거시 매체의 새 기사를 GPT로 "시장 전체에
   영향을 줄 만한가" 1~5점으로 매기고, 4점 이상만 /api/major_news 로 내려준다.
   여기서는 그걸 상단에 한 줄씩 돌려 보여주고, AI 시황 위에 목록으로 보여준다.
   ========================================================================== */

const HEADLINE_REFRESH_MS = 60000;
const HEADLINE_ROTATE_MS = 7000;

let headlineItems = [];
let headlineIdx = 0;
let headlinePaused = false;
let headlineListOpen = false;

function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function headlineAgo(e) {
  return timeAgo((e.time || "").slice(0, 19));
}

function headlineBadge(e) {
  const lvl = e.importance >= 5 ? "hl-lvl5" : "hl-lvl4";
  return `<span class="hl-cat ${lvl}">${escHtml(e.category)}</span>`;
}

function renderHeadlineBar() {
  const el = document.getElementById("headlineBar");
  if (!el) return;
  if (!headlineItems.length) {
    setHTMLIfChanged(
      el,
      `<span class="hl-label">주요 뉴스</span>
       <span class="hl-empty">지난 24시간 동안 코인·미국 증시에 크게 영향을 줄 만한 뉴스는 없었습니다</span>`
    );
    return;
  }
  headlineIdx %= headlineItems.length;
  const e = headlineItems[headlineIdx];
  setHTMLIfChanged(
    el,
    `<span class="hl-label">주요 뉴스</span>
     <span class="hl-item">
       ${headlineBadge(e)}
       <a class="hl-text" href="${escHtml(e.link)}" target="_blank" rel="noopener noreferrer"
          title="${escHtml(e.impact)}">${escHtml(e.headline)}</a>
       <span class="hl-meta">${escHtml(e.source)} · ${headlineAgo(e)}</span>
     </span>
     ${
       headlineItems.length > 1
         ? `<span class="hl-nav">
              <button type="button" class="hl-btn" data-hl="-1" aria-label="이전 뉴스">‹</button>
              <span class="hl-count">${headlineIdx + 1}/${headlineItems.length}</span>
              <button type="button" class="hl-btn" data-hl="1" aria-label="다음 뉴스">›</button>
            </span>`
         : ""
     }
     <span class="hl-caret" title="목록 펼치기">${headlineListOpen ? "▴" : "▾"}</span>`
  );
}

// 상단 줄 아래로 펼쳐지는 전체 목록 (글씨 외 영역을 누르면 열고 닫음)
function renderHeadlineList() {
  const el = document.getElementById("headlineList");
  if (!el) return;
  el.hidden = !headlineListOpen || !headlineItems.length;
  setHTMLIfChanged(
    el,
    headlineItems
      .map(
        (e) => `
        <div class="hl-row">
          ${headlineBadge(e)}
          <div class="hl-row-body">
            <a class="hl-row-title" href="${escHtml(e.link)}" target="_blank" rel="noopener noreferrer">${escHtml(e.headline)}</a>
            <div class="hl-row-impact">${escHtml(e.impact)}</div>
          </div>
          <span class="hl-meta">${escHtml(e.source)} · ${headlineAgo(e)}</span>
        </div>`
      )
      .join("")
  );
}

function renderAiEvents() {
  const el = document.getElementById("aiEvents");
  if (!el) return;
  if (!headlineItems.length) {
    setHTMLIfChanged(el, "");
    return;
  }
  const rows = headlineItems
    .map(
      (e) => `
      <a class="ai-event" href="${escHtml(e.link)}" target="_blank" rel="noopener noreferrer">
        <div class="ai-event-head">
          ${headlineBadge(e)}
          <span class="ai-event-title">${escHtml(e.headline)}</span>
          <span class="ai-event-meta">${escHtml(e.source)} · ${headlineAgo(e)}</span>
        </div>
        <div class="ai-event-impact">${escHtml(e.impact)}</div>
      </a>`
    )
    .join("");
  setHTMLIfChanged(
    el,
    `<div class="ai-events">
       <div class="ai-events-head">주요 이벤트 <span class="ai-score-sub">지난 24시간 · 코인/미국 증시 영향도 상위 뉴스 · AI 분류</span></div>
       ${rows}
     </div>`
  );
}

document.getElementById("headlineBar")?.addEventListener("click", (e) => {
  if (e.target.closest("a")) return; // 뉴스 글씨 = 원문 링크(새 탭)
  if (!headlineItems.length) return;
  const btn = e.target.closest(".hl-btn");
  if (btn) {
    headlineIdx = (headlineIdx + Number(btn.dataset.hl) + headlineItems.length) % headlineItems.length;
  } else {
    headlineListOpen = !headlineListOpen;
    renderHeadlineList();
  }
  renderHeadlineBar();
});
document.getElementById("headlineBar")?.addEventListener("mouseenter", () => (headlinePaused = true));
document.getElementById("headlineBar")?.addEventListener("mouseleave", () => (headlinePaused = false));

async function refreshHeadlines() {
  try {
    const data = await fetchJSON("/api/major_news");
    headlineItems = data.items || [];
    renderHeadlineBar();
    renderHeadlineList();
    renderAiEvents();
  } catch {
    /* 조회 실패 시 이전 표시를 유지한다 */
  }
}

refreshHeadlines();
setInterval(refreshHeadlines, HEADLINE_REFRESH_MS);
setInterval(() => {
  if (headlinePaused || headlineItems.length < 2) return;
  headlineIdx = (headlineIdx + 1) % headlineItems.length;
  renderHeadlineBar();
}, HEADLINE_ROTATE_MS);
