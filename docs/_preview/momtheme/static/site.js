/* mssql-ocp-migrate preview theme: theme switch, search (Ctrl+K), copy buttons, on-this-page highlight. */
(function () {
  "use strict";
  var root = document.documentElement;
  var staticBase = document.body.getAttribute("data-static") || "static/";
  var rootPage = document.body.getAttribute("data-root") || "index.html";
  var siteBase = rootPage.replace(/index\.html$/, "");

  /* ---- theme: light -> dark -> system ---- */
  var toggle = document.getElementById("theme-toggle");
  function currentTheme() { return root.dataset.theme || "system"; }
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = { system: "light", light: "dark", dark: "system" }[currentTheme()];
      if (next === "system") { delete root.dataset.theme; } else { root.dataset.theme = next; }
      try { if (next === "system") localStorage.removeItem("mom-theme"); else localStorage.setItem("mom-theme", next); } catch (e) {}
      toggle.setAttribute("aria-label", "Colour theme: " + next + " (click to change)");
    });
  }

  /* ---- copy buttons on code blocks ---- */
  document.querySelectorAll("div.highlight").forEach(function (block) {
    var pre = block.querySelector("pre");
    if (!pre) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.addEventListener("click", function () {
      var text = pre.innerText.replace(/^\$ /gm, "").replace(/^>>> /gm, "");
      var done = function (ok) { btn.textContent = ok ? "Copied" : "Copy failed"; setTimeout(function () { btn.textContent = "Copy"; }, 1500); };
      try { navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); }); }
      catch (e) { done(false); }
    });
    block.appendChild(btn);
  });

  /* ---- highlight the section being read in "On this page" ---- */
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll(".page-toc a[href^='#']"));
  if (tocLinks.length && "IntersectionObserver" in window) {
    var byId = {};
    tocLinks.forEach(function (a) { byId[decodeURIComponent(a.getAttribute("href").slice(1))] = a; });
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting && byId[en.target.id]) {
          tocLinks.forEach(function (a) { a.classList.remove("active"); });
          byId[en.target.id].classList.add("active");
        }
      });
    }, { rootMargin: "-80px 0px -70% 0px" });
    Object.keys(byId).forEach(function (id) { var el = document.getElementById(id); if (el) obs.observe(el); });
  }

  /* ---- search ---- */
  var dialog = document.getElementById("search-dialog");
  var input = document.getElementById("search-input");
  var list = document.getElementById("search-results");
  var openBtn = document.getElementById("search-open");
  var index = null, selected = -1, lastFocus = null;

  function load() {
    if (index) return Promise.resolve(index);
    return fetch(staticBase + "search.json").then(function (r) { return r.json(); }).then(function (d) { index = d; return d; });
  }
  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function mark(s, terms) {
    var out = esc(s);
    terms.forEach(function (t) { if (t.length > 1) out = out.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig"), "<mark>$1</mark>"); });
    return out;
  }
  function search(q) {
    var terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!terms.length) { list.innerHTML = ""; return; }
    var hits = [];
    index.forEach(function (p) {
      var title = p.t.toLowerCase(), heads = p.h.join(" ").toLowerCase(), body = p.x.toLowerCase(), score = 0;
      for (var i = 0; i < terms.length; i++) {
        var t = terms[i], s = 0;
        if (title.indexOf(t) >= 0) s += 10;
        if (heads.indexOf(t) >= 0) s += 4;
        var at = body.indexOf(t);
        if (at >= 0) s += 1 + Math.min(3, body.split(t).length - 1) * 0.5;
        if (!s) { score = 0; break; }
        score += s;
      }
      if (score) hits.push({ p: p, s: score });
    });
    hits.sort(function (a, b) { return b.s - a.s; });
    hits = hits.slice(0, 12);
    if (!hits.length) { list.innerHTML = '<li class="r-empty">No pages match “' + esc(q) + '”.</li>'; return; }
    list.innerHTML = hits.map(function (h) {
      var body = h.p.x, at = body.toLowerCase().indexOf(terms[0]);
      var start = Math.max(0, at - 60);
      if (start > 0) { var sp = body.indexOf(" ", start); if (sp > 0 && sp < at) start = sp + 1; }
      var snip = at >= 0 ? (start > 0 ? "… " : "") + body.slice(start, at + 120) + " …" : body.slice(0, 140) + " …";
      return '<li><a href="' + siteBase + h.p.u + '"><span class="r-title">' + mark(h.p.t, terms) + '</span><span class="r-path">' +
        esc(h.p.s) + '</span><span class="r-snip">' + mark(snip, terms) + "</span></a></li>";
    }).join("");
    selected = -1;
  }
  function open() {
    lastFocus = document.activeElement;
    dialog.hidden = false;
    input.value = "";
    list.innerHTML = "";
    input.focus();
    load().catch(function () { list.innerHTML = '<li class="r-empty">The search index could not be loaded.</li>'; });
  }
  function close() { dialog.hidden = true; if (lastFocus) lastFocus.focus(); }
  if (openBtn) openBtn.addEventListener("click", open);
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); dialog.hidden ? open() : close(); }
    else if (!dialog.hidden && e.key === "Escape") { close(); }
  });
  dialog.addEventListener("click", function (e) { if (e.target === dialog) close(); });
  input.addEventListener("input", function () { if (index) search(input.value); else load().then(function () { search(input.value); }); });
  input.addEventListener("keydown", function (e) {
    var links = list.querySelectorAll("a");
    if (!links.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      selected = (selected + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
      links.forEach(function (a, i) { a.classList.toggle("sel", i === selected); });
      links[selected].scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      window.location.href = links[Math.max(selected, 0)].getAttribute("href");
    }
  });
})();
