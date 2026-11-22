const api = globalThis.browser ?? globalThis.chrome;
const $ = id => document.getElementById(id);
const mb = n => (n / 1048576).toFixed(1) + " MB";

(async () => {
  const c = { port: 6805, intercept: true, minMB: 0, token: "", ...(await api.storage.local.get(null)) };
  $("port").value = c.port;
  $("intercept").checked = c.intercept;
  $("minMB").value = c.minMB || 0;
  $("token").value = c.token || "";

  $("save").onclick = async () => {
    await api.storage.local.set({
      port: Math.min(65535, Math.max(1024, +$("port").value || 6805)),
      intercept: $("intercept").checked,
      minMB: Math.max(0, Number($("minMB").value) || 0),
      token: $("token").value.trim(),
    });
    $("msg").textContent = "Saved";
    ping();
  };

  const ping = async () => {
    const r = await api.runtime.sendMessage({ type: "ping" });
    if (r && r.ok) {
      $("dot").className = "dot on";
      $("ver").textContent = "v" + r.version;
      $("status").innerHTML = "<small style='color:#6fe3b0'>connected</small>";
    } else {
      $("dot").className = "dot bad";
      $("ver").textContent = "";
      $("status").innerHTML = "<small>Qorvex is not running</small>";
    }
  };

  $("sync").onclick = async () => {
    const r = await api.runtime.sendMessage({ type: "sync" });
    if (!r) return $("msg").textContent = "Start Qorvex first";
    $("intercept").checked = r.intercept;
    $("minMB").value = r.minMB;
    $("msg").textContent = "Synced from Qorvex";
    ping();
  };

  $("open").onclick = async () => {
    const port = +$("port").value || 6805;
    api.tabs.create({ url: `http://127.0.0.1:${port}/` });
  };

  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  $("page").onclick = async () => {
    const ok = await api.runtime.sendMessage({ type: "send", url: tab.url, media: true, quality: $("q").value });
    $("pmsg").textContent = ok ? "Sent ✓" : "Start Qorvex first";
  };

  const list = (await api.runtime.sendMessage({ type: "media", tabId: tab.id })) || [];
  if (list.length) $("media").innerHTML = "";
  for (const m of list) {
    const row = document.createElement("div");
    row.className = "m";
    const name = document.createElement("span");
    let label = m.type;
    try { label = decodeURIComponent(new URL(m.url).pathname.split("/").pop() || m.type); } catch {}
    name.textContent = label;
    name.title = m.url;
    const size = document.createElement("small");
    size.textContent = mb(m.size);
    const send = document.createElement("button");
    send.textContent = "⬇";
    send.onclick = async () => {
      send.textContent = (await api.runtime.sendMessage({ type: "send", url: m.url, referer: tab.url })) ? "✓" : "!";
    };
    row.append(name, size, send);
    $("media").appendChild(row);
  }
  ping();
})();
