async function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

function formatPushError(error, fallbackMessage) {
  const message = String(error?.message || fallbackMessage || "推送操作失败");
  const name = String(error?.name || "");
  if (/Registration failed - push service error/i.test(message)) {
    return "浏览器创建系统推送通道失败。请先关闭 VPN 后重试，并确认 Chrome 与 Google Play 服务可正常连接推送服务。";
  }
  if (/AbortError/i.test(name) || /aborted/i.test(message)) {
    return "浏览器中断了推送订阅。请稍后重试。";
  }
  if (/NotAllowedError/i.test(name) || /denied/i.test(message)) {
    return "通知权限未授予，或系统限制了浏览器通知。请先检查系统通知设置。";
  }
  if (/InvalidStateError/i.test(name)) {
    return "当前浏览器推送状态异常。请删除主屏幕图标后重新安装，再次开启推送。";
  }
  return message;
}

async function getExistingSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return null;
  }
  const directRegistration = await navigator.serviceWorker.getRegistration();
  if (directRegistration) {
    const directSubscription = await directRegistration.pushManager.getSubscription();
    if (directSubscription) {
      return directSubscription;
    }
  }
  const readyRegistration = await navigator.serviceWorker.ready.catch(() => null);
  if (readyRegistration) {
    const readySubscription = await readyRegistration.pushManager.getSubscription();
    if (readySubscription) {
      return readySubscription;
    }
  }
  const registrations = await navigator.serviceWorker.getRegistrations().catch(() => []);
  for (const registration of registrations) {
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      return subscription;
    }
  }
  return null;
}

function isStandaloneMode() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function isIOS() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}

function getNotificationPermissionState() {
  if (!("Notification" in window)) {
    return "unsupported";
  }
  return Notification.permission;
}

async function ensureServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    throw new Error("当前浏览器不支持 Service Worker");
  }
  try {
    const version = window.DINGPAN_ASSET_VERSION ? `?v=${encodeURIComponent(window.DINGPAN_ASSET_VERSION)}` : "";
    const registration = await navigator.serviceWorker.register(`/sw.js${version}`);
    await registration.update();
    return await navigator.serviceWorker.ready;
  } catch (error) {
    throw new Error(`Service Worker 注册失败：${formatPushError(error, "浏览器拒绝了 Service Worker 注册")}`);
  }
}

async function ensureNotificationPermission() {
  if (!("Notification" in window)) {
    throw new Error("当前浏览器不支持通知权限");
  }
  if (Notification.permission === "granted") {
    return;
  }
  if (Notification.permission === "denied") {
    throw new Error("浏览器通知权限已被拒绝，请先在浏览器设置里手动开启");
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("用户未授予通知权限");
  }
}

async function subscribePush() {
  if (isIOS() && !isStandaloneMode()) {
    throw new Error("iPhone 请先把站点添加到主屏幕，再从桌面图标打开后开启推送");
  }
  await ensureNotificationPermission();
  const registration = await ensureServiceWorker();
  const vapidResponse = await fetch("/api/push/vapid-key");
  const vapidData = await vapidResponse.json().catch(() => ({}));
  if (!vapidResponse.ok) {
    throw new Error(vapidData.detail || "无法获取 VAPID 公钥");
  }
  let subscription;
  try {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: await urlBase64ToUint8Array(vapidData.public_key)
    });
  } catch (error) {
    throw new Error(`浏览器订阅失败：${formatPushError(error, "浏览器未能创建本机推送订阅")}`);
  }
  const response = await fetch("/api/push/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`服务器保存订阅失败：${data.detail || "订阅推送失败"}`);
  }
  return subscription;
}

async function unsubscribePush() {
  const subscription = await getExistingSubscription();
  if (!subscription) {
    return;
  }
  await fetch("/api/push/unsubscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint: subscription.endpoint })
  });
  await subscription.unsubscribe();
}

async function fetchPushStatus() {
  const response = await fetch("/api/push/status");
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "无法读取推送状态");
  }
  return data;
}

async function getPushDiagnostics() {
  const serverStatus = await fetchPushStatus().catch(() => null);
  const subscription = await getExistingSubscription().catch(() => null);
  return {
    standalone: isStandaloneMode(),
    ios: isIOS(),
    secure_context: window.isSecureContext,
    notification_permission: getNotificationPermissionState(),
    local_subscription: Boolean(subscription),
    service_worker_supported: "serviceWorker" in navigator,
    push_manager_supported: "PushManager" in window,
    server_status: serverStatus,
  };
}

window.DingPanPush = {
  getExistingSubscription,
  getPushDiagnostics,
  getNotificationPermissionState,
  isStandaloneMode,
  subscribePush,
  unsubscribePush,
  ensureServiceWorker,
  ensureNotificationPermission,
  fetchPushStatus
};
