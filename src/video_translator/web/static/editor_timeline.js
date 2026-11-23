/* Editor timeline: tracks under the video (Vietnamese voice with waveforms, background music with a
 * waveform, subtitles, blur), like a video editor. Plain browser JS, no build step.
 * window.Editor comes from editor.js, window.EditorAudio from editor_audio.js.
 *
 * - Every line is one block in the voice track and one block in the subtitle track. Dragging a
 *   block moves the line, dragging its left or right edge changes the start or the end. Both
 *   tracks show the same line, so they move together (the voice follows the subtitle time).
 * - Mute (voice, music) and hide (subtitles, blur) only change what you see and hear in this
 *   browser while editing; they never change the rendered video.
 * - Positions use CSS custom properties (--s start, --d length in seconds, --pps pixels per
 *   second) so zooming is one style change. Waveforms are canvases drawn only for what is visible.
 * All server text goes into the DOM with textContent only. */
(function () {
  "use strict";

  var LABEL_W = 138;          // px, must match --tl-label in editor_timeline.css
  var MIN_LINE = 0.2;         // seconds, same rule as the server
  var DRAG_PX = 3;            // movement before a press counts as a drag
  var SNAP_PX = 8;
  var MAX_PPS = 400;
  var STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];

  function ed() { return window.Editor || null; }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function round3(x) { return Math.round(x * 1000) / 1000; }

  function h(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else n.setAttribute(k, v === true ? "" : String(v));
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function pref(key, fallback) {
    try {
      var v = window.localStorage.getItem("vt_tl_" + key);
      return v === null ? fallback : v;
    } catch (e) { return fallback; }
  }
  function setPref(key, value) {
    try { window.localStorage.setItem("vt_tl_" + key, String(value)); } catch (e) { /* ignore */ }
  }

  // ---------- state ----------
  var T = {
    mounted: false,
    pps: 30,
    minPps: 0.05,
    off: { voice: false, zh: false, music: false, subs: false, blur: false },
    voicePeaks: {},       // index -> {duration, peaks}
    voiceRate: 50,
    bg: null,             // {duration, peaks}
    zh: null,             // {duration, peaks}: the separated Chinese voice (only with voice separation)
    stems: false,
    bgRate: 20,
    keys: {},             // index -> "has_audio|audio_stale|speed" (to know when a clip changed)
    holdFollowUntil: 0,
    drag: null,
    raf: 0,
    rafPlay: 0
  };
  var els = {};           // dom references
  var blocks = {};        // index -> {voice, subs, canvas, drawn}
  var segCount = -1;

  function E() { return ed(); }
  function duration() { return Math.max(1, E().duration ? E().duration() : 1); }
  function segs() { return (E().state && E().state.segments) || []; }
  function seg(i) {
    var a = segs();
    if (a[i] && a[i].index === i) return a[i];
    for (var k = 0; k < a.length; k++) if (a[k].index === i) return a[k];
    return null;
  }
  function editable() { return !!(E() && E().isEditable && E().isEditable()); }
  function laneWidth() { return duration() * T.pps; }
  function viewWidth() { return Math.max(100, els.scroll.clientWidth - LABEL_W); }

  function shortTime(t, step) {
    t = Math.max(0, t);
    var h0 = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var ss = step < 1 ? s.toFixed(1) : String(Math.floor(s));
    if (s < 10) ss = "0" + ss;
    return (h0 ? h0 + ":" + (m < 10 ? "0" : "") + m : String(m)) + ":" + ss;
  }

  // ---------- layout / zoom ----------
  function setPps(pps, anchorX) {
    var old = T.pps;
    pps = clamp(pps, T.minPps, MAX_PPS);
    if (anchorX === undefined) anchorX = viewWidth() / 2;
    var t = (els.scroll.scrollLeft + anchorX) / old;
    T.pps = pps;
    setPref("pps", pps);
    applyWidth();
    els.scroll.scrollLeft = Math.max(0, t * pps - anchorX);
    updateZoomUi();
    schedule();
  }

  function applyWidth() {
    els.inner.style.setProperty("--pps", String(T.pps));
    els.inner.style.setProperty("--dur", String(duration()));
    els.inner.style.width = (LABEL_W + laneWidth() + 24) + "px";
  }

  function fitPps() { return clamp(viewWidth() / duration(), T.minPps, MAX_PPS); }

  function updateMin() {
    T.minPps = clamp(viewWidth() / duration() * 0.5, 0.01, 5);
  }

  function updateZoomUi() {
    if (!els.zoom) return;
    var span = Math.log(MAX_PPS / T.minPps);
    els.zoom.value = String(Math.round((Math.log(T.pps / T.minPps) / span) * 1000));
    els.zoomLabel.textContent = T.pps >= 10 ? Math.round(T.pps) + " px/s" : T.pps.toFixed(2) + " px/s";
  }

  function onZoomSlider() {
    var span = Math.log(MAX_PPS / T.minPps);
    setPps(T.minPps * Math.exp((Number(els.zoom.value) / 1000) * span));
  }

  // ---------- drawing (only what is visible) ----------
  function schedule() {
    if (T.raf) return;
    T.raf = requestAnimationFrame(function () { T.raf = 0; renderVisible(); });
  }

  function visibleRange() {
    var a = els.scroll.scrollLeft / T.pps;
    return { a: a, b: a + viewWidth() / T.pps };
  }

  function colour(name, fallback) {
    var v = window.getComputedStyle(document.body).getPropertyValue(name).trim();
    return v || fallback;
  }

  /* Draw peaks (0..100, `rate` per second) from time t0 over `widthPx` css pixels into a canvas. */
  function drawWave(canvas, peaks, rate, t0, widthPx, heightPx, fill) {
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.round(widthPx));
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.max(1, Math.round(heightPx * dpr));
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, heightPx);
    ctx.fillStyle = fill;
    var mid = heightPx / 2;
    var perPx = rate / T.pps;          // peak slices per pixel
    for (var x = 0; x < w; x++) {
      var from = Math.floor(t0 * rate + x * perPx);
      var to = Math.max(from + 1, Math.ceil(t0 * rate + (x + 1) * perPx));
      if (from >= peaks.length) break;
      var m = 0;
      for (var i = from; i < to && i < peaks.length; i++) if (peaks[i] > m) m = peaks[i];
      var half = Math.max(m > 0 ? 0.5 : 0, (m / 100) * (mid - 1));
      if (half > 0) ctx.fillRect(x, mid - half, 1, half * 2);
    }
  }

  function drawVoice(index, force) {
    var b = blocks[index];
    var data = T.voicePeaks[index];
    if (!b || !data) return;
    var key = T.pps + "|" + data.peaks.length + "|" + data.duration;
    if (!force && b.drawn === key) return;
    b.drawn = key;
    var heightPx = els.voiceLane.clientHeight - 8 || 44;
    var w = Math.min(Math.max(1, data.duration * T.pps), 8000);  // a clip is never that wide in practice
    b.canvas.style.width = w + "px";
    drawWave(b.canvas, data.peaks, T.voiceRate, 0, w, heightPx, colour("--accent", "#1f6feb"));
  }

  function drawBackground() {
    var c = els.bgCanvas;
    if (!T.bg || !T.bg.peaks.length) { c.hidden = true; return; }
    c.hidden = false;
    var x0 = Math.max(0, els.scroll.scrollLeft);
    var x1 = Math.min(laneWidth(), x0 + viewWidth() + 2);
    var w = Math.max(1, x1 - x0);
    c.style.left = x0 + "px";
    c.style.width = w + "px";
    drawWave(c, T.bg.peaks, T.bgRate, x0 / T.pps, w, els.musicLane.clientHeight - 6 || 38, colour("--ok", "#2da44e"));
  }

  function drawZh() {
    var c = els.zhCanvas;
    if (!T.stems || !T.zh || !T.zh.peaks.length) { c.hidden = true; return; }
    c.hidden = false;
    var x0 = Math.max(0, els.scroll.scrollLeft);
    var x1 = Math.min(laneWidth(), x0 + viewWidth() + 2);
    var w = Math.max(1, x1 - x0);
    c.style.left = x0 + "px";
    c.style.width = w + "px";
    drawWave(c, T.zh.peaks, T.bgRate, x0 / T.pps, w, els.zhLane.clientHeight - 6 || 38, colour("--warn", "#9a6700"));
  }

  function drawRuler() {
    var r = visibleRange();
    var step = STEPS[STEPS.length - 1];
    for (var i = 0; i < STEPS.length; i++) if (STEPS[i] * T.pps >= 80) { step = STEPS[i]; break; }
    var minor = step >= 10 ? step / 5 : step / 2;
    els.ruler.textContent = "";
    var frag = document.createDocumentFragment();
    var first = Math.max(0, Math.floor(r.a / minor) - 1) * minor;
    var count = 0;
    for (var t = first; t <= r.b + minor && count < 400; t += minor, count++) {
      var major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      var tick = h("span", { class: "tl-tick" + (major ? " major" : "") });
      tick.style.left = (t * T.pps) + "px";
      if (major) tick.appendChild(h("span", { class: "tl-tick-label", text: shortTime(t, step) }));
      frag.appendChild(tick);
    }
    els.ruler.appendChild(frag);
  }

  function renderVisible() {
    if (!T.mounted) return;
    drawRuler();
    drawBackground();
    drawZh();
    var r = visibleRange();
    segs().forEach(function (s) {
      if (s.end < r.a - 1 || s.start > r.b + 1) return;
      drawVoice(s.index, false);
    });
    updatePlayhead();
  }

  // ---------- blocks ----------
  function blockState(s) {
    var flags = s.flags || [];
    if (flags.indexOf("too_long") >= 0 || flags.indexOf("truncated") >= 0) return "bad";
    if (s.audio_stale || flags.indexOf("needs_realign") >= 0) return "stale";
    return "ok";
  }

  function place(i, start, end) {
    var b = blocks[i];
    if (!b) return;
    [b.voice, b.subs].forEach(function (n) {
      n.style.setProperty("--s", String(start));
      n.style.setProperty("--d", String(Math.max(0.02, end - start)));
    });
  }

  function refreshBlock(s) {
    var b = blocks[s.index];
    if (!b) return;
    place(s.index, s.start, s.end);
    var st = blockState(s);
    [b.voice, b.subs].forEach(function (n) {
      n.classList.remove("ok", "bad", "stale");
      n.classList.add(st);
    });
    b.voice.classList.toggle("noclip", !s.has_audio);
    b.subs.firstChild.textContent = s.text_vi || "";
    var tip = "#" + (s.index + 1) + "  " + E().formatTime(s.start) + " - " + E().formatTime(s.end) +
      " (" + (s.end - s.start).toFixed(2) + " s)\n" + (s.text_vi || "");
    b.voice.title = tip;
    b.subs.title = tip;
  }

  function makeBlock(kind, s) {
    var node = h("div", { class: "tl-block tl-" + kind, "data-index": s.index, "data-kind": kind });
    if (kind === "voice") {
      var canvas = h("canvas", { class: "tl-wave", "aria-hidden": "true" });
      node.appendChild(canvas);
      node.appendChild(h("span", { class: "tl-n", text: "#" + (s.index + 1) }));
    } else {
      node.appendChild(h("span", { class: "tl-text", lang: "vi" }));
    }
    node.appendChild(h("i", { class: "tl-h tl-h-l", "data-edge": "start" }));
    node.appendChild(h("i", { class: "tl-h tl-h-r", "data-edge": "end" }));
    return node;
  }

  function buildBlocks() {
    els.voiceLane.textContent = "";
    els.subsLane.textContent = "";
    blocks = {};
    var fv = document.createDocumentFragment(), fs = document.createDocumentFragment();
    segs().forEach(function (s) {
      var v = makeBlock("voice", s), t = makeBlock("subs", s);
      blocks[s.index] = { voice: v, subs: t, canvas: v.firstChild, drawn: "" };
      fv.appendChild(v);
      fs.appendChild(t);
      T.keys[s.index] = clipKey(s);
    });
    els.voiceLane.appendChild(fv);
    els.subsLane.appendChild(fs);
    segs().forEach(refreshBlock);
    segCount = segs().length;
    markActive(E().activeIndex ? E().activeIndex() : -1);
  }

  function clipKey(s) { return [s.has_audio ? 1 : 0, s.audio_stale ? 1 : 0, s.speed].join("|"); }

  var activeShown = -1;
  function markActive(i) {
    if (blocks[activeShown]) {
      blocks[activeShown].voice.classList.remove("is-active");
      blocks[activeShown].subs.classList.remove("is-active");
    }
    activeShown = i;
    if (blocks[i]) {
      blocks[i].voice.classList.add("is-active");
      blocks[i].subs.classList.add("is-active");
    }
  }

  // ---------- playhead ----------
  function updatePlayhead() {
    if (!T.mounted) return;
    var t = (E().video && E().video.currentTime) || 0;
    var x = t * T.pps;
    els.playhead.style.left = (LABEL_W + x) + "px";
    if (E().video && !E().video.paused && Date.now() > T.holdFollowUntil && !T.drag) {
      var sl = els.scroll.scrollLeft, vw = viewWidth();
      if (x < sl || x > sl + vw - 30) els.scroll.scrollLeft = Math.max(0, x - vw * 0.2);
    }
  }

  function playLoop() {
    updatePlayhead();
    if (E().video && !E().video.paused && !E().video.ended) T.rafPlay = requestAnimationFrame(playLoop);
    else T.rafPlay = 0;
  }

  // ---------- mute / hide ----------
  var BUTTONS = {
    voice: { word: "Mute", done: "Muted" },
    zh: { word: "Mute", done: "Muted" },
    music: { word: "Mute", done: "Muted" },
    subs: { word: "Hide", done: "Hidden" },
    blur: { word: "Hide", done: "Hidden" }
  };

  function applyOff() {
    var stage = document.getElementById("ed-stage");
    if (stage) {
      stage.classList.toggle("tl-hide-subs", T.off.subs);
      stage.classList.toggle("tl-hide-blur", T.off.blur);
    }
    if (window.EditorAudio && window.EditorAudio.setMute) {
      window.EditorAudio.setMute({ voice: T.off.voice, bg: T.off.music, zh: T.off.zh });
    }
    Object.keys(BUTTONS).forEach(function (k) {
      var btn = els.buttons[k], on = T.off[k];
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? BUTTONS[k].done : BUTTONS[k].word;
      els.rows[k].classList.toggle("is-off", on);
    });
    updateDubNote();
  }

  function updateDubNote() {
    var dub = !!(E() && E().isDub && E().isDub());
    ["voice", "music"].forEach(function (k) {
      els.buttons[k].disabled = dub;
      els.buttons[k].title = dub
        ? "Not available while you watch the finished dubbed video (its sound is already mixed)."
        : "Only changes what you hear while editing. The rendered video is not changed.";
    });
  }

  function toggle(kind) {
    T.off[kind] = !T.off[kind];
    setPref("off_" + kind, T.off[kind] ? "1" : "0");
    applyOff();
  }

  // ---------- pointer: seek on lanes / ruler ----------
  function timeAtEvent(ev) {
    var rect = els.inner.getBoundingClientRect();
    return clamp((ev.clientX - rect.left - LABEL_W) / T.pps, 0, duration());
  }

  function wireSeek(node) {
    node.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0 || ev.target.closest(".tl-block")) return;
      if (ev.target.closest(".tl-label")) return;
      ev.preventDefault();
      T.holdFollowUntil = Date.now() + 3000;
      E().seek(timeAtEvent(ev));
      var move = function (e2) { E().seek(timeAtEvent(e2)); updatePlayhead(); };
      var up = function () {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
      updatePlayhead();
    });
  }

  // ---------- pointer: drag blocks ----------
  function neighbours(i) {
    var a = segs(), pos = -1;
    for (var k = 0; k < a.length; k++) if (a[k].index === i) { pos = k; break; }
    return {
      prev: pos > 0 ? a[pos - 1] : null,
      next: pos >= 0 && pos < a.length - 1 ? a[pos + 1] : null
    };
  }

  function snapCandidates(i) {
    var n = neighbours(i), out = [0];
    if (n.prev) out.push(n.prev.end);
    if (n.next) out.push(n.next.start);
    out.push((E().video && E().video.currentTime) || 0);
    return out;
  }

  function snapped(value, cands) {
    var best = null, bestD = SNAP_PX / T.pps;
    cands.forEach(function (c) {
      var d = Math.abs(c - value);
      if (d <= bestD) { best = c; bestD = d; }
    });
    return best;
  }

  function computeDrag(d, dx) {
    var delta = dx / T.pps;
    var lo = d.prev ? d.prev.end : 0;
    var hi = d.next ? d.next.start : duration();
    var s = d.s0, e = d.e0;
    if (d.mode === "move") {
      var len = d.e0 - d.s0;
      s = clamp(d.s0 + delta, lo, hi - len);
      var a = snapped(s, d.cands), b = snapped(s + len, d.cands);
      if (a !== null) s = clamp(a, lo, hi - len);
      else if (b !== null) s = clamp(b - len, lo, hi - len);
      e = s + len;
    } else if (d.mode === "start") {
      s = clamp(d.s0 + delta, lo, d.e0 - MIN_LINE);
      var c = snapped(s, d.cands);
      if (c !== null) s = clamp(c, lo, d.e0 - MIN_LINE);
    } else {
      e = clamp(d.e0 + delta, d.s0 + MIN_LINE, hi);
      var f = snapped(e, d.cands);
      if (f !== null) e = clamp(f, d.s0 + MIN_LINE, hi);
    }
    return { start: round3(s), end: round3(e) };
  }

  function showTip(text, ev) {
    var tip = els.tip;
    tip.textContent = text;
    tip.hidden = false;
    var rect = els.panel.getBoundingClientRect();
    tip.style.left = clamp(ev.clientX - rect.left + 12, 0, Math.max(0, rect.width - 190)) + "px";
    tip.style.top = Math.max(0, ev.clientY - rect.top - 34) + "px";
  }

  function wireBlocks(lane) {
    lane.addEventListener("pointerdown", function (ev) {
      var node = ev.target.closest(".tl-block");
      if (!node || ev.button !== 0) return;
      var i = Number(node.getAttribute("data-index"));
      var s = seg(i);
      if (!s) return;
      var edge = ev.target.getAttribute && ev.target.getAttribute("data-edge");
      var n = neighbours(i);
      var d = {
        index: i, node: node, x0: ev.clientX, moved: false, pointer: ev.pointerId,
        mode: edge || "move", s0: s.start, e0: s.end, prev: n.prev, next: n.next,
        cands: snapCandidates(i), result: null
      };
      T.drag = d;
      T.holdFollowUntil = Date.now() + 3000;
      try { node.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      ev.preventDefault();
    });

    lane.addEventListener("pointermove", function (ev) {
      var d = T.drag;
      if (!d || ev.pointerId !== d.pointer) return;
      var dx = ev.clientX - d.x0;
      if (!d.moved && (Math.abs(dx) < DRAG_PX || !editable())) return;
      d.moved = true;
      d.node.classList.add("is-dragging");
      d.result = computeDrag(d, dx);
      place(d.index, d.result.start, d.result.end);
      showTip(E().formatTime(d.result.start) + " - " + E().formatTime(d.result.end) +
        "  (" + (d.result.end - d.result.start).toFixed(2) + " s)", ev);
    });

    function finish(ev, cancel) {
      var d = T.drag;
      if (!d || ev.pointerId !== d.pointer) return;
      T.drag = null;
      d.node.classList.remove("is-dragging");
      els.tip.hidden = true;
      try { d.node.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      var s = seg(d.index);
      if (!d.moved) {
        if (!cancel) E().select(d.index);
        return;
      }
      var changed = false;
      if (!cancel && d.result) changed = E().setTimes(d.index, d.result.start, d.result.end);
      if (!changed && s) refreshBlock(s);   // cancelled, or nothing changed: back to the real times
      if (!cancel) E().select(d.index, { seek: false });   // a drag must not move the playhead
    }
    lane.addEventListener("pointerup", function (ev) { finish(ev, false); });
    lane.addEventListener("pointercancel", function (ev) { finish(ev, true); });
    window.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && T.drag) {
        var d = T.drag;
        T.drag = null;
        d.node.classList.remove("is-dragging");
        els.tip.hidden = true;
        var s = seg(d.index);
        if (s) refreshBlock(s);
      }
    });
    lane.addEventListener("keydown", function (ev) {
      var node = ev.target.closest && ev.target.closest(".tl-block");
      if (node && (ev.key === "Enter" || ev.key === " ")) {
        ev.preventDefault();
        E().select(Number(node.getAttribute("data-index")));
      }
    });
  }

  // ---------- data ----------
  function loadWaveforms() {
    return E().api("GET", "/waveforms").then(function (res) {
      T.voicePeaks = {};
      Object.keys(res.voice || {}).forEach(function (k) { T.voicePeaks[Number(k)] = res.voice[k]; });
      T.voiceRate = res.voice_rate || 50;
      T.bg = res.background || null;
      T.bgRate = res.background_rate || 20;
      T.stems = !!res.stems;
      T.zh = res.voice_zh || null;
      if (els.rows) {
        els.rows.zh.hidden = !T.stems;
        var hint = els.rows.music.querySelector(".tl-hint");
        if (hint) hint.textContent = T.stems ? "separated from the voice" : "original audio";
      }
      Object.keys(blocks).forEach(function (k) { blocks[k].drawn = ""; });
      schedule();
    }, function () { /* no waveforms: blocks still work */ });
  }

  var oneTimers = {};
  function reloadOne(i) {
    clearTimeout(oneTimers[i]);
    oneTimers[i] = setTimeout(function () {
      E().api("GET", "/waveforms?index=" + i).then(function (res) {
        var one = (res.voice || {})[String(i)];
        if (one) T.voicePeaks[i] = one; else delete T.voicePeaks[i];
        if (blocks[i]) blocks[i].drawn = "";
        schedule();
      }, function () { /* ignore */ });
    }, 300);
  }

  function onSegmentUpdated(s) {
    if (!T.mounted || !s) return;
    if (segs().length !== segCount) { buildBlocks(); schedule(); return; }
    refreshBlock(s);
    var key = clipKey(s);
    if (T.keys[s.index] !== key) {
      T.keys[s.index] = key;
      reloadOne(s.index);
    }
    schedule();
  }

  function updateBlurBar() {
    if (!els.blurBar) return;
    var b = (E().state && E().state.blur && E().state.blur.settings) || null;
    var live = window.EditorBlur && window.EditorBlur.settings ? window.EditorBlur.settings() : null;
    var s = live || b;
    if (!s) { els.blurText.textContent = "Blur box"; return; }
    els.blurText.textContent = s.enabled
      ? "Blur box on: " + s.width + " x " + s.height + " px at " + s.x + ", " + s.y + ", strength " + s.strength + " (whole video)"
      : "Blur box off: the picture is not changed (whole video)";
    els.blurBar.classList.toggle("is-inactive", !s.enabled);
  }

  // ---------- build the panel ----------
  function row(kind, title, hint, cls) {
    var btn = h("button", { type: "button", class: "btn tiny tl-btn", "aria-pressed": "false" });
    btn.addEventListener("click", function () { toggle(kind); });
    var label = h("div", { class: "tl-label" }, [
      h("div", { class: "tl-name" }, [h("strong", { text: title }), hint ? h("span", { class: "tl-hint", text: hint }) : null]),
      btn
    ]);
    var lane = h("div", { class: "tl-lane " + (cls || "") });
    var r = h("div", { class: "tl-row tl-row-" + kind }, [label, lane]);
    els.buttons[kind] = btn;
    els.rows[kind] = r;
    return { row: r, lane: lane };
  }

  function mount(host) {
    els.buttons = {};
    els.rows = {};
    els.panel = host;
    host.textContent = "";
    host.classList.add("tl-panel");

    var out = h("span", { class: "tl-zoomlabel muted small-text" });
    var minus = h("button", { type: "button", class: "btn tiny", "aria-label": "Zoom out", title: "Zoom out", text: "-" });
    var plus = h("button", { type: "button", class: "btn tiny", "aria-label": "Zoom in", title: "Zoom in", text: "+" });
    var fit = h("button", { type: "button", class: "btn tiny", title: "Show the whole video", text: "Fit" });
    var zoom = h("input", { type: "range", min: "0", max: "1000", step: "1", "aria-label": "Zoom" });
    els.zoom = zoom;
    els.zoomLabel = out;
    var bar = h("div", { class: "tl-bar" }, [
      h("h2", { class: "tl-title", text: "Tracks" }),
      minus, zoom, plus, fit, out,
      h("span", { class: "muted small-text tl-help", text: "Ctrl + scroll to zoom. Drag a block to move it, drag its edges to change start or end." })
    ]);
    host.appendChild(bar);

    var ruler = h("div", { class: "tl-lane tl-ruler-lane" });
    els.ruler = ruler;
    var rulerRow = h("div", { class: "tl-row tl-row-ruler" }, [h("div", { class: "tl-label tl-label-empty" }), ruler]);

    var v = row("voice", "Vietnamese voice", "one block per line", "tl-voice-lane");
    var zhRow = row("zh", "Chinese voice", "separated", "tl-zh-lane");
    zhRow.row.hidden = true;  // shown only when the voice was separated from the music
    var m = row("music", "Background music", "original audio", "tl-music-lane");
    var s = row("subs", "Subtitles", "Vietnamese", "tl-subs-lane");
    var b = row("blur", "Blur", "whole video", "tl-blur-lane");
    els.voiceLane = v.lane;
    els.musicLane = m.lane;
    els.zhLane = zhRow.lane;
    els.zhCanvas = h("canvas", { class: "tl-zh-wave", "aria-hidden": "true", hidden: true });
    zhRow.lane.appendChild(els.zhCanvas);
    els.subsLane = s.lane;
    els.bgCanvas = h("canvas", { class: "tl-bg-wave", "aria-hidden": "true", hidden: true });
    m.lane.appendChild(els.bgCanvas);
    els.blurText = h("span", { class: "tl-blur-text" });
    els.blurBar = h("button", { type: "button", class: "tl-blur-bar" }, [els.blurText]);
    els.blurBar.addEventListener("click", function () {
      var panel = document.getElementById("blur-panel");
      if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    b.lane.appendChild(els.blurBar);

    els.playhead = h("div", { class: "tl-playhead", "aria-hidden": "true" });
    els.inner = h("div", { class: "tl-inner" }, [rulerRow, v.row, zhRow.row, m.row, s.row, b.row, els.playhead]);
    els.scroll = h("div", { class: "tl-scroll", tabindex: "0", role: "group", "aria-label": "Timeline tracks" }, [els.inner]);
    els.tip = h("div", { class: "tl-tip", hidden: true, role: "status" });
    host.appendChild(els.scroll);
    host.appendChild(els.tip);

    minus.addEventListener("click", function () { setPps(T.pps / 1.5); });
    plus.addEventListener("click", function () { setPps(T.pps * 1.5); });
    fit.addEventListener("click", function () { setPps(fitPps(), 0); els.scroll.scrollLeft = 0; });
    zoom.addEventListener("input", onZoomSlider);

    els.scroll.addEventListener("scroll", schedule, { passive: true });
    ["wheel", "touchstart"].forEach(function (n) {
      els.scroll.addEventListener(n, function () { T.holdFollowUntil = Date.now() + 3000; }, { passive: true });
    });
    els.scroll.addEventListener("wheel", function (ev) {
      if (!(ev.ctrlKey || ev.metaKey)) return;
      ev.preventDefault();
      var rect = els.scroll.getBoundingClientRect();
      var anchor = clamp(ev.clientX - rect.left - LABEL_W, 0, viewWidth());
      setPps(T.pps * (ev.deltaY < 0 ? 1.25 : 0.8), anchor);
    }, { passive: false });
    els.scroll.addEventListener("keydown", function (ev) {
      if (ev.target !== els.scroll) return;
      if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); setPps(T.pps * 1.5); }
      else if (ev.key === "-") { ev.preventDefault(); setPps(T.pps / 1.5); }
    });
    if (window.ResizeObserver) new ResizeObserver(function () { updateMin(); schedule(); }).observe(els.scroll);

    wireSeek(ruler);
    wireSeek(m.lane);
    wireSeek(v.lane);
    wireSeek(s.lane);
    wireBlocks(v.lane);
    wireBlocks(s.lane);
  }

  function init() {
    var host = document.getElementById("tracks-panel");
    if (!host || T.mounted || !E() || !E().state) return;
    mount(host);
    T.mounted = true;
    ["voice", "zh", "music", "subs", "blur"].forEach(function (k) { T.off[k] = pref("off_" + k, "0") === "1"; });
    updateMin();
    var saved = Number(pref("pps", ""));
    T.pps = clamp(saved > 0 ? saved : Math.max(fitPps(), 20), T.minPps, MAX_PPS);
    applyWidth();
    updateZoomUi();
    buildBlocks();
    applyOff();
    updateBlurBar();
    loadWaveforms();
    schedule();

    var Ed = E();
    Ed.on("segment-updated", onSegmentUpdated);
    Ed.on("active-changed", markActive);
    Ed.on("refreshed", function () {
      updateMin();
      applyWidth();
      buildBlocks();
      applyOff();
      updateBlurBar();
      loadWaveforms();
      schedule();
    });
    Ed.on("seek", function () { updatePlayhead(); });
    Ed.on("play", function () { if (!T.rafPlay) T.rafPlay = requestAnimationFrame(playLoop); });
    Ed.on("pause", updatePlayhead);
    Ed.on("media-changed", updateDubNote);
    Ed.on("blur-changed", updateBlurBar);
    Ed.video.addEventListener("loadedmetadata", function () { updateMin(); applyWidth(); schedule(); });
    Ed.video.addEventListener("timeupdate", updatePlayhead);
  }

  window.EditorTimeline = {
    _state: T,
    setZoom: function (pps) { if (T.mounted) setPps(pps); },
    toggleTrack: function (kind) { if (T.mounted && BUTTONS[kind]) toggle(kind); },
    isOff: function (kind) { return !!T.off[kind]; }
  };

  var E0 = ed();
  if (E0) {
    E0.on("ready", init);
    if (E0.state && E0.state.job && E0.state.job.id) init();   // loaded after the editor was ready
  }
})();
