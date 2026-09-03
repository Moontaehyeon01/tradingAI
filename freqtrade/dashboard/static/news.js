/* ==========================================================================
   코인 뉴스 패널 (왼쪽)

   서버(server.py)가 백그라운드 스레드로 여러 크립토 뉴스 RSS를 긁어와
   제목/유사도로 중복을 걸러내고 한국어로 번역까지 마쳐서 /api/news 로
   내려준다. 여기서는 그 결과를 그대로 받아 그리기만 한다 - 번역이나
   중복 제거를 프론트에서 다시 할 필요는 없다.
   ========================================================================== */

const NEWS_REFRESH_MS = 60000;

function newsTimeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diffMin = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (diffMin < 1) return "방금 전";
  if (diffMin < 60) return `${diffMin}분 전`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}시간 전`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}일 전`;
}

function newsEscape(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function renderNews(list) {
  const container = document.getElementById("newsList");
  if (!container) return;

  if (!list || !list.length) {
    setHTMLIfChanged(container, `<div class="empty-row">뉴스를 불러오는 중…</div>`);
    return;
  }

  const html = list
    .map((n) => {
      const title = newsEscape(n.title);
      const summary = newsEscape(n.summary || "");
      const href = newsEscape(n.link || "#");
      return `
        <a class="news-item" href="${href}" target="_blank" rel="noopener noreferrer">
          <div class="news-item-title">${title}</div>
          ${summary ? `<div class="news-item-summary">${summary}</div>` : ""}
          <div class="news-item-meta">
            <span class="news-item-source">${newsEscape(n.source)}</span>
            <span>${newsTimeAgo(n.published)}</span>
          </div>
        </a>`;
    })
    .join("");

  setHTMLIfChanged(container, html);
}

async function refreshNews() {
  try {
    const data = await fetchJSON("/api/news");
    renderNews(data.news);
  } catch {
    /* 조회 실패 시 이전 표시를 유지한다 */
  }
}

refreshNews();
setInterval(refreshNews, NEWS_REFRESH_MS);
