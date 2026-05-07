async function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

async function getExistingSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return null;
  }
  const registration = await navigator.serviceWorker.getRegistration("/sw.js");
  if (!registration) {
    return null;
  }
  return registration.pushManager.getSubscription();
}

async function ensureServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    throw new Error("当前浏览器不支持 Service Worker");
  }
  return navigator.serviceWorker.register("/sw.js");
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
  await ensureNotificationPermission();
  const registration = await ensureServiceWorker();
  const vapidResponse = await fetch("/api/push/vapid-key");
  const vapidData = await vapidResponse.json().catch(() => ({}));
  if (!vapidResponse.ok) {
    throw new Error(vapidData.detail || "无法获取 VAPID 公钥");
  }
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: await urlBase64ToUint8Array(vapidData.public_key)
  });
  const response = await fetch("/api/push/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "订阅推送失败");
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

window.DingPanPush = {
  getExistingSubscription,
  subscribePush,
  unsubscribePush,
  ensureServiceWorker,
  ensureNotificationPermission,
  fetchPushStatus
};
