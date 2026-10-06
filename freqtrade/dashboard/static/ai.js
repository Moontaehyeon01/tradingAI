/* ==========================================================================
   AI 시황 의견 + AI 방향 예측 (참고용, 자동매매와 무관)

   롱/숏 판단은 매일 결과로 채점되고 매주 재학습되는 예측 모델(서버 predictor.py)이
   하고, GPT는 그 판단과 근거를 리포트로 풀어 쓴다. 여기서는 렌더링만 하고 주문/신호
   어디에도 연결하지 않는다. 모델 성적은 항상 "동전 던지기"·"항상 롱" 기준선과
   나란히 보여줘서, 기준선을 못 이기면 화면에 그대로 드러나게 한다.
   ========================================================================== */

const AI_OPINION_REFRESH_MS = 120000; // 서버가 30분마다만 갱신하니 자주 물어볼 필요 없음

function aiViewClass(view) {
  if (view === "롱" || view === "강세") return "bullish";
  if (view === "숏" || view === "약세") return "bearish";
  return "neutral";
}

// 문단 텍스트(\n\n으로 구분)를 <p>로 쪼갠다. 상세 리포트가 여러 문단이라 그대로
// 한 덩어리로 넣으면 읽기 힘들다.
function aiParagraphs(text) {
  if (!text) return "";
  return text
    .split(/\n{2,}/)
    .map((p) => `<p>${p.trim()}</p>`)
    .join("");
}

function aiPct(v) {
  return v === null || v === undefined ? "–" : `${v}%`;
}

function renderPredictionScore(pred) {
  if (!pred || pred.error) return "";
  const live = pred.live;
  const live30 = pred.live_30d;
  const m = pred.model;
  const ho = m && m.holdout;

  const warnings = [];
  if (ho && ho.acc <= Math.max(0.5, ho.long_acc)) {
    warnings.push(
      `검증 구간(최근 ${ho.n / 4}일)에서 모델 정확도 ${(ho.acc * 100).toFixed(1)}%가 기준선` +
        `(항상 롱 ${(ho.long_acc * 100).toFixed(1)}%, 동전 50%)을 넘지 못했습니다. 지금 롱/숏 판단은 참고 수준입니다.`
    );
  }
  if (live && live.n >= 20 && !live.beats_baseline) {
    warnings.push(`실전 기록 ${live.n}건에서도 기준선을 이기지 못하고 있습니다.`);
  }

  const liveBlock = live
    ? `
      <div class="ai-score-item"><div class="lbl">실전 정확도</div><div class="val">${aiPct(live.acc)} <small>(${live.n}건)</small></div></div>
      <div class="ai-score-item"><div class="lbl">기준선: 항상 롱</div><div class="val">${aiPct(live.always_long)}</div></div>
      <div class="ai-score-item"><div class="lbl">기준선: 동전 던지기</div><div class="val">50%</div></div>
      <div class="ai-score-item"><div class="lbl">최근 30일</div><div class="val">${live30 ? aiPct(live30.acc) : "–"}</div></div>
      <div class="ai-score-item"><div class="lbl">판단대로 했다면 (합계)</div><div class="val ${live.follow_ret_pct >= 0 ? "pos" : "neg"}">${live.follow_ret_pct >= 0 ? "+" : ""}${live.follow_ret_pct}%</div></div>`
    : `<div class="ai-score-empty">실전 기록 수집 중입니다 - 매일 09:10(한국시간)에 예측하고 다음날 같은 시각에 채점합니다.</div>`;

  const modelLine = m
    ? `모델 v${m.version} · 사용 신호: ${m.set} · 검증(최근 180일) 정확도 ${(ho.acc * 100).toFixed(1)}% vs 항상 롱 ${(ho.long_acc * 100).toFixed(1)}% · ` +
      `마지막 학습 ${timeAgo(m.trained_at.replace("T", " ").slice(0, 19))} · 매주 재학습(더 나은 신호 조합이 확인될 때만 교체)`
    : "모델 학습 대기 중";

  const recent = (pred.recent || []).slice(0, 8);
  const recentRows = recent
    .map(
      (p) => `
      <tr>
        <td>${p.date.slice(5)}</td>
        <td>${p.symbol.replace("USDT", "")}</td>
        <td><span class="ai-view-pill ${aiViewClass(p.decision)}">${p.decision}</span></td>
        <td>${(p.prob_up * 100).toFixed(0)}%</td>
        <td class="${p.ret_pct >= 0 ? "pos" : "neg"}">${p.actual} ${p.ret_pct >= 0 ? "+" : ""}${p.ret_pct}%</td>
        <td>${p.correct ? "✅" : "❌"}</td>
      </tr>`
    )
    .join("");

  return `
    <div class="ai-score">
      <div class="ai-score-head">AI 예측 성적 <span class="ai-score-sub">매일 09:10(KST) 예측 · 다음날 자동 채점 · 매주 재학습</span></div>
      ${warnings.map((w) => `<div class="ai-score-warn">⚠ ${w}</div>`).join("")}
      <div class="ai-score-grid">${liveBlock}</div>
      <div class="ai-score-model">${modelLine}</div>
      ${pred.last_error ? `<div class="ai-score-warn">예측 작업 오류: ${pred.last_error}</div>` : ""}
      ${
        recentRows
          ? `<div class="ai-score-table-wrap"><table class="ai-score-table">
               <thead><tr><th>날짜</th><th>코인</th><th>판단</th><th>상승확률</th><th>실제</th><th></th></tr></thead>
               <tbody>${recentRows}</tbody></table></div>`
          : ""
      }
    </div>`;
}

function renderAiOpinion(payload, pred) {
  const el = document.getElementById("aiOpinionBody");
  if (!el) return;

  const score = renderPredictionScore(pred);
  if (payload.error) {
    setHTMLIfChanged(el, `${score}<div class="empty-row">${payload.error}</div>`);
    return;
  }
  if (!payload.data) {
    setHTMLIfChanged(
      el,
      `${score}<div class="empty-row">아직 생성된 의견이 없습니다 (첫 생성까지 잠시 걸릴 수 있습니다)</div>`
    );
    return;
  }

  const d = payload.data;
  const coins = (d.coins || [])
    .map(
      (c) => `
      <div class="ai-coin-card">
        <button type="button" class="ai-expand-toggle"
                data-label-collapsed="상세 리포트 보기" data-label-expanded="접기">
          <div class="ai-coin-head">
            <span class="ai-coin-symbol">${c.symbol}</span>
            <span class="ai-view-pill ${aiViewClass(c.view)}">${c.view}${
              c.prob_up !== undefined && c.prob_up !== null ? ` · 상승 ${(c.prob_up * 100).toFixed(0)}%` : ""
            }</span>
          </div>
          <div class="ai-confidence">확신도: ${c.confidence}</div>
          <div class="ai-reasoning">${c.reasoning}</div>
          <div class="ai-expand-hint"><span class="ai-expand-arrow">▾</span> <span class="ai-expand-label">상세 리포트 보기</span></div>
        </button>
        <div class="ai-detail" hidden>${aiParagraphs(c.detail)}</div>
      </div>`
    )
    .join("");

  const generatedAgo = d.generated_at ? timeAgo(d.generated_at.replace("T", " ").slice(0, 19)) : "";

  setHTMLIfChanged(
    el,
    `
    <div class="ai-disclaimer">⚠ 이 의견은 AI가 생성한 참고용 정보이며, 투자 조언이 아니고 자동매매에 연결되어 있지 않습니다. 매매 판단은 본인 책임입니다.</div>
    ${score}
    <button type="button" class="ai-expand-toggle ai-summary-toggle"
            data-label-collapsed="전체 시황 리포트 보기" data-label-expanded="접기">
      <div class="ai-summary">${d.market_summary || ""}</div>
      <div class="ai-expand-hint"><span class="ai-expand-arrow">▾</span> <span class="ai-expand-label">전체 시황 리포트 보기</span></div>
    </button>
    <div class="ai-detail ai-market-report" hidden>${aiParagraphs(d.market_report)}</div>
    <div class="ai-coin-grid">${coins}</div>
    ${d.caveat ? `<div class="ai-caveat">${d.caveat}</div>` : ""}
    <div class="ai-meta">롱/숏: 예측 모델 · 리포트: ${d.model || ""} · ${generatedAgo} 생성${
       d.trigger ? ` · 주요 뉴스로 앞당겨 갱신: ${escHtml(d.trigger)}` : ""
     }</div>
  `
  );
}

// setHTMLIfChanged는 내용이 안 바뀌면 innerHTML을 안 건드리므로(서버 캐시가
// 30분에 한 번만 갱신됨), 여기서 토글한 hidden 상태가 재렌더로 리셋되지
// 않는다. 이벤트 위임으로 한 번만 등록하면 재렌더와 무관하게 계속 동작한다.
document.getElementById("aiOpinionBody")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".ai-expand-toggle");
  if (!btn) return;
  const detail = btn.nextElementSibling;
  if (!detail || !detail.classList.contains("ai-detail")) return;
  const willExpand = detail.hidden; // 지금 접혀있으면 이번 클릭으로 펼쳐짐
  detail.hidden = !willExpand;
  btn.classList.toggle("expanded", willExpand);
  const label = btn.querySelector(".ai-expand-label");
  if (label) {
    label.textContent = willExpand
      ? btn.dataset.labelExpanded
      : btn.dataset.labelCollapsed;
  }
});

async function refreshAiOpinion() {
  try {
    const [data, pred] = await Promise.all([
      fetchJSON("/api/ai_opinion"),
      fetchJSON("/api/ai_prediction").catch(() => null),
    ]);
    renderAiOpinion(data, pred);
  } catch {
    /* 조회 실패 시 이전 표시를 유지한다 */
  }
}

refreshAiOpinion();
setInterval(refreshAiOpinion, AI_OPINION_REFRESH_MS);
