(function initializeElrenTheme() {
  "use strict";

  // Native date/time controls snapshot the document language when they are
  // created. Resolve it in the head, before the form exists, so an English
  // page never inherits Chromium's Chinese day-field placeholder.
  try {
    const queryLanguage = new URLSearchParams(window.location.search).get("lang");
    const savedLanguage = queryLanguage
      ?? window.localStorage.getItem("elren.ui-language.v1")
      ?? window.localStorage.getItem("milo.ui-language.v1")
      ?? window.localStorage.getItem("deepdesk.ui-language.v1");
    document.documentElement.lang = savedLanguage === "en" ? "en-US" : "zh-CN";
  } catch {
    try {
      document.documentElement.lang = new URLSearchParams(window.location.search).get("lang") === "en"
        ? "en-US"
        : "zh-CN";
    } catch {}
  }

  const MODE_KEY = "elren.theme-mode.v1";
  const ACCENT_KEY = "elren.accent-color.v1";
  const DEFAULT_MODE = "light";
  const DEFAULT_ACCENT = "#a6533f";

  function normalizeMode(value) {
    return DEFAULT_MODE;
  }

  function normalizeAccent(value) {
    return DEFAULT_ACCENT;
  }

  function accentContrast(hex) {
    const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const linear = channels.map((channel) => (
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    ));
    const luminance = (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2]);
    // Pick whichever of near-black or white has the higher WCAG contrast.
    // Their contrast ratios cross at a relative luminance of about 0.179.
    return luminance > 0.179 ? "#06121a" : "#ffffff";
  }

  function accentLuminance(hex) {
    const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const linear = channels.map((channel) => (
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
    ));
    return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2]);
  }

  // Keep the fixed warm accent legible if this helper is reused by a legacy
  // embedded surface with different luminance.
  function visibleAccent(hex, resolvedMode) {
    const luminance = accentLuminance(hex);
    if (resolvedMode === "dark" && luminance < 0.18) return "#f3f6fb";
    if (resolvedMode === "light" && luminance > 0.82) return "#172033";
    return hex;
  }

  function read() {
    return { mode: DEFAULT_MODE, accent: DEFAULT_ACCENT };
  }

  function apply(mode, accent) {
    const normalizedMode = DEFAULT_MODE;
    const normalizedAccent = DEFAULT_ACCENT;
    const resolvedMode = DEFAULT_MODE;
    const root = document.documentElement;
    root.dataset.themeMode = normalizedMode;
    root.dataset.theme = resolvedMode;
    root.style.setProperty("--accent", normalizedAccent);
    root.style.setProperty("--accent-contrast", accentContrast(normalizedAccent));
    root.style.setProperty("--accent-visible", visibleAccent(normalizedAccent, resolvedMode));
    return { mode: normalizedMode, accent: normalizedAccent, resolvedMode };
  }

  function save(mode, accent) {
    const preference = apply(mode, accent);
    try {
      window.localStorage.setItem(MODE_KEY, preference.mode);
      window.localStorage.setItem(ACCENT_KEY, preference.accent);
    } catch {
      // The preview still works when browser storage is unavailable.
    }
    return preference;
  }

  function restore() {
    const preference = read();
    return apply(preference.mode, preference.accent);
  }

  window.ElrenTheme = Object.freeze({
    ACCENT_KEY,
    DEFAULT_ACCENT,
    DEFAULT_MODE,
    MODE_KEY,
    apply,
    normalizeAccent,
    read,
    restore,
    save,
  });
  restore();
}());
