self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

function toAbsoluteUrl(path) {
  try {
    return new URL(path || "/dashboard", self.location.origin).toString();
  } catch (error) {
    return `${self.location.origin}/dashboard`;
  }
}

function buildNotificationOptions(payload) {
  const timestamp = Number(payload.timestamp || Date.now());
  const tag = payload.tag || "dingpan-report";
  const url = toAbsoluteUrl(payload.url);
  const stockCode = payload.stock_code || "";
  const tradeDate = payload.trade_date || "";

  return {
    body: payload.body,
    icon: "/static/icons/icon-512.png",
    badge: "/static/icons/icon-192.png",
    image: payload.image || undefined,
    tag,
    renotify: Boolean(payload.renotify),
    requireInteraction: Boolean(payload.require_interaction),
    silent: false,
    vibrate: [120, 40, 120],
    timestamp,
    lang: "zh-CN",
    dir: "ltr",
    actions: [
      { action: "open-report", title: "查看日报" },
      { action: "open-dashboard", title: "打开面板" },
    ],
    data: {
      url,
      tag,
      stock_code: stockCode,
      trade_date: tradeDate,
      timestamp,
    },
  };
}

self.addEventListener("push", (event) => {
  let payload = {
    title: "盯盘侠",
    body: "新的日报已生成",
    url: "/dashboard",
    tag: "dingpan-report",
    timestamp: Date.now(),
    renotify: true,
  };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch (error) {
      payload.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, buildNotificationOptions(payload))
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = toAbsoluteUrl(
    event.action === "open-dashboard"
      ? "/dashboard"
      : event.notification.data?.url || "/dashboard"
  );
  event.waitUntil((async () => {
    const windowClients = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windowClients) {
      if (client.url.startsWith(self.location.origin) && "focus" in client) {
        await client.focus();
        if ("navigate" in client && client.url !== targetUrl) {
          await client.navigate(targetUrl);
        }
        return;
      }
    }
    await clients.openWindow(targetUrl);
  })());
});

self.addEventListener("notificationclose", (event) => {
  const closedTag = event.notification?.data?.tag || "unknown";
  console.info("[dingpan-sw] notification closed:", closedTag);
});
