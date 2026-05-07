let deferredInstallPrompt = null;

function isStandaloneMode() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
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
  if (!isStandaloneMode()) {
    setInstallBannerState("安装到桌面后，可从独立应用窗口打开并单独开启推送。", true);
  }
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  setInstallBannerState("", false);
});

window.addEventListener("DOMContentLoaded", () => {
  const installButton = document.getElementById("install-button");
  if (installButton) {
    installButton.addEventListener("click", () => {
      void handleInstallClick();
    });
  }

  if (isStandaloneMode()) {
    setInstallBannerState("", false);
    return;
  }

  const isiOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
  if (isiOS) {
    setInstallBannerState("iPhone 请在 Safari 菜单中选择“添加到主屏幕”，再从桌面图标打开。", true);
  }
});
