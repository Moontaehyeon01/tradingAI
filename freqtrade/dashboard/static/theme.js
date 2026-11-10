/* ==========================================================================
   라이트 / 다크 모드 (2026-11-10)

   색은 style.css 의 :root 토큰에만 있고, 다크 모드는 <html data-theme="dark"> 일 때
   같은 이름의 토큰을 덮어쓴다. 처음 테마는 index.html <head> 의 짧은 스크립트가
   CSS 보다 먼저 정한다(밝은 화면이 잠깐 번쩍이는 것 방지). 선택은 이 브라우저에만
   저장된다(localStorage).
   그래프(Chart.js)와 TradingView 위젯은 CSS 를 따라오지 않아서, cssVar() 로 토큰 값을
   읽어 그리고 onThemeChange() 로 바뀔 때 다시 칠한다(app.js, charts.js).
   ========================================================================== */

const THEME_KEY = "theme";
const _themeListeners = [];

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function onThemeChange(fn) {
  _themeListeners.push(fn);
}

function _syncThemeButton() {
  const btn = document.getElementById("themeBtn");
  if (!btn) return;
  const label = currentTheme() === "dark" ? "라이트 모드로" : "다크 모드로";
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
  try {
    localStorage.setItem(THEME_KEY, currentTheme());
  } catch (e) {
    /* 사생활 보호 모드 등에서는 저장만 안 되고 전환은 된다 */
  }
  _syncThemeButton();
  for (const fn of _themeListeners) {
    try {
      fn(currentTheme());
    } catch (e) {
      console.error("theme listener", e);
    }
  }
}

document.getElementById("themeBtn")?.addEventListener("click", () => {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
});
_syncThemeButton();
