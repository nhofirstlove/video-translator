/* Video Translator editor: page shell, video player, subtitle overlay, timeline and line list.
   No dependencies. All data is inserted with textContent / value, never as HTML.
   Exposes window.Editor (see docs/editor-api.md). editor_audio.js loads after this file. */
(function () {
  "use strict";

  var EDITABLE = { awaiting_review: true, done: true, paused: true, failed: true };
  var STATUS_LABELS = {
    pending: "Not started", running: "Running", awaiting_review: "Needs your review",
    paused: "Paused", done: "Finished", failed: "Failed", queued: "Waiting"
  };
  var FLAG_LABELS = {
    too_long: ["Too long", "The Vietnamese text is too long for this time slot; the voice would have to be sped up."],
    truncated: ["Truncated", "The voice clip had to be cut to fit this time slot."],
    needs_realign: ["Needs realign", "The times changed after the voice was made. Apply to refit the voice."]
  };
  var NUDGE = 0.1;
  var SAVE_DELAY = 600;
  var STATE_LABELS = { dirty: "Unsaved", saving: "Saving...", saved: "Saved", error: "Not saved" };

  // ---------- small helpers ----------
  function $(id) { return document.getElementById(id); }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    props = props || {};
    Object.keys(props).forEach(function (key) {
      var value = props[key];
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else if (value !== false && value != null) node.setAttribute(key, value);
    });
    (children || []).forEach(function (child) {
      if (child != null) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function pad(n, width) {
    var s = String(n);
    while (s.length < width) s = "0" + s;
    return s;
  }

  function round3(x) { return Math.round(x * 1000) / 1000; }

  function formatTime(sec) {
    if (typeof sec !== "number" || !isFinite(sec) || sec < 0) sec = 0;
    var ms = Math.round(sec * 1000);
    var h = Math.floor(ms / 3600000); ms -= h * 3600000;
    var m = Math.floor(ms / 60000); ms -= m * 60000;
    var s = Math.floor(ms / 1000); ms -= s * 1000;
    return (h ? h + ":" + pad(m, 2) : pad(m, 2)) + ":" + pad(s, 2) + "." + pad(ms, 3);
  }

  /* Accepts "75.4", "75,4", "1:15.4", "1:02:03.5", "75.4s". Returns NaN when it cannot be read. */
  function parseTime(str) {
    if (typeof str === "number") return isFinite(str) && str >= 0 ? str : NaN;
    var s = String(str == null ? "" : str).replace(/\s+/g, "").replace(/s$/i, "");
    if (!s) return NaN;
    if (s.indexOf(".") < 0 && (s.match(/,/g) || []).length === 1) s = s.replace(",", ".");
    var parts = s.split(":");
    if (parts.length > 3) return NaN;
    var total = 0;
    for (var i = 0; i < parts.length; i++) {
      var last = i === parts.length - 1;
      if (!(last ? /^(\d+\.?\d*|\.\d+)$/ : /^\d+$/).test(parts[i])) return NaN;
      total = total * 60 + parseFloat(parts[i]);
    }
    return isFinite(total) ? total : NaN;
  }

  function charCount(text) { return Array.from(text || "").length; }

  function readPref(key, fallback) {
    try {
      var v = window.localStorage.getItem("vt_editor_" + key);
      return v === null ? fallback : v === "1";
    } catch (e) { return fallback; }
  }
  function writePref(key, value) {
    try { window.localStorage.setItem("vt_editor_" + key, value ? "1" : "0"); } catch (e) { /* ignore */ }
  }

  // ---------- state ----------
  var jobId = location.pathname.replace(/\/+$/, "").split("/").pop();
  var video = $("ed-video");
  var state = { job: null, duration: 0, media: {}, segments: [], audio: null, blur: null, budget: null };
  var listeners = {};
  var rows = {};          // segment index -> row object
  var blocks = {};        // segment index -> timeline block element
  var segMap = {};        // segment index -> segment object (same objects as state.segments)
  var saved = {};         // segment index -> last values known to be stored on the server
  var pend = {};          // segment index -> pending save bookkeeping
  var activeIndex = -1;   // line used for highlight and keyboard actions
  var curIndex = -1;      // line under the playhead (subtitle overlay)
  var editable = true;
  var firstLoad = true;
  var dubOn = false;
  var dubVersion = 0;
  var srcKey = null;
  var pollTimer = null;
  var rafId = 0;
  var panelMounted = false;
  var overlayKey = "";
  var growSupported = false;
  try { growSupported = !!(window.CSS && CSS.supports && CSS.supports("field-sizing", "content")); } catch (e) { /* ignore */ }

  // ---------- events ----------
  function on(name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
    return function () {
      listeners[name] = (listeners[name] || []).filter(function (f) { return f !== fn; });
    };
  }
  function emit(name, arg) {
    (listeners[name] || []).slice().forEach(function (fn) {
      try { fn(arg); } catch (e) { if (window.console) console.error("Editor listener for " + name + " failed", e); }
    });
  }

  // ---------- toast ----------
  function toast(message, kind) {
    var box = $("ed-toasts");
    if (!box) return;
    kind = kind === "error" || kind === "ok" ? kind : "info";
    var node = el("div", { class: "ed-toast " + kind, text: String(message) });
    box.appendChild(node);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, kind === "error" ? 7000 : 3500);
  }

  // ---------- api ----------
  function resolvePath(path) {
    path = String(path || "");
    if (path.indexOf("/api/") === 0) return path;
    if (path.charAt(0) !== "/") path = "/" + path;
    return "/api/jobs/" + encodeURIComponent(jobId) + path;
  }

  function detailText(body, status) {
    var d = body && body.detail;
    if (Array.isArray(d)) {
      d = d.map(function (x) { return x && x.msg ? x.msg : String(x); }).join("; ");
    } else if (d && typeof d === "object") {
      d = d.message || JSON.stringify(d);
    }
    return d ? String(d) : "The request failed (" + status + ").";
  }

  function api(method, path, body) {
    var init = { method: method || "GET", headers: { Accept: "application/json" } };
    if (body !== undefined && body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(resolvePath(path), init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (!res.ok) {
          var err = new Error(detailText(data, res.status));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }, function () {
      var err = new Error("Could not reach the server. Check that the app is still running.");
      err.status = 0;
      throw err;
    });
  }

  // ---------- segment lookups ----------
  function indexSegments() {
    segMap = {};
    state.segments.forEach(function (s) { segMap[s.index] = s; });
  }
  function seg(index) { return segMap[index] || null; }

  /* Segment index of the line that contains time t (last line starting at or before t), or -1. */
  function indexAt(t) {
    var list = state.segments;
    var lo = 0, hi = list.length - 1, found = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (list[mid].start <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (found < 0) return -1;
    return t < list[found].end ? list[found].index : -1;
  }

  function position(index) { // position in state.segments
    for (var i = 0; i < state.segments.length; i++) if (state.segments[i].index === index) return i;
    return -1;
  }

  function totalDuration() {
    if (state.duration > 0) return state.duration;
    if (video && isFinite(video.duration) && video.duration > 0) return video.duration;
    var last = state.segments.length ? state.segments[state.segments.length - 1].end : 0;
    return last || 1;
  }

  // ---------- video + media ----------
  function mediaUrl(kind) { return "/api/jobs/" + encodeURIComponent(jobId) + "/media/" + kind; }

  function setVideoSource() {
    var media = state.media || {};
    var useDub = dubOn && !!media.dub_video;
    var key = useDub ? "dub:" + dubVersion : (media.video ? "video" : "none");
    if (key === srcKey) return;
    var first = srcKey === null;
    srcKey = key;
    var t = video.currentTime || 0;
    var playing = !video.paused && !video.ended;
    $("ed-novideo").hidden = key !== "none";
    video.hidden = key === "none";
    if (key === "none") {
      video.removeAttribute("src");
      video.load();
      return;
    }
    video.src = useDub ? mediaUrl("dub") + "?v=" + dubVersion : mediaUrl("video");
    if (!first && (t > 0 || playing)) {
      video.addEventListener("loadedmetadata", function restore() {
        video.removeEventListener("loadedmetadata", restore);
        try { video.currentTime = Math.min(t, video.duration || t); } catch (e) { /* ignore */ }
        if (playing) { var p = video.play(); if (p && p.catch) p.catch(function () {}); }
      });
    }
  }

  function seekTo(t) {
    if (typeof t !== "number" || !isFinite(t)) return;
    t = Math.max(0, t);
    if (isFinite(video.duration) && video.duration > 0) t = Math.min(t, video.duration);
    try { video.currentTime = t; } catch (e) { /* no media yet */ }
    tick();
  }

  function seekToLine(index) {
    var s = seg(index);
    if (!s) return;
    // a hair past the start so the frame lookup lands inside this line, not the previous one
    seekTo(s.start + 0.001);
  }

  // ---------- playback tick, overlay, timeline head ----------
  function updatePlayhead(t) {
    var dur = totalDuration();
    var head = $("ed-tl-head");
    head.style.left = Math.min(100, Math.max(0, (t / dur) * 100)) + "%";
    $("ed-clock").textContent = formatTime(t) + " / " + formatTime(dur);
    var tl = $("ed-timeline");
    tl.setAttribute("aria-valuemax", String(Math.round(dur * 10) / 10));
    tl.setAttribute("aria-valuenow", String(Math.round(t * 10) / 10));
    tl.setAttribute("aria-valuetext", formatTime(t));
  }

  function updateOverlay() {
    var s = curIndex >= 0 ? seg(curIndex) : null;
    var showSubs = $("ed-opt-subs").checked;
    var showZh = $("ed-opt-zh").checked;
    var vi = s && showSubs ? (s.text_vi || "") : "";
    var zh = s && showSubs && showZh ? (s.text_zh || "") : "";
    var key = curIndex + "|" + vi + "|" + zh;
    if (key === overlayKey) return;
    overlayKey = key;
    var viEl = $("ed-sub-vi"), zhEl = $("ed-sub-zh");
    viEl.textContent = vi; viEl.hidden = !vi;
    zhEl.textContent = zh; zhEl.hidden = !zh;
  }

  function tick() {
    var t = video.currentTime || 0;
    updatePlayhead(t);
    var idx = indexAt(t);
    if (idx !== curIndex) {
      curIndex = idx;
      if (idx >= 0 && idx !== activeIndex) setActive(idx, { scroll: true });
    }
    updateOverlay();
  }

  function loop() {
    tick();
    if (!video.paused && !video.ended) rafId = requestAnimationFrame(loop); else rafId = 0;
  }
  function startLoop() { if (!rafId) rafId = requestAnimationFrame(loop); }

  // ---------- active line + scrolling ----------
  function setActive(index, opts) {
    opts = opts || {};
    if (index === activeIndex && !opts.force) return;
    var prev = rows[activeIndex];
    if (prev) { prev.root.classList.remove("is-active"); prev.root.removeAttribute("aria-current"); }
    if (blocks[activeIndex]) blocks[activeIndex].classList.remove("is-active");
    activeIndex = index;
    var row = rows[index];
    if (row) { row.root.classList.add("is-active"); row.root.setAttribute("aria-current", "true"); }
    if (blocks[index]) blocks[index].classList.add("is-active");
    emit("active-changed", index);
    if (row && opts.scroll && (opts.force || $("ed-opt-follow").checked || opts.always)) scrollRowIntoView(row.root, !!opts.always);
  }

  function scrollRowIntoView(node, always) {
    var list = $("ed-lines");
    // do not yank the list away while the user is typing in it (unless they navigated with the keyboard)
    var focused = document.activeElement;
    if (!always && focused && list.contains(focused) && /^(TEXTAREA|INPUT)$/.test(focused.tagName)) return;
    var scrolls = window.getComputedStyle(list).overflowY === "auto";
    if (scrolls) {
      var top = node.offsetTop;
      var bottom = top + node.offsetHeight;
      if (top < list.scrollTop + 4) list.scrollTop = Math.max(0, top - 8);
      else if (bottom > list.scrollTop + list.clientHeight - 4) list.scrollTop = bottom - list.clientHeight + 8;
    } else {
      var rect = node.getBoundingClientRect();
      var sticky = $("ed-player").getBoundingClientRect();
      var head = Math.max(0, sticky.bottom) + 8;
      if (rect.top < head || rect.bottom > window.innerHeight - 8) {
        window.scrollTo(0, window.pageYOffset + rect.top - head);
      }
    }
  }

  // ---------- timeline ----------
  function blockClass(s) {
    var flags = s.flags || [];
    if (flags.indexOf("too_long") >= 0 || flags.indexOf("truncated") >= 0) return "bad";
    if (s.audio_stale || flags.indexOf("needs_realign") >= 0) return "stale";
    return "ok";
  }

  function placeBlock(node, s) {
    var dur = totalDuration();
    node.style.left = (s.start / dur) * 100 + "%";
    node.style.width = Math.max(0.2, ((s.end - s.start) / dur) * 100) + "%";
    node.className = "ed-tl-block " + blockClass(s) + (s.index === activeIndex ? " is-active" : "");
    node.setAttribute("title", "#" + (s.index + 1) + "  " + formatTime(s.start) + " - " + formatTime(s.end) + "\n" + (s.text_vi || ""));
  }

  function renderTimeline() {
    var track = $("ed-tl-track");
    track.textContent = "";
    blocks = {};
    var frag = document.createDocumentFragment();
    state.segments.forEach(function (s) {
      var b = el("div", { "data-index": String(s.index) });
      placeBlock(b, s);
      blocks[s.index] = b;
      frag.appendChild(b);
    });
    track.appendChild(frag);
  }

  function wireTimeline() {
    var tl = $("ed-timeline");
    tl.addEventListener("click", function (e) {
      var block = e.target.closest ? e.target.closest(".ed-tl-block") : null;
      if (block) {
        var index = Number(block.getAttribute("data-index"));
        setActive(index, { scroll: true, always: true });
        seekToLine(index);
        return;
      }
      var rect = $("ed-tl-track").getBoundingClientRect();
      if (rect.width <= 0) return;
      seekTo(((e.clientX - rect.left) / rect.width) * totalDuration());
    });
    tl.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        seekTo((video.currentTime || 0) + (e.key === "ArrowRight" ? 5 : -5));
      }
    });
  }

  // ---------- rows ----------
  function setRowControlsDisabled(row, disabled) {
    row.ctl.forEach(function (c) { c.disabled = disabled; });
  }

  function renderFlags(row, s) {
    var box = row.flagsEl;
    box.textContent = "";
    var flags = (s.flags || []).slice();
    flags.forEach(function (f) {
      var info = FLAG_LABELS[f] || [f.replace(/_/g, " "), ""];
      box.appendChild(el("span", { class: "flag " + (f === "too_long" || f === "truncated" ? "bad" : "warn"), text: info[0], title: info[1] }));
    });
    if (s.audio_stale) {
      box.appendChild(el("span", { class: "flag warn", text: "Voice out of date", title: "The text changed after the voice was made. Create the voice again for this line." }));
    }
  }

  function updateRowView(s) {
    var row = rows[s.index];
    if (!row) return;
    var d = document.activeElement;
    if (d !== row.startIn) row.startIn.value = formatTime(s.start);
    if (d !== row.endIn) row.endIn.value = formatTime(s.end);
    row.durEl.textContent = (Math.round((s.end - s.start) * 10) / 10).toFixed(1) + " s";
    if (d !== row.vi && row.vi.value !== (s.text_vi || "")) { row.vi.value = s.text_vi || ""; growOne(row.vi); }
    row.charsEl.textContent = charCount(s.text_vi) + " chars";
    renderFlags(row, s);
    row.root.classList.toggle("has-bad", (s.flags || []).indexOf("too_long") >= 0 || (s.flags || []).indexOf("truncated") >= 0);
    row.root.classList.toggle("is-stale", !!s.audio_stale || (s.flags || []).indexOf("needs_realign") >= 0);
    if (blocks[s.index]) placeBlock(blocks[s.index], s);
  }

  function growOne(ta) {
    if (growSupported) return;
    ta.style.height = "auto";
    ta.style.height = (ta.scrollHeight + 2) + "px";
  }
  function growAll() {
    if (growSupported) return;
    var list = Object.keys(rows).map(function (k) { return rows[k].vi; });
    list.forEach(function (ta) { ta.style.height = "auto"; });
    var heights = list.map(function (ta) { return ta.scrollHeight; });
    list.forEach(function (ta, i) { ta.style.height = (heights[i] + 2) + "px"; });
  }

  function setRowState(index, st) {
    var row = rows[index];
    if (!row) return;
    var p = pend[index] || (pend[index] = newPend());
    clearTimeout(p.stateTimer);
    row.stateEl.textContent = st ? STATE_LABELS[st] : "";
    row.stateEl.setAttribute("data-state", st || "");
    if (st === "saved") {
      p.stateTimer = setTimeout(function () {
        if (row.stateEl.getAttribute("data-state") === "saved") setRowState(index, "");
      }, 2200);
    }
  }

  function showRowError(index, message) {
    var row = rows[index];
    if (!row) return;
    row.errEl.textContent = message || "";
    row.root.classList.toggle("has-error", !!message);
  }

  function buildTimeGroup(index, field, label) {
    var id = "ed-" + field + "-" + index;
    var input = el("input", {
      id: id, class: "time-input", type: "text", inputmode: "decimal", autocomplete: "off",
      spellcheck: "false", "aria-label": label + " time of line " + (index + 1) + ". Minutes:seconds or plain seconds."
    });
    var minus = el("button", { type: "button", class: "btn tiny", text: "−", "aria-label": label + " 0.1 seconds earlier", title: label + " -0.1 s" });
    var plus = el("button", { type: "button", class: "btn tiny", text: "+", "aria-label": label + " 0.1 seconds later", title: label + " +0.1 s" });
    var now = el("button", { type: "button", class: "btn tiny now", text: "Now", "aria-label": "Set " + label.toLowerCase() + " of line " + (index + 1) + " to the current video time", title: "Set " + label.toLowerCase() + " to the current video time" });
    var group = el("div", { class: "tg", role: "group", "aria-label": label + " of line " + (index + 1) }, [
      el("span", { class: "tg-label", text: label, "aria-hidden": "true" }), minus, input, plus, now
    ]);
    minus.addEventListener("click", function () { nudge(index, field, -NUDGE); });
    plus.addEventListener("click", function () { nudge(index, field, NUDGE); });
    now.addEventListener("click", function () { commitTime(index, field, video.currentTime || 0); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); commitInput(index, field, input); }
      else if (e.key === "Escape") { var s = seg(index); if (s) input.value = formatTime(s[field]); input.blur(); }
    });
    input.addEventListener("change", function () { commitInput(index, field, input); });
    return { group: group, input: input, buttons: [minus, plus, now] };
  }

  function renderRow(s) {
    var index = s.index;
    var start = buildTimeGroup(index, "start", "Start");
    var end = buildTimeGroup(index, "end", "End");
    var durEl = el("span", { class: "line-dur muted", title: "Length of the line" });
    var stateEl = el("span", { class: "line-state", "data-state": "" });
    var vi = el("textarea", {
      class: "line-vi", lang: "vi", rows: "2", spellcheck: "false",
      "aria-label": "Vietnamese text of line " + (index + 1)
    });
    vi.value = s.text_vi || "";
    var zhEl = el("p", { class: "line-zh zh", lang: "zh", text: s.text_zh || "" });
    var flagsEl = el("span", { class: "line-flags" });
    var charsEl = el("span", { class: "line-chars muted small-text" });
    var errEl = el("p", { class: "line-error", role: "alert" });
    var audioEl = el("div", { class: "line-audio", "data-index": String(index) });
    var root = el("article", { class: "line", id: "line-" + index, "data-index": String(index), "aria-label": "Line " + (index + 1) }, [
      el("div", { class: "line-head" }, [
        el("span", { class: "line-idx", text: "#" + (index + 1) }),
        start.group, end.group, stateEl
      ]),
      zhEl, vi,
      el("div", { class: "line-meta" }, [durEl, flagsEl, charsEl]),
      errEl, audioEl
    ]);
    var row = {
      root: root, startIn: start.input, endIn: end.input, vi: vi, durEl: durEl, stateEl: stateEl,
      flagsEl: flagsEl, charsEl: charsEl, errEl: errEl, audioEl: audioEl,
      ctl: [start.input, end.input, vi].concat(start.buttons, end.buttons)
    };

    vi.addEventListener("input", function () { onTextInput(index, vi); });
    vi.addEventListener("blur", function () { onTextBlur(index, vi); });
    vi.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        flush(index).then(function () { goToLine(position(index) + 1, true); });
      }
    });
    root.addEventListener("focusin", function () { if (activeIndex !== index) setActive(index); });
    root.addEventListener("mousedown", function (e) { root._wasFocused = e.target === vi && document.activeElement === vi; });
    root.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest && t.closest("button, input, select, a, label, .line-audio")) { setActive(index); return; }
      if (t === vi && root._wasFocused) return; // already editing: do not jump the video on every caret click
      setActive(index);
      seekToLine(index);
    });
    if (!editable) setRowControlsDisabled(row, true);
    rows[index] = row;
    updateRowView(s);
    mountRowAudio(row, s);
    return row;
  }

  function mountRowAudio(row, s) {
    if (window.EditorAudio && typeof window.EditorAudio.mountLine === "function") {
      try { window.EditorAudio.mountLine(row.audioEl, s); } catch (e) { if (window.console) console.error(e); }
    }
  }

  function renderLines() {
    var list = $("ed-lines");
    var focusInfo = null;
    var f = document.activeElement;
    if (f && f.classList && f.classList.contains("line-vi") && list.contains(f)) {
      var r = f.closest(".line");
      focusInfo = { index: Number(r.getAttribute("data-index")), start: f.selectionStart, end: f.selectionEnd };
    }
    list.textContent = "";
    rows = {};
    var frag = document.createDocumentFragment();
    state.segments.forEach(function (s) { frag.appendChild(renderRow(s).root); });
    if (!state.segments.length) frag.appendChild(el("p", { class: "muted", text: "This job has no lines yet." }));
    list.appendChild(frag);
    growAll();
    $("ed-count").textContent = "(" + state.segments.length + ")";
    if (activeIndex >= 0 && rows[activeIndex]) {
      rows[activeIndex].root.classList.add("is-active");
      rows[activeIndex].root.setAttribute("aria-current", "true");
    }
    if (focusInfo && rows[focusInfo.index]) {
      var ta = rows[focusInfo.index].vi;
      ta.focus({ preventScroll: true });
      try { ta.setSelectionRange(focusInfo.start, focusInfo.end); } catch (e) { /* ignore */ }
    }
  }

  // ---------- editing: saving ----------
  function newPend() { return { fields: {}, timer: null, inflight: false, promise: null, stateTimer: null }; }

  function scheduleSave(index, fields, delay) {
    var p = pend[index] || (pend[index] = newPend());
    Object.keys(fields).forEach(function (k) { p.fields[k] = fields[k]; });
    clearTimeout(p.timer);
    p.timer = null;
    showRowError(index, "");
    if (delay > 0) {
      setRowState(index, "dirty");
      p.timer = setTimeout(function () { p.timer = null; flush(index); }, delay);
      return Promise.resolve();
    }
    return flush(index);
  }

  function flush(index) {
    var p = pend[index];
    if (!p) return Promise.resolve();
    clearTimeout(p.timer);
    p.timer = null;
    if (p.inflight) return p.promise;
    var keys = Object.keys(p.fields);
    if (!keys.length) return Promise.resolve();
    var body = p.fields;
    p.fields = {};
    p.inflight = true;
    setRowState(index, "saving");
    p.promise = api("PATCH", "/segments/" + index, body).then(function (res) {
      onSaved(index, res);
    }, function (err) {
      onSaveFailed(index, body, err);
    }).then(function () {
      p.inflight = false;
      if (Object.keys(p.fields).length && !p.timer) return flush(index);
      if (!p.timer && rows[index] && rows[index].stateEl.getAttribute("data-state") === "saving") setRowState(index, "saved");
      return null;
    });
    return p.promise;
  }

  function flushAll() {
    return Promise.all(Object.keys(pend).map(function (k) { return flush(Number(k)); }));
  }

  function onSaved(index, res) {
    if (!res || typeof res.index !== "number") return;
    showRowError(index, "");
    updateSegment(res);
    var row = rows[index];
    if (row && row.stateEl.getAttribute("data-state") === "saving") {
      var p = pend[index];
      if (p && (Object.keys(p.fields).length || p.timer)) return;
      setRowState(index, "saved");
    }
  }

  function onSaveFailed(index, sent, err) {
    var s = seg(index);
    var p = pend[index];
    if (err && err.status === 409) {
      toast(err.message, "error");
      refresh().catch(function () {});
    }
    if (s && saved[index]) {
      var changed = false;
      Object.keys(sent).forEach(function (k) {
        if (p && Object.prototype.hasOwnProperty.call(p.fields, k)) return; // edited again since: leave it
        if (k in saved[index]) { s[k] = saved[index][k]; changed = true; }
      });
      if (changed) {
        var row = rows[index];
        if (row) {
          if ("text_vi" in sent && !(p && "text_vi" in p.fields)) { row.vi.value = s.text_vi; growOne(row.vi); }
          if ("start" in sent) row.startIn.value = formatTime(s.start);
          if ("end" in sent) row.endIn.value = formatTime(s.end);
        }
        updateRowView(s);
        tick();
        emit("segment-updated", s);
      }
    }
    showRowError(index, err && err.message ? err.message : "Could not save this line.");
    setRowState(index, "error");
  }

  function commitTime(index, field, value) {
    var s = seg(index);
    if (!s || !editable) return;
    var row = rows[index];
    if (typeof value !== "number" || !isFinite(value)) {
      showRowError(index, "Enter a time like 1:05.200 or 65.2 (seconds).");
      if (row) (field === "start" ? row.startIn : row.endIn).value = formatTime(s[field]);
      return;
    }
    value = round3(Math.max(0, value));
    var dur = state.duration;
    if (dur > 0 && value > dur) value = round3(dur);
    if (value === s[field]) {
      if (row) (field === "start" ? row.startIn : row.endIn).value = formatTime(s[field]);
      return;
    }
    var nextStart = field === "start" ? value : s.start;
    var nextEnd = field === "end" ? value : s.end;
    if (nextStart >= nextEnd) {
      showRowError(index, "The start must be before the end.");
      if (row) (field === "start" ? row.startIn : row.endIn).value = formatTime(s[field]);
      return;
    }
    s[field] = value;
    if (row) (field === "start" ? row.startIn : row.endIn).value = formatTime(value);
    updateRowView(s);
    tick();
    emit("segment-updated", s);
    var patch = {};
    patch[field] = value;
    scheduleSave(index, patch, 0);
  }

  /* Move a line (start and end together), used by dragging blocks on the timeline. Same
   * optimistic update and one PATCH as commitTime. Returns true when something changed. */
  function setTimes(index, start, end) {
    var s = seg(index);
    if (!s || !editable) return false;
    start = round3(Math.max(0, start));
    end = round3(end);
    if (state.duration > 0 && end > state.duration) end = round3(state.duration);
    if (!(start < end) || (start === s.start && end === s.end)) return false;
    s.start = start;
    s.end = end;
    updateRowView(s);
    tick();
    emit("segment-updated", s);
    scheduleSave(index, { start: start, end: end }, 0);
    return true;
  }

  /* Select a line from outside (a click on the timeline): highlight, scroll the list, jump there. */
  function selectLine(index, opts) {
    if (!seg(index)) return;
    setActive(index, { scroll: true, always: true, force: true });
    if (!opts || opts.seek !== false) seekToLine(index);
  }

  function commitInput(index, field, input) {
    commitTime(index, field, parseTime(input.value));
  }

  function nudge(index, field, delta) {
    var s = seg(index);
    if (!s || !editable) return;
    commitTime(index, field, round3(s[field] + delta));
  }

  function onTextInput(index, ta) {
    var s = seg(index);
    if (!s || !editable) return;
    s.text_vi = ta.value;
    rows[index].charsEl.textContent = charCount(ta.value) + " chars";
    growOne(ta);
    if (blocks[index]) placeBlock(blocks[index], s);
    tick();
    if (!ta.value.trim()) {
      // do not send an empty text; the server would refuse it
      var p = pend[index];
      if (p) { delete p.fields.text_vi; clearTimeout(p.timer); p.timer = null; }
      showRowError(index, "The Vietnamese text cannot be empty.");
      setRowState(index, "");
      return;
    }
    scheduleSave(index, { text_vi: ta.value }, SAVE_DELAY);
  }

  function onTextBlur(index, ta) {
    var s = seg(index);
    if (!s) return;
    if (!ta.value.trim() && saved[index]) {
      ta.value = saved[index].text_vi;
      s.text_vi = saved[index].text_vi;
      rows[index].charsEl.textContent = charCount(s.text_vi) + " chars";
      growOne(ta);
      showRowError(index, "The Vietnamese text cannot be empty, so the previous text was kept.");
      tick();
      return;
    }
    flush(index);
  }

  // ---------- public: updateSegment / refresh ----------
  function updateSegment(incoming) {
    if (!incoming || typeof incoming.index !== "number") return;
    var cur = seg(incoming.index);
    if (!cur) return;
    var index = incoming.index;
    var p = pend[index];
    var keep = {};
    if (p) Object.keys(p.fields).forEach(function (k) { keep[k] = true; });
    if (p && p.timer) keep.text_vi = true;
    var row = rows[index];
    if (row && document.activeElement === row.vi && incoming.text_vi != null &&
        row.vi.value.trim() === String(incoming.text_vi).trim()) keep.text_vi = true;
    saved[index] = {
      start: incoming.start != null ? incoming.start : (saved[index] || cur).start,
      end: incoming.end != null ? incoming.end : (saved[index] || cur).end,
      text_vi: incoming.text_vi != null ? incoming.text_vi : (saved[index] || cur).text_vi
    };
    var copy = {};
    Object.keys(incoming).forEach(function (k) { if (!keep[k]) copy[k] = incoming[k]; });
    Object.assign(cur, copy);
    cur.chars = charCount(cur.text_vi);
    updateRowView(cur);
    if (row) mountRowAudio(row, cur);
    tick();
    emit("segment-updated", cur);
  }

  function applyData(data) {
    var prevStatus = state.job ? state.job.status : null;
    state.job = data.job || {};
    state.duration = Number(data.duration) || 0;
    state.media = data.media || {};
    state.segments = Array.isArray(data.segments) ? data.segments : [];
    state.audio = data.audio || null;
    state.blur = data.blur || null;
    state.budget = data.budget === undefined ? null : data.budget;
    indexSegments();
    saved = {};
    state.segments.forEach(function (s) {
      saved[s.index] = { start: s.start, end: s.end, text_vi: s.text_vi };
      if (typeof s.chars !== "number") s.chars = charCount(s.text_vi);
    });
    pend = {};
    return prevStatus;
  }

  function renderHeader() {
    var job = state.job || {};
    var name = job.input_name || job.filename || jobId;
    $("ed-title").textContent = name;
    $("ed-title").setAttribute("title", name);
    document.title = "Edit " + name + " - Video Translator";
    var badge = $("ed-status");
    badge.hidden = !job.status;
    badge.className = "badge " + (job.status || "");
    badge.textContent = STATUS_LABELS[job.status] || job.status || "";
    var toggle = $("ed-dub-toggle");
    var media = state.media || {};
    toggle.disabled = !media.dub_video;
    if (!media.dub_video && dubOn) { dubOn = false; toggle.checked = false; emit("media-changed", { dub: false }); }
    if (!media.video && media.dub_video && !dubOn) { dubOn = true; toggle.checked = true; dubVersion = Date.now(); }
    $("ed-dub-label").title = media.dub_video ? "" : "The dubbed video does not exist yet. Apply the changes to build it.";
  }

  function setEditable(flag) {
    editable = flag;
    document.body.classList.toggle("readonly", !flag);
    Object.keys(rows).forEach(function (k) { setRowControlsDisabled(rows[k], !flag); });
    var banner = $("ed-banner");
    if (flag) {
      banner.hidden = true;
      banner.textContent = "";
    } else {
      var job = state.job || {};
      var msg = job.message || "";
      banner.textContent = "This job is " + (STATUS_LABELS[job.status] || job.status || "busy").toLowerCase() +
        ". Editing is locked until it finishes." + (msg ? " " + msg : "");
      banner.hidden = false;
    }
  }

  function renderAll() {
    renderHeader();
    setVideoSource();
    indexSegments();
    renderTimeline();
    renderLines();
    setEditable(!!EDITABLE[(state.job || {}).status]);
    curIndex = -2; // force the overlay to re-evaluate
    overlayKey = "";
    tick();
    if (!editable) startPolling(); else stopPolling();
  }

  function load() {
    return api("GET", "/editor").then(function (data) { return data; });
  }

  function refresh() {
    return flushAll().then(load).then(function (data) {
      var prevStatus = applyData(data);
      if (dubOn && prevStatus !== null && prevStatus !== state.job.status) dubVersion = Date.now();
      if (dubOn && prevStatus !== null && prevStatus !== state.job.status) srcKey = null;
      $("ed-loaderror").hidden = true;
      renderAll();
      emit("refreshed");
      return state;
    });
  }

  function mountPanel() {
    var panel = $("audio-panel");
    if (panel && window.EditorAudio && typeof window.EditorAudio.mountPanel === "function") {
      try { window.EditorAudio.mountPanel(panel); panelMounted = true; } catch (e) { if (window.console) console.error(e); }
    }
  }

  /* Re-mounts the audio panel and every line's audio area. editor_audio.js calls this when it loads late. */
  function mountAudio() {
    if (!window.EditorAudio) return;
    mountPanel();
    state.segments.forEach(function (s) { if (rows[s.index]) mountRowAudio(rows[s.index], s); });
  }

  // ---------- read-only polling ----------
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      api("GET", "/api/jobs/" + encodeURIComponent(jobId)).then(function (job) {
        if (!job) return;
        if (EDITABLE[job.status]) {
          stopPolling();
          refresh().then(function () { toast("The job finished. You can edit again.", "ok"); }, function (e) { toast(e.message, "error"); });
        } else if (state.job) {
          state.job.status = job.status;
          state.job.message = job.message;
          renderHeader();
          setEditable(false);
        }
      }, function () { /* keep trying */ });
    }, 3000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---------- navigation + keyboard ----------
  function goToLine(pos, focusText) {
    if (!state.segments.length) return;
    if (pos < 0) pos = 0;
    if (pos >= state.segments.length) {
      toast("That was the last line.", "info");
      return;
    }
    var index = state.segments[pos].index;
    setActive(index, { scroll: true, always: true });
    seekToLine(index);
    if (focusText && rows[index]) rows[index].vi.focus({ preventScroll: true });
  }

  function isTyping(target) {
    if (!target || !target.tagName) return false;
    return /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable;
  }

  function onKeydown(e) {
    if (e.defaultPrevented) return;
    var target = e.target;
    var typing = isTyping(target);
    if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      var pos = position(activeIndex);
      if (pos < 0) pos = Math.max(0, position(curIndex));
      var next = e.key === "ArrowDown" ? pos + 1 : pos - 1;
      if (next < 0) return;
      var focusText = !!(target && target.classList && target.classList.contains("line-vi"));
      var go = function () { goToLine(next, focusText); };
      if (focusText) flush(activeIndex).then(go); else go();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (typing) return;
    if (e.key === " " || e.code === "Space") {
      if (target && target.closest && target.closest("button, a, video, summary, [role=button]")) return;
      e.preventDefault();
      if (video.paused) { var p = video.play(); if (p && p.catch) p.catch(function () {}); } else video.pause();
      return;
    }
    if ((e.key === "[" || e.key === "]") && editable) {
      if (activeIndex < 0) { toast("Pick a line first.", "info"); return; }
      e.preventDefault();
      commitTime(activeIndex, e.key === "[" ? "start" : "end", video.currentTime || 0);
    }
  }

  // ---------- init ----------
  function wireStatic() {
    var subs = $("ed-opt-subs"), zh = $("ed-opt-zh"), follow = $("ed-opt-follow");
    subs.checked = readPref("subs", true);
    zh.checked = readPref("zh", false);
    follow.checked = readPref("follow", true);
    subs.addEventListener("change", function () { writePref("subs", subs.checked); updateOverlay(); });
    zh.addEventListener("change", function () { writePref("zh", zh.checked); updateOverlay(); });
    follow.addEventListener("change", function () {
      writePref("follow", follow.checked);
      if (follow.checked && rows[activeIndex]) scrollRowIntoView(rows[activeIndex].root, true);
    });

    $("ed-dub-toggle").addEventListener("change", function (e) {
      dubOn = e.target.checked && !!(state.media && state.media.dub_video);
      dubVersion = Date.now();
      setVideoSource();
      emit("media-changed", { dub: dubOn });
    });

    video.addEventListener("play", function () { startLoop(); emit("play"); });
    video.addEventListener("playing", startLoop);
    video.addEventListener("pause", function () { tick(); emit("pause"); });
    video.addEventListener("seeking", tick);
    video.addEventListener("seeked", function () { tick(); emit("seek", video.currentTime); });
    video.addEventListener("timeupdate", tick);
    video.addEventListener("durationchange", function () { updatePlayhead(video.currentTime || 0); });
    video.addEventListener("loadedmetadata", function () {
      if (video.videoWidth && video.videoHeight) {
        $("ed-player").style.setProperty("--ar", String(video.videoWidth / video.videoHeight));
      }
      tick();
    });
    video.addEventListener("error", function () {
      if (video.getAttribute("src")) toast("The video could not be loaded.", "error");
    });

    wireTimeline();
    document.addEventListener("keydown", onKeydown);
    $("ed-retry").addEventListener("click", start);
  }

  function showLoadError(err) {
    $("ed-loading") && ($("ed-loading").textContent = "");
    $("ed-loaderror-text").textContent = err && err.status === 404
      ? "This job was not found. It may have been deleted."
      : "Could not load the editor data. " + (err && err.message ? err.message : "");
    $("ed-loaderror").hidden = false;
  }

  function start() {
    $("ed-loaderror").hidden = true;
    refresh().then(function () {
      if (firstLoad) {
        firstLoad = false;
        mountPanel();
        emit("ready");
      }
    }, showLoadError);
  }

  var Editor = {
    jobId: jobId,
    state: state,
    video: video,
    api: api,
    updateSegment: updateSegment,
    refresh: refresh,
    on: on,
    emit: emit,
    toast: toast,
    formatTime: formatTime,
    parseTime: parseTime,
    mountAudio: mountAudio,
    isDub: function () { return dubOn; },
    isEditable: function () { return editable; },
    activeIndex: function () { return activeIndex; },
    duration: totalDuration,
    seek: seekTo,
    select: selectLine,
    setTimes: setTimes,
    // Call after changing Editor.state.job.status from outside (the audio panel's Apply flow).
    syncStatus: function () { renderHeader(); setEditable(!!EDITABLE[(state.job || {}).status]); }
  };
  window.Editor = Editor;

  function boot() { wireStatic(); start(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
