// Works in Chrome/Edge/Brave (MV3 service worker) and Firefox (MV2 background page).
const api = globalThis.browser ?? globalThis.chrome;
const act = api.action || api.browserAction;
const DEF = { port: 6805, intercept: true, minMB: 0, token: "" };
const cfg = async () => ({ ...DEF, ...(await api.storage.local.get(null)) });
const media = new Map(); // tabId -> sniffed media files
const MAX_MEDIA = 20;

const base = (c) => `http://127.0.0.1:${c.port}`;
const authHeaders = (c) => ({
  "Content-Type": "application/json",
  "X-Qorvex-Extension": "1", // announces this request came from the extension
  ...(c.token ? { "X-Qorvex-Token": c.token } : {}),
});

function badge(text, title, bad) {
  act.setBadgeBackgroundColor({ color: bad ? "#c0392b" : "#35c88a" });
  act.setBadgeText({ text });
  if (title) act.setTitle({ title });
  if (text) setTimeout(() => act.setBadgeText({ text: "" }), 2000);
}

async function request(path, body, timeout = 6000) {
  const c = await cfg();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const r = await fetch(base(c) + path, body === undefined
      ? { headers: authHeaders(c), signal: controller.signal }
      : { method: "POST", headers: authHeaders(c), body: JSON.stringify(body), signal: controller.signal });
    if (r.status === 403) { badge("!", "Qorvex rejected the request — check the extension token in Settings", true); return null; }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return null;
    return data;
  } catch {
    return null; // Qorvex not running -> caller lets the browser handle it
  } finally {
    clearTimeout(timer);
  }
}

async function post(body) {
  const data = await request("/api/add", body);
  if (data) badge("✓", "Sent to Qorvex");
  return !!data;
}

async function sendDirect(url, referer, filename) {
  let cookie = "";
  try { cookie = (await api.cookies.getAll({ url })).map(k => `${k.name}=${k.value}`).join("; "); } catch {}
  return post({ url, cookie, referer: referer || "", filename: filename || "", ua: navigator.userAgent, intercepted: true });
}

const sendMedia = (url, quality = "best") => post({ url, media: true, quality });

async function syncFromServer() {
  const data = await request("/api/state");
  if (!data || !data.settings) return null;
  const patch = {
    intercept: !!data.settings.intercept,
    minMB: Number(data.settings.intercept_min_mb) || 0,
  };
  await api.storage.local.set(patch);
  return { ...data, ...patch };
}

// 1) Take over normal browser downloads (like IDM). If Qorvex is offline the browser downloads as usual.
api.downloads.onCreated.addListener(async item => {
  const c = await cfg(), url = item.finalUrl || item.url;
  if (!c.intercept || !/^https?:/.test(url)) return;
  const size = Number(item.fileSize);
  if (c.minMB && size > 0 && size < c.minMB * 1048576) return;
  if (await sendDirect(url, item.referrer, item.filename)) {
    try { await api.downloads.cancel(item.id); await api.downloads.erase({ id: item.id }); } catch {}
  }
});

// 2) Right-click menus
function menus() {
  api.contextMenus.removeAll(() => {
    api.contextMenus.create({ id: "qvx-direct", title: "Download with Qorvex", contexts: ["link", "video", "audio"] });
    api.contextMenus.create({ id: "qvx-media", title: "Download video with Qorvex (YouTube etc.)", contexts: ["link", "video", "page"] });
    api.contextMenus.create({ id: "qvx-open", title: "Open Qorvex", contexts: ["action"] });
  });
}
api.runtime.onInstalled.addListener(menus);
api.runtime.onStartup.addListener(menus);

api.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "qvx-open") {
    const c = await cfg();
    return api.tabs.create({ url: base(c) + "/" });
  }
  if (info.menuItemId === "qvx-media") return sendMedia(info.linkUrl || tab.url);
  const u = info.linkUrl || info.srcUrl || "";
  /^https?:/.test(u) ? sendDirect(u, tab?.url) : sendMedia(tab.url); // blob: video -> resolve the page instead
});

// 3) Sniff direct media files for the popup list
async function getMedia(tabId) {
  if (media.has(tabId)) return media.get(tabId);
  const key = "m" + tabId;
  const stored = api.storage.session ? (await api.storage.session.get(key))[key] : null;
  return stored || [];
}
api.webRequest.onHeadersReceived.addListener(async d => {
  if (d.tabId < 0 || d.statusCode >= 300) return;
  const header = n => d.responseHeaders.find(x => x.name.toLowerCase() === n)?.value || "";
  const type = header("content-type"), size = +header("content-length") || 0;
  if (!/^(video|audio)\//.test(type) || size < 1048576) return;
  const list = await getMedia(d.tabId);
  if (list.some(m => m.url === d.url)) return;
  list.unshift({ url: d.url, type, size });
  const trimmed = list.slice(0, MAX_MEDIA);
  media.set(d.tabId, trimmed);
  api.storage.session?.set({ ["m" + d.tabId]: trimmed });
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);
api.tabs.onRemoved.addListener(id => media.delete(id));

api.runtime.onMessage.addListener((m, sender, reply) => {
  if (m.type === "send") {
    (m.media ? sendMedia(m.url, m.quality) : sendDirect(m.url, m.referer || sender.tab?.url)).then(reply);
  } else if (m.type === "media") {
    getMedia(m.tabId).then(reply);
  } else if (m.type === "sync") {
    syncFromServer().then(reply);
  } else if (m.type === "ping") {
    request("/api/state").then(d => reply(d ? { ok: true, version: d.version, settings: d.settings } : { ok: false }));
  } else return false;
  return true;
});
