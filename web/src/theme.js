// Light/dark persistence. The design tokens live in index.css (:root and :root.dark).
const KEY = "lf-theme";

export function initTheme() {
  const t = localStorage.getItem(KEY);
  const dark = t ? t === "dark" : window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", !!dark);
  applyPrefs();
}

export function toggleTheme() {
  const dark = !document.documentElement.classList.contains("dark");
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem(KEY, dark ? "dark" : "light");
  window.dispatchEvent(new Event("lf-theme"));   // keep every toggle button in sync
  return dark;
}

export function isDark() {
  return document.documentElement.classList.contains("dark");
}

// --- Appearance / accessibility prefs (density, text size, contrast, motion) ---
const PREFS_KEY = "lf-appearance";
const DEFAULTS = { density: "comfortable", text: "normal", contrast: "normal", motion: "full" };

export function getPrefs() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") }; }
  catch { return { ...DEFAULTS }; }
}

export function applyPrefs(p = getPrefs()) {
  const r = document.documentElement;
  r.setAttribute("data-density", p.density);
  r.setAttribute("data-text", p.text);
  r.setAttribute("data-contrast", p.contrast);
  r.setAttribute("data-motion", p.motion);
}

export function setPref(key, val) {
  const p = { ...getPrefs(), [key]: val };
  localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  applyPrefs(p);
  return p;
}
