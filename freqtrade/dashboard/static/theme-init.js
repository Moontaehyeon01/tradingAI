// 첫 화면이 그려지기 전에 테마·화면을 정한다(밝은 화면 번쩍임 방지).
// 인라인 스크립트를 금지하는 보안 정책(CSP) 때문에 index.html 에서 이 파일로 옮겼다(2026-11-12).
// 라이트/다크 모드: 저장된 선택이 있으면 그것, 없으면 라이트(theme.js 에서 전환)
  (function () {
    var t = null;
    try { t = localStorage.getItem("theme"); } catch (e) {}
    document.documentElement.dataset.theme = t === "dark" ? "dark" : "light";
    var v = (location.hash || "").slice(1);
    if (["overview", "signals", "history", "ai"].indexOf(v) < 0) v = "overview";
    document.documentElement.dataset.view = v;
  })();
