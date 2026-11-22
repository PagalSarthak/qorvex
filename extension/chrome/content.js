// IDM-style download button over any video (YouTube, Vimeo, news sites, direct mp4...).
(() => {
  if (window.__qvx) return; window.__qvx = true;
  const api = globalThis.browser ?? globalThis.chrome;
  const Q = [["best", "Best quality"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"], ["360", "360p"], ["audio", "Audio only"]];
  let host, btn, menu, mode, hideT, menuOpen = false;

  function build() {
    host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;display:none;top:0;left:0";
    const sh = host.attachShadow({ mode: "closed" });
    sh.innerHTML = `<style>
      button{font:600 12px system-ui,sans-serif;background:#7c5cff;color:#fff;border:0;border-radius:8px;padding:7px 11px;cursor:pointer;box-shadow:0 2px 10px #0007}
      button:hover{background:#6a49f0}
      #m{display:none;margin-top:6px;background:#161b22;border:1px solid #2b3440;border-radius:8px;overflow:hidden;box-shadow:0 6px 18px #000a}
      #m button{display:block;width:100%;text-align:left;background:none;box-shadow:none;border-radius:0;font-weight:500;padding:8px 14px;white-space:nowrap}
      #m button:hover{background:#252c36}</style><button id="b">⬇ Qorvex</button><div id="m"></div>`;
    btn = sh.getElementById("b"); menu = sh.getElementById("m");
    for (const [q, label] of Q) {
      const b = document.createElement("button"); b.textContent = label;
      b.onclick = e => { e.stopPropagation(); send({ url: mode.page, media: true, quality: q }); };
      menu.appendChild(b);
    }
    btn.onclick = e => {
      e.stopPropagation(); e.preventDefault();
      if (mode.direct) return send({ url: mode.direct, referer: location.href });
      menuOpen = !menuOpen; menu.style.display = menuOpen ? "block" : "none";
    };
  }
  function flash(t) { btn.textContent = t; setTimeout(() => (btn.textContent = "⬇ Qorvex"), 1800); }
  async function send(msg) {
    menuOpen = false; menu.style.display = "none"; flash("Sending…");
    try { flash((await api.runtime.sendMessage({ type: "send", ...msg })) ? "✓ Sent to Qorvex" : "Start Qorvex first"); }
    catch { flash("Reload the page"); }
  }
  // Direct file URL -> download as-is. blob:/MSE/HLS (YouTube...) -> resolve the page URL via yt-dlp.
  const modeOf = v => { const s = v.currentSrc || v.src || "";
    return /^https?:/.test(s) && !/\.(m3u8|mpd)(\?|$)/i.test(s) ? { direct: s } : { page: location.href }; };
  function place(v) {
    if (!host) build();
    const r = v.getBoundingClientRect();
    host.style.top = Math.max(8, r.top + 12) + "px"; host.style.left = Math.max(8, r.left + 12) + "px";
    mode = modeOf(v);
    const root = document.fullscreenElement || document.documentElement;   // stay visible in fullscreen
    if (host.parentNode !== root) root.appendChild(host);
    host.style.display = "block";
  }
  const hide = () => { if (host && !menuOpen) host.style.display = "none"; };
  let last = 0;
  document.addEventListener("mousemove", e => {
    const t = performance.now(); if (t - last < 120) return; last = t;
    const els = document.elementsFromPoint(e.clientX, e.clientY);  // players put overlays above <video>
    if (host && els.includes(host)) { clearTimeout(hideT); return; }
    const v = els.find(x => x.tagName === "VIDEO" && x.clientWidth > 240 && x.clientHeight > 135);
    clearTimeout(hideT);
    if (v) { place(v); hideT = setTimeout(hide, 2800); } else hideT = setTimeout(hide, 500);
  }, true);
  document.addEventListener("click", () => { if (menuOpen) { menuOpen = false; menu.style.display = "none"; } }, true);
})();
