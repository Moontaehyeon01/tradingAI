/* ==========================================================================
   AI 시황 의견 (참고용, 자동매매와 무관)

   서버가 30분마다 OpenAI에 시세+뉴스를 넘겨 받아온 의견을 그대로 보여준다.
   여기서는 렌더링만 하고, 주문/신호 어디에도 연결하지 않는다 - 이 프로젝트의
   실전 봇들은 전부 몇 달치 백테스트로 검증된 규칙으로만 매매하고, LLM 의견은
   그런 식으로 재현 검증이 안 되기 때문에 사람이 참고만 하도록 분리해뒀다.
   ========================================================================== */

const AI_OPINION_REFRESH_MS = 120000; // 서버가 30분마다만 갱신하니 자주 물어볼 필요 없음

function aiViewClass(view) {
  if (view === "강세") return "bullish";
  if (view === "약세") return "bearish";
  return "neutral";
}

function renderAiOpinion(payload) {
  const el = document.getElementById("aiOpinionBody");
  if (!el) return;

  if (payload.error) {
    setHTMLIfChanged(el, `<div class="empty-row">${payload.error}</div>`);
    return;
  }
  if (!payload.data) {
    setHTMLIfChanged(
      el,
      `<div class="empty-row">아직 생성된 의견이 없습니다 (첫 생성까지 잠시 걸릴 수 있습니다)</div>`
    );
    return;
  }

  const d = payload.data;
  const coins = (d.coins || [])
    .map(
      (c) => `
      <div class="ai-coin-card">
        <div class="ai-coin-head">
          <span class="ai-coin-symbol">${c.symbol}</span>
          <span class="ai-view-pill ${aiViewClass(c.view)}">${c.view}</span>
        </div>
        <div class="ai-confidence">확신도: ${c.confidence}</div>
        <div class="ai-reasoning">${c.reasoning}</div>
      </div>`
    )
    .join("");

  const generatedAgo = d.generated_at ? timeAgo(d.generated_at.replace("T", " ").slice(0, 19)) : "";

  setHTMLIfChanged(
    el,
    `
    <div class="ai-disclaimer">⚠ 이 의견은 AI가 생성한 참고용 정보이며, 투자 조언이 아니고 자동매매에 연결되어 있지 않습니다. 매매 판단은 본인 책임입니다.</div>
    <div class="ai-summary">${d.market_summary || ""}</div>
    <div class="ai-coin-grid">${coins}</div>
    ${d.caveat ? `<div class="ai-caveat">${d.caveat}</div>` : ""}
    <div class="ai-meta">${d.model || ""} · ${generatedAgo} 생성</div>
  `
  );
}

async function refreshAiOpinion() {
  try {
    const data = await fetchJSON("/api/ai_opinion");
    renderAiOpinion(data);
  } catch {
    /* 조회 실패 시 이전 표시를 유지한다 */
  }
}

refreshAiOpinion();
setInterval(refreshAiOpinion, AI_OPINION_REFRESH_MS);
