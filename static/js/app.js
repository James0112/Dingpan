let deferredInstallPrompt = null;
const INSTALL_BANNER_DISMISSED_KEY = "dingpan_install_banner_dismissed";
const THEME_STORAGE_KEY = "dingpan-theme";
const THEME_COLOR_LIGHT = "#f5f5f7";
const THEME_COLOR_DARK = "#121212";
let themeMediaQuery = null;

function isStandaloneMode() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function getStoredThemeChoice() {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) || "system";
  } catch (error) {
    return "system";
  }
}

function resolveTheme(choice) {
  if (choice === "dark") {
    return "dark";
  }
  if (choice === "light") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function themeLabel(choice) {
  if (choice === "light") {
    return "主题：浅色";
  }
  if (choice === "dark") {
    return "主题：深色";
  }
  return "主题：跟随系统";
}

function updateThemeMeta(resolvedTheme) {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.setAttribute("content", resolvedTheme === "dark" ? THEME_COLOR_DARK : THEME_COLOR_LIGHT);
  }
}

function applyTheme(choice) {
  const resolvedTheme = resolveTheme(choice);
  const root = document.documentElement;
  root.setAttribute("data-theme", resolvedTheme);
  root.setAttribute("data-theme-choice", choice);
  updateThemeMeta(resolvedTheme);

  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.textContent = window.matchMedia("(max-width: 640px)").matches ? themeLabel(choice).replace("主题：", "") : themeLabel(choice);
    button.dataset.themeChoice = choice;
  });
}

window.applyThemeChoice = applyTheme;

function persistTheme(choice) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch (error) {
    // Ignore localStorage write failures in private mode.
  }
}

function cycleThemeChoice() {
  const current = document.documentElement.getAttribute("data-theme-choice") || "system";
  if (current === "system") {
    return "light";
  }
  if (current === "light") {
    return "dark";
  }
  return "system";
}

function syncThemeFromSystem() {
  const choice = document.documentElement.getAttribute("data-theme-choice") || "system";
  if (choice === "system") {
    applyTheme("system");
  }
}

function bindThemeToggle() {
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextChoice = cycleThemeChoice();
      persistTheme(nextChoice);
      applyTheme(nextChoice);
    });
  });
}

function bindViewportKeyboardBehavior() {
  const root = document.documentElement;

  const updateViewportState = () => {
    if (!window.visualViewport) {
      root.style.setProperty("--viewport-bottom-offset", "0px");
      return;
    }

    const viewport = window.visualViewport;
    const keyboardOpen = viewport.height < window.innerHeight * 0.75;
    document.body.classList.toggle("keyboard-open", keyboardOpen);

    const viewportBottomOffset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
    root.style.setProperty("--viewport-bottom-offset", `${viewportBottomOffset}px`);
  };

  if (!window.visualViewport) {
    updateViewportState();
    return;
  }

  window.visualViewport.addEventListener("resize", updateViewportState);
  window.visualViewport.addEventListener("scroll", updateViewportState);
  window.addEventListener("resize", updateViewportState);
  updateViewportState();
}

function ensureInstallBanner() {
  return document.getElementById("install-banner");
}

function setInstallBannerState(message, visible) {
  const banner = ensureInstallBanner();
  if (!banner) {
    return;
  }
  const text = banner.querySelector("[data-install-text]");
  if (text && message) {
    text.textContent = message;
  }
  banner.hidden = !visible;
}

function isInstallBannerDismissed() {
  return window.localStorage.getItem(INSTALL_BANNER_DISMISSED_KEY) === "1";
}

function dismissInstallBanner() {
  window.localStorage.setItem(INSTALL_BANNER_DISMISSED_KEY, "1");
  setInstallBannerState("", false);
}

async function handleInstallClick() {
  if (!deferredInstallPrompt) {
    setInstallBannerState("请使用浏览器菜单中的“添加到主屏幕”完成安装。", true);
    return;
  }
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
  setInstallBannerState("", false);
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  if (!isStandaloneMode() && !isInstallBannerDismissed()) {
    setInstallBannerState("安装到桌面后，可从独立应用窗口打开并单独开启推送。", true);
  }
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  window.localStorage.removeItem(INSTALL_BANNER_DISMISSED_KEY);
  setInstallBannerState("", false);
});

window.addEventListener("DOMContentLoaded", () => {
  applyTheme(getStoredThemeChoice());
  bindThemeToggle();
  bindViewportKeyboardBehavior();

  if (window.matchMedia) {
    themeMediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    if (typeof themeMediaQuery.addEventListener === "function") {
      themeMediaQuery.addEventListener("change", syncThemeFromSystem);
    } else if (typeof themeMediaQuery.addListener === "function") {
      themeMediaQuery.addListener(syncThemeFromSystem);
    }
  }

  const installButton = document.getElementById("install-button");
  const dismissButton = document.getElementById("install-dismiss-button");
  if (installButton) {
    installButton.addEventListener("click", () => {
      void handleInstallClick();
    });
  }
  if (dismissButton) {
    dismissButton.addEventListener("click", () => {
      dismissInstallBanner();
    });
  }

  if (isStandaloneMode()) {
    setInstallBannerState("", false);
    return;
  }

  if (isInstallBannerDismissed()) {
    setInstallBannerState("", false);
    return;
  }

  const isiOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
  if (isiOS) {
    setInstallBannerState("iPhone 请在 Safari 菜单中选择“添加到主屏幕”，再从桌面图标打开。", true);
  }
});
