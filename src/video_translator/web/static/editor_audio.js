/* Editor audio controls: per-line voice controls, volume panel, in-browser dub preview, Apply.
 * Plain browser JS, no build step. Contract: docs/editor-api.md (window.Editor is provided by editor.js).
 * All server text goes into the DOM with textContent only. */
(function () {
  "use strict";

  var LOOKAHEAD = 2.5;      // schedule clips that start within this many media seconds
  var PREFETCH_AHEAD = 45;  // fetch + decode clips for lines starting within this window
  var KEEP_AHEAD = 60;      // drop decoded clips for lines further ahead than this
  var KEEP_BEHIND = 10;     // ... or already finished longer ago than this
  var TICK_MS = 250;
  var RAMP = 0.15;          // seconds for volume ramps
  var DRIFT = 0.25;         // re-schedule when the clip and the video disagree by more than this
  var MAX_INFLIGHT = 3;
  var EDITABLE = ["awaiting_review", "done", "paused", "failed"];
  var TERMINAL = ["done", "failed", "paused", "awaiting_review"];
  var SPEED_MIN = 0.7, SPEED_MAX = 1.5;

  function noop() {}
  function ed() { return window.Editor || null; }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function num(v, fallback) { v = Number(v); return isFinite(v) ? v : fallback; }

  function h(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else n.setAttribute(k, v === true ? "" : String(v));
      });
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function spinner() { return h("span", { class: "ea-spinner", "aria-hidden": "true" }); }

  function toast(msg, kind) {
    var E = ed();
    if (E && E.toast) E.toast(msg, kind || "info");
  }

  /* Own fetch helper so the exact path form never depends on Editor.api. */
  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var detail = data && data.detail;
          if (Array.isArray(detail)) {
            detail = detail.map(function (d) { return (d && d.msg) || String(d); }).join("; ");
          }
          throw new Error(detail || ("The server answered " + res.status + "."));
        }
        return data;
      });
    }, function () {
      throw new Error("Could not reach the server. Check that the app is still running.");
    });
  }

  function jobBase() { var E = ed(); return "/api/jobs/" + (E ? E.jobId : ""); }
  function segments() { var E = ed(); return (E && E.state && E.state.segments) || []; }
  function getSeg(i, fallback) {
    var list = segments();
    for (var k = 0; k < list.length; k++) if (list[k].index === i) return list[k];
    return fallback || null;
  }
  function audioState() {
    var E = ed();
    var a = (E && E.state && E.state.audio) || {};
    var bg = clamp(num(a.background_volume, 1), 0, 2);
    return {
      voice: clamp(num(a.voice_volume, 1), 0, 2),
      bg: bg,
      ducked: clamp(num(a.ducked_volume, 0.25), 0, bg)
    };
  }
  function jobStatus() {
    var E = ed();
    return (E && E.state && E.state.job && E.state.job.status) || "";
  }
  function editable() {
    var s = jobStatus();
    return !s || EDITABLE.indexOf(s) !== -1;
  }
  function seekVideo(t, pause) {
    var E = ed();
    if (!E || !E.video) return;
    try {
      if (pause) E.video.pause();
      E.video.currentTime = Math.max(0, t);
    } catch (e) { /* video not ready */ }
  }

  /* ---------------------------------------------------------------- clip versions */
  var epoch = 0;      // bumped when the whole job was refreshed (clips may have changed)
  var ver = {};       // per-line counter bumped after regenerate/speed
  function bump(i) { ver[i] = (ver[i] || 0) + 1; }
  function clipTag(i) { return epoch + "." + (ver[i] || 0); }
  function clipUrl(i) { return jobBase() + "/segments/" + i + "/audio?v=" + clipTag(i); }
  function clipKey(seg) { return seg.index + ":" + clipTag(seg.index); }

  /* ---------------------------------------------------------------- per-line controls */
  var ctrls = {};     // index -> {el, seg}
  var ui = {};        // index -> transient UI state, survives re-mounts of the row
  var listening = null;
  var listenAudio = null;

  function uiOf(i) { return ui[i] || (ui[i] = { busy: null, focus: null, pendingSpeed: undefined, draft: null }); }

  function stateLabel(seg) {
    if (!seg.has_audio) return { text: "no voice yet", cls: "none" };
    if (seg.audio_stale) return { text: "voice out of date", cls: "stale" };
    return { text: "voice ready", cls: "ready" };
  }

  function stopListening() {
    var prev = listening;
    listening = null;
    if (listenAudio) { try { listenAudio.pause(); } catch (e) { /* ignore */ } }
    if (prev !== null) renderLine(prev);
  }

  function listen(i) {
    var seg = getSeg(i, ctrls[i] && ctrls[i].seg);
    if (!seg) return;
    if (listening === i) { stopListening(); return; }
    var prev = listening;
    seekVideo(seg.start, true);
    if (!listenAudio) {
      listenAudio = new Audio();
      listenAudio.preload = "auto";
      listenAudio.addEventListener("ended", function () { var p = listening; listening = null; if (p !== null) renderLine(p); });
      listenAudio.addEventListener("error", function () {
        var p = listening;
        if (p === null) return;
        listening = null;
        renderLine(p);
        toast("This voice clip could not be played. Try generating it again.", "error");
      });
    }
    listening = i;
    try { listenAudio.pause(); } catch (e) { /* ignore */ }
    listenAudio.src = clipUrl(i);
    listenAudio.volume = clamp(audioState().voice, 0, 1);
    var p = listenAudio.play();
    if (p && p.catch) {
      p.catch(function () {
        if (listening === i) { listening = null; renderLine(i); toast("Your browser blocked playback. Click Listen again.", "error"); }
      });
    }
    if (prev !== null) renderLine(prev);
    renderLine(i);
  }

  function afterSegmentChange(i, newSeg) {
    var E = ed();
    if (E && E.updateSegment) E.updateSegment(newSeg);
    if (ctrls[i]) ctrls[i].seg = newSeg;
    renderLine(i);
    previewResync();
    renderPanelMissing();
  }

  function regenerate(i) {
    var u = uiOf(i);
    var seg = getSeg(i, ctrls[i] && ctrls[i].seg);
    var wasNew = seg && !seg.has_audio;
    u.busy = "regen";
    u.focus = "regen";
    renderLine(i);
    api("POST", jobBase() + "/segments/" + i + "/regenerate", {}).then(function (newSeg) {
      u.busy = null;
      bump(i);
      u.focus = "regen";
      afterSegmentChange(i, newSeg);
      toast(wasNew ? "Voice generated for this line." : "Voice regenerated for this line.", "ok");
    }, function (err) {
      u.busy = null;
      u.focus = "regen";
      renderLine(i);
      toast(err.message, "error");
    });
  }

  function applySpeed(i, value) {
    var u = uiOf(i);
    if (u.busy) { u.pendingSpeed = value; return; }
    u.busy = "speed";
    u.focus = "speed";
    renderLine(i);
    api("POST", jobBase() + "/segments/" + i + "/speed", { speed: value }).then(function (newSeg) {
      u.busy = null;
      u.draft = null;
      bump(i);
      afterSegmentChange(i, newSeg);
      toast(value === null ? "Speed reset to automatic." : "Speed set to " + value.toFixed(2) + "x.", "ok");
      runPendingSpeed(i);
    }, function (err) {
      u.busy = null;
      u.draft = null;
      renderLine(i);
      toast(err.message, "error");
      runPendingSpeed(i);
    });
  }
  function runPendingSpeed(i) {
    var u = uiOf(i);
    if (u.pendingSpeed !== undefined) {
      var v = u.pendingSpeed;
      u.pendingSpeed = undefined;
      applySpeed(i, v);
    }
  }

  function focusRole(el, role) {
    var t = el.querySelector('[data-role="' + role + '"]');
    if (t && !t.disabled) { try { t.focus(); } catch (e) { /* ignore */ } return true; }
    return false;
  }

  function renderLine(i) {
    var c = ctrls[i];
    if (!c || !c.el) return;
    var el = c.el;
    var seg = getSeg(i, c.seg) || c.seg;
    var u = uiOf(i);
    var E = ed();
    var canEdit = editable();
    var busy = !!u.busy;
    if (!u.focus && el.contains(document.activeElement) && document.activeElement.dataset) {
      u.focus = document.activeElement.dataset.role || null;
    }
    clear(el);
    el.classList.add("ea-line");

    var top = h("div", { class: "ea-line-top" });
    var lbl = stateLabel(seg);
    var listenBtn = h("button", {
      type: "button", class: "ea-btn ea-small", "data-role": "listen",
      "aria-pressed": listening === i ? "true" : "false",
      "aria-label": (listening === i ? "Stop voice of line " : "Listen to voice of line ") + (i + 1),
      disabled: !seg.has_audio,
      title: seg.has_audio ? "" : "There is no voice for this line yet. Generate it first.",
      text: listening === i ? "Stop" : "Listen"
    });
    listenBtn.addEventListener("click", function () { listen(i); });
    var regenLabel = seg.has_audio ? "Regenerate" : "Generate voice";
    var regenBtn = h("button", {
      type: "button", class: "ea-btn ea-small", "data-role": "regen",
      "aria-label": regenLabel + " for line " + (i + 1),
      disabled: busy || !canEdit,
      title: canEdit ? "Free: runs on your computer; may take a few seconds." : "Editing is paused while the job is running.",
      text: regenLabel
    });
    regenBtn.addEventListener("click", function () { regenerate(i); });
    var state = h("span", { class: "ea-state ea-state-" + lbl.cls, role: "status", text: lbl.text });
    top.appendChild(listenBtn);
    top.appendChild(regenBtn);
    top.appendChild(state);
    if (u.busy === "regen") {
      top.appendChild(spinner());
      top.appendChild(h("span", { class: "ea-muted", text: "Generating voice..." }));
    }
    el.appendChild(top);

    // speed
    var hasClip = !!seg.has_audio;
    var cur = u.draft !== null && u.draft !== undefined ? u.draft : (seg.speed === null || seg.speed === undefined ? 1 : seg.speed);
    var sid = "ea-speed-" + i;
    var sp = h("div", { class: "ea-speed" });
    sp.appendChild(h("label", { class: "ea-lab", for: sid, text: "Speed" }));
    var slider = h("input", {
      type: "range", id: sid, min: SPEED_MIN, max: SPEED_MAX, step: "0.05", "data-role": "speed",
      "aria-label": "Speaking speed for line " + (i + 1),
      title: hasClip ? "" : "Generate the voice first, then you can change its speed.",
      "aria-disabled": (!hasClip || !canEdit) ? "true" : null
    });
    slider.value = String(cur);
    if (!hasClip || !canEdit) slider.disabled = true;
    var auto = (seg.speed === null || seg.speed === undefined) && (u.draft === null || u.draft === undefined);
    var readout = h("output", { class: "ea-readout", for: sid, text: auto ? "auto" : Number(cur).toFixed(2) + "x" });
    slider.setAttribute("aria-valuetext", readout.textContent);
    slider.addEventListener("input", function () {
      u.draft = Number(slider.value);
      readout.textContent = u.draft.toFixed(2) + "x";
      slider.setAttribute("aria-valuetext", readout.textContent);
    });
    slider.addEventListener("change", function () {
      var v = clamp(Number(slider.value), SPEED_MIN, SPEED_MAX);
      u.draft = v;
      applySpeed(i, Math.round(v * 100) / 100);
    });
    sp.appendChild(slider);
    sp.appendChild(readout);
    var showReset = hasClip && ((seg.speed !== null && seg.speed !== undefined) || (u.draft !== null && u.draft !== undefined));
    if (showReset) {
      var reset = h("button", {
        type: "button", class: "ea-btn ea-small ea-ghost", "data-role": "reset",
        "aria-label": "Reset speed of line " + (i + 1) + " to automatic", text: "Reset to auto", disabled: busy || !canEdit
      });
      reset.addEventListener("click", function () { u.focus = "speed"; applySpeed(i, null); });
      sp.appendChild(reset);
    }
    if (u.busy === "speed") {
      sp.appendChild(spinner());
      sp.appendChild(h("span", { class: "ea-muted", text: "Changing speed..." }));
    }
    el.appendChild(sp);

    if (u.focus) {
      var role = u.focus;
      if (focusRole(el, role)) { if (!busy) u.focus = null; }
      else if (!busy) { u.focus = null; focusRole(el, "regen"); }
    }
    void E;
  }


  function mountLine(el, seg) {
    if (!el || !seg) return;
    var i = seg.index;
    if (i === undefined && el.dataset) i = Number(el.dataset.index);
    ctrls[i] = { el: el, seg: seg };
    renderLine(i);
  }
  function renderAllLines() {
    Object.keys(ctrls).forEach(function (k) {
      var c = ctrls[k];
      if (c.el && !c.el.isConnected) { delete ctrls[k]; return; }
      renderLine(Number(k));
    });
  }

  /* ---------------------------------------------------------------- dub preview (Web Audio) */
  var P = {
    ctx: null, bg: null, voice: null, vsrc: null, graph: false, pref: null,
    entries: {}, cache: {}, inflight: {}, failed: {}, nIn: 0, lastSweep: 0, timer: null, wired: false,
    mute: { voice: false, bg: false }, mutedByUs: false
  };

  function anyClip() { return segments().some(function (s) { return s.has_audio; }); }
  /* While the finished dubbed mp4 is playing it already contains the voice: never add the preview on top. */
  function dubPlaying() { var E = ed(); return !!(E && E.isDub && E.isDub()); }
  function previewEnabled() { return dubPlaying() ? false : (P.pref === null ? anyClip() : P.pref); }

  function getCtx() {
    if (P.ctx) return P.ctx;
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { P.ctx = new AC(); } catch (e) { return null; }
    return P.ctx;
  }

  /* Build the graph only once the context is really running, otherwise routing the video through a
   * suspended context would silence it. Returns true when the graph is ready. */
  function ensureGraph() {
    var E = ed();
    if (P.graph) {
      if (P.ctx.state === "suspended") P.ctx.resume().catch(noop);
      return P.ctx.state === "running";
    }
    if (!E || !E.video) return false;
    var ctx = getCtx();
    if (!ctx) return false;
    if (ctx.state !== "running") {
      ctx.resume().then(function () { if (ctx.state === "running") previewResync(); }).catch(noop);
      return false;
    }
    try {
      P.vsrc = ctx.createMediaElementSource(E.video);
    } catch (e) {
      return false;
    }
    P.bg = ctx.createGain();
    P.voice = ctx.createGain();
    P.vsrc.connect(P.bg);
    P.bg.connect(ctx.destination);
    P.voice.connect(ctx.destination);
    P.graph = true;
    applyMute();
    return true;
  }

  /* Track mute buttons of the timeline (preview only). Not while the finished dub plays: its sound is already mixed. */
  function muted(which) { return !dubPlaying() && !!P.mute[which]; }

  function applyStaticGains() {
    if (!P.graph) return;
    var a = audioState();
    var now = P.ctx.currentTime;
    P.voice.gain.cancelScheduledValues(now);
    P.voice.gain.setTargetAtTime(muted("voice") ? 0 : a.voice, now, 0.02);
  }

  /* Without the Web Audio graph the original sound comes straight from the video element. */
  function applyMute() {
    var E = ed(), v = E && E.video;
    if (P.graph) {
      if (P.mutedByUs && v) { v.muted = false; P.mutedByUs = false; }
      applyStaticGains();
      applyBg();
    } else if (v) {
      if (muted("bg") && !v.muted) { v.muted = true; P.mutedByUs = true; }
      else if (!muted("bg") && P.mutedByUs) { v.muted = false; P.mutedByUs = false; }
    }
  }

  function setMute(m) {
    m = m || {};
    if (typeof m.voice === "boolean") P.mute.voice = m.voice;
    if (typeof m.bg === "boolean") P.mute.bg = m.bg;
    applyMute();
  }

  function liveEntries(now) {
    var out = [];
    Object.keys(P.entries).forEach(function (k) {
      var e = P.entries[k];
      if (e.endCtx > now - 0.02) out.push(e);
    });
    out.sort(function (a, b) { return a.when - b.when; });
    return out;
  }

  /* Schedule the background gain: base level, ducked while any scheduled clip is audible. */
  function applyBg() {
    if (!P.graph) return;
    var ctx = P.ctx, g = P.bg.gain, now = ctx.currentTime;
    var a = audioState();
    var keep = muted("bg") ? 0 : 1;
    var base = (previewEnabled() ? a.bg : 1) * keep;
    var duck = (previewEnabled() ? a.ducked : 1) * keep;
    var cur = g.value;
    g.cancelScheduledValues(now);
    g.setValueAtTime(cur, now);
    var last = now;
    function ramp(v, t) { t = Math.max(t, last + 0.001); g.linearRampToValueAtTime(v, t); last = t; }
    function hold(v, t) { t = Math.max(t, last + 0.001); g.setValueAtTime(v, t); last = t; }

    var merged = [];
    if (previewEnabled()) {
      liveEntries(now).forEach(function (e) {
        var s = Math.max(e.when, now), en = e.endCtx;
        var m = merged[merged.length - 1];
        if (m && s - m.e < 2 * RAMP + 0.05) m.e = Math.max(m.e, en);
        else merged.push({ s: s, e: en });
      });
    }
    if (!merged.length) {
      if (Math.abs(cur - base) > 0.001) ramp(base, now + RAMP);
      return;
    }
    var first = merged[0];
    if (first.s <= now + 0.001) {
      ramp(duck, now + RAMP);
    } else {
      if (Math.abs(cur - base) > 0.001) ramp(base, Math.min(now + RAMP, Math.max(now + 0.01, first.s - RAMP)));
    }
    merged.forEach(function (m, idx) {
      if (!(idx === 0 && first.s <= now + 0.001)) {
        hold(base, m.s - RAMP);
        ramp(duck, m.s);
      }
      hold(duck, m.e);
      ramp(base, m.e + RAMP);
    });
  }

  function stopEntry(e) {
    try { e.src.onended = null; e.src.stop(); } catch (err) { /* already stopped */ }
    try { e.src.disconnect(); } catch (err) { /* ignore */ }
  }
  function clearAll() {
    var had = false;
    Object.keys(P.entries).forEach(function (k) { stopEntry(P.entries[k]); had = true; });
    P.entries = {};
    if (had && P.graph) applyBg();
  }

  function ensureClip(seg) {
    var key = clipKey(seg);
    if (P.cache[key] || P.inflight[key]) return;
    if (P.failed[key] && Date.now() - P.failed[key] < 15000) return;
    if (P.nIn >= MAX_INFLIGHT) return;
    var ctx = getCtx();
    if (!ctx) return;
    P.inflight[key] = true;
    P.nIn++;
    var done = function () { delete P.inflight[key]; P.nIn--; };
    fetch(clipUrl(seg.index)).then(function (res) {
      if (!res.ok) throw new Error("no clip");
      return res.arrayBuffer();
    }).then(function (ab) {
      return ctx.decodeAudioData(ab);
    }).then(function (buf) {
      done();
      var s = getSeg(seg.index);
      if (s && clipKey(s) === key) {
        P.cache[key] = { buf: buf, idx: seg.index };
        tick();
      }
    }).catch(function () {
      done();
      P.failed[key] = Date.now();
    });
  }

  function prefetch(t) {
    var list = segments();
    for (var k = 0; k < list.length; k++) {
      var s = list[k];
      if (!s.has_audio) continue;
      if (s.start > t + PREFETCH_AHEAD || s.end < t - 0.5) continue;
      ensureClip(s);
      if (P.nIn >= MAX_INFLIGHT) break;
    }
  }

  function sweepCache(t) {
    Object.keys(P.cache).forEach(function (key) {
      var c = P.cache[key];
      var s = getSeg(c.idx);
      if (!s || !s.has_audio || clipKey(s) !== key || s.start > t + KEEP_AHEAD || s.end < t - KEEP_BEHIND) delete P.cache[key];
    });
    Object.keys(P.failed).forEach(function (key) {
      if (Date.now() - P.failed[key] > 15000) delete P.failed[key];
    });
  }

  function scheduleLine(seg, buf, t, r, now) {
    var ctx = P.ctx;
    var S = seg.start;
    var src = ctx.createBufferSource();
    src.buffer = buf;
    src.playbackRate.value = r;
    src.connect(P.voice);
    var when, offset;
    if (t <= S) { when = now + (S - t) / r; offset = 0; }
    else { when = now; offset = t - S; }
    try { src.start(when, offset); } catch (e) { try { src.disconnect(); } catch (e2) { /* ignore */ } return false; }
    var entry = {
      index: seg.index, src: src, when: when, offset: offset, rate: r, S: S, dur: buf.duration,
      endCtx: when + (buf.duration - offset) / r
    };
    src.onended = function () { try { src.disconnect(); } catch (e) { /* ignore */ } };
    P.entries[seg.index] = entry;
    return true;
  }

  function hasEntries() { for (var k in P.entries) return true; return false; }

  function tick() {
    var E = ed();
    if (!E || !E.video) return;
    var v = E.video;
    var enabled = previewEnabled();
    if (!enabled) {
      if (hasEntries()) clearAll();
      if (P.graph) applyBg();
      return;
    }
    if (P.ctx || !v.paused) {
      if (!getCtx()) return;
      prefetch(v.currentTime || 0);
    }
    if (v.paused || v.ended || v.seeking || v.readyState < 3) {
      if (hasEntries()) clearAll();
      return;
    }
    if (!ensureGraph()) return;
    var ctx = P.ctx;
    var now = ctx.currentTime, t = v.currentTime, r = v.playbackRate || 1;

    // Drop finished entries and detect drift (video stalled, clock skew).
    var drifted = false;
    Object.keys(P.entries).forEach(function (k) {
      var e = P.entries[k];
      if (t > e.S + e.dur + 0.5) { stopEntry(e); delete P.entries[k]; return; }
      if (now > e.endCtx + 0.1) return;
      var clipPos = e.offset + (now - e.when) * e.rate;
      if (Math.abs(clipPos - (t - e.S)) > DRIFT) drifted = true;
    });
    if (drifted) { clearAll(); }

    var added = false;
    var list = segments();
    for (var k = 0; k < list.length; k++) {
      var seg = list[k];
      if (!seg.has_audio || P.entries[seg.index]) continue;
      if (seg.start - t > LOOKAHEAD * r) continue;
      if (seg.end < t - 30) continue;
      var c = P.cache[clipKey(seg)];
      if (!c) continue;
      if (t >= seg.start + c.buf.duration - 0.02) continue;
      if (scheduleLine(seg, c.buf, t, r, now)) added = true;
    }
    if (added || drifted) applyBg();

    var tm = Date.now();
    if (tm - P.lastSweep > 2000) { P.lastSweep = tm; sweepCache(t); }
  }

  function previewResync() {
    clearAll();
    tick();
    updateToggle();
  }

  function wirePreview() {
    if (P.wired) return;
    var E = ed();
    if (!E || !E.video) return;
    P.wired = true;
    var v = E.video;
    ["play", "playing", "seeked", "ratechange"].forEach(function (n) { v.addEventListener(n, previewResync); });
    ["pause", "seeking", "waiting", "ended", "emptied"].forEach(function (n) { v.addEventListener(n, function () { clearAll(); }); });
    // Resume a suspended AudioContext on the first user gesture.
    var onGesture = function () {
      if (P.ctx && P.ctx.state === "suspended") P.ctx.resume().then(previewResync).catch(noop);
      else if (!v.paused) previewResync();
    };
    ["pointerdown", "keydown"].forEach(function (n) { document.addEventListener(n, onGesture, true); });
    P.timer = setInterval(tick, TICK_MS);
  }

  function setPreview(on) {
    P.pref = !!on;
    if (!on) { clearAll(); if (P.graph) applyBg(); }
    else previewResync();
    updateToggle();
  }

  function debug() {
    var now = P.ctx ? P.ctx.currentTime : 0;
    var E = ed();
    var a = audioState();
    return {
      enabled: previewEnabled(), graph: P.graph, ctxState: P.ctx ? P.ctx.state : null,
      t: E && E.video ? E.video.currentTime : null,
      rate: E && E.video ? E.video.playbackRate : null,
      scheduled: Object.keys(P.entries).map(function (k) {
        var e = P.entries[k];
        return { index: e.index, startsIn: e.when - now, offset: e.offset, endsIn: e.endCtx - now, rate: e.rate };
      }),
      audible: Object.keys(P.entries).some(function (k) { var e = P.entries[k]; return e.when <= now && now < e.endCtx; }),
      cached: Object.keys(P.cache),
      inflight: Object.keys(P.inflight),
      voiceGain: P.graph ? P.voice.gain.value : null,
      bgGain: P.graph ? P.bg.gain.value : null,
      mute: { voice: P.mute.voice, bg: P.mute.bg },
      target: a
    };
  }

  /* ---------------------------------------------------------------- panel */
  var panel = null;           // {el, refs...}
  var saveTimer = null;
  var saveSeq = 0;
  var apply = { running: false, timer: null, polls: 0, seenActive: false, missing: [], error: "", message: "", fraction: null, stage: "" };

  function pct(v) { return Math.round(v * 100) + "%"; }

  function sliderRow(id, label, hint) {
    var row = h("div", { class: "ea-slider-row" });
    row.appendChild(h("label", { class: "ea-lab", for: id, text: label }));
    var input = h("input", { type: "range", id: id, min: "0", max: "2", step: "0.05" });
    var out = h("output", { class: "ea-readout", for: id });
    row.appendChild(input);
    row.appendChild(out);
    if (hint) row.appendChild(h("span", { class: "ea-hint ea-slider-hint", text: hint }));
    return { row: row, input: input, out: out };
  }

  function mountPanel(el) {
    if (!el) return;
    clear(el);
    el.classList.add("ea-panel");
    var refs = { el: el };
    panel = refs;

    el.appendChild(h("h3", { class: "ea-title", text: "Voice and volume" }));

    // dub preview toggle
    var tid = "ea-preview-toggle";
    var tg = h("div", { class: "ea-toggle" });
    var cb = h("input", { type: "checkbox", id: tid, "aria-describedby": "ea-preview-hint" });
    tg.appendChild(cb);
    tg.appendChild(h("label", { for: tid, text: "Dub preview" }));
    el.appendChild(tg);
    el.appendChild(h("p", {
      id: "ea-preview-hint", class: "ea-hint",
      text: "Plays the Vietnamese voice over the video while you watch. The original sound is lowered under the voice. " +
        "This preview is approximate. The exact result comes from Apply and re-render."
    }));
    refs.toggle = cb;
    cb.addEventListener("change", function () { setPreview(cb.checked); });
    refs.noClip = h("p", { class: "ea-hint ea-noclip", role: "status" });
    el.appendChild(refs.noClip);

    // sliders
    var v = sliderRow("ea-vol-voice", "Voice volume");
    var b = sliderRow("ea-vol-bg", "Background volume");
    var d = sliderRow("ea-vol-duck", "Background while voice speaks");
    refs.v = v; refs.b = b; refs.d = d;
    var sl = h("div", { class: "ea-sliders" }, [v.row, b.row, d.row]);
    el.appendChild(sl);
    refs.saveState = h("p", { class: "ea-hint ea-save", role: "status", "aria-live": "polite" });
    el.appendChild(refs.saveState);

    function onSlider(which) {
      var E = ed();
      if (!E || !E.state) return;
      if (!E.state.audio) E.state.audio = { voice_volume: 1, background_volume: 1, ducked_volume: 0.25 };
      var a = E.state.audio;
      a.voice_volume = Number(v.input.value);
      a.background_volume = Number(b.input.value);
      a.ducked_volume = Math.min(Number(d.input.value), a.background_volume);
      if (which === "bg" && a.ducked_volume > a.background_volume) a.ducked_volume = a.background_volume;
      syncSliders(true);
      applyStaticGains();
      if (P.graph) applyBg();
      if (listenAudio) listenAudio.volume = clamp(a.voice_volume, 0, 1);
      queueSave();
    }
    v.input.addEventListener("input", function () { onSlider("voice"); });
    b.input.addEventListener("input", function () { onSlider("bg"); });
    d.input.addEventListener("input", function () { onSlider("duck"); });

    // apply
    var ap = h("div", { class: "ea-apply" });
    refs.applyBtn = h("button", { type: "button", class: "ea-btn ea-primary", text: "Apply and re-render" });
    refs.applyBtn.addEventListener("click", startApply);
    refs.applyNote = h("p", { class: "ea-hint", text: "Rebuilds the subtitles, voice track and video from your edits. Voice generation is free and runs on your computer." });
    ap.appendChild(refs.applyBtn);
    ap.appendChild(refs.applyNote);
    refs.status = h("div", { class: "ea-status", role: "status", "aria-live": "polite" });
    refs.missing = h("div", { class: "ea-missing" });
    refs.bar = h("div", { class: "ea-bar", role: "progressbar", "aria-label": "Re-render progress", "aria-valuemin": "0", "aria-valuemax": "100", hidden: "" });
    refs.barFill = h("div", { class: "ea-bar-fill" });
    refs.bar.appendChild(refs.barFill);
    refs.error = h("div", { class: "ea-error", role: "alert" });
    refs.links = h("div", { class: "ea-links" });
    ap.appendChild(refs.bar);
    ap.appendChild(refs.status);
    ap.appendChild(refs.missing);
    ap.appendChild(refs.error);
    ap.appendChild(refs.links);
    el.appendChild(ap);

    syncSliders(false);
    updateToggle();
    renderApply();
    wirePreview();
    if (jobStatus() === "running" && !apply.running) {
      apply.running = true; apply.seenActive = true; apply.polls = 0;
      pollSoon(0);
    }
  }

  function syncSliders(keepFocus) {
    if (!panel) return;
    var a = audioState();
    var pairs = [[panel.v, a.voice], [panel.b, a.bg], [panel.d, a.ducked]];
    panel.d.input.max = String(a.bg);
    pairs.forEach(function (p) {
      if (!keepFocus || document.activeElement !== p[0].input) p[0].input.value = String(p[1]);
      p[0].out.textContent = pct(p[1]);
      p[0].input.setAttribute("aria-valuetext", pct(p[1]));
    });
    var dis = !editable();
    [panel.v, panel.b, panel.d].forEach(function (p) { p.input.disabled = false; void dis; });
  }

  function updateToggle() {
    if (!panel) return;
    panel.toggle.checked = previewEnabled();
    panel.toggle.disabled = dubPlaying();
    var any = anyClip();
    if (dubPlaying()) { panel.noClip.textContent = "The finished dubbed video is playing, so the preview is off. Untick \"Dubbed result\" to preview your edits."; return; }
    panel.noClip.textContent = any ? "" : "No voice clips exist yet, so there is nothing to preview. Generate a voice on a line first.";
  }

  function queueSave() {
    if (!panel) return;
    panel.saveState.textContent = "Changes not saved yet...";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 500);
  }
  function saveNow() {
    saveTimer = null;
    var a = audioState();
    var seq = ++saveSeq;
    if (panel) panel.saveState.textContent = "Saving...";
    api("PUT", jobBase() + "/audio-settings", {
      voice_volume: a.voice, background_volume: a.bg, ducked_volume: a.ducked
    }).then(function () {
      if (panel && seq === saveSeq) panel.saveState.textContent = "Volume settings saved.";
    }, function (err) {
      if (panel && seq === saveSeq) panel.saveState.textContent = "Could not save the volume settings.";
      toast(err.message, "error");
    });
  }

  function stageName(s) { return String(s || "").replace(/_/g, " "); }

  function renderApply() {
    if (!panel) return;
    var r = panel;
    var running = apply.running || jobStatus() === "running";
    r.applyBtn.disabled = running || !editable();
    r.applyBtn.title = (!editable() && !running) ? "The job is busy right now." : "";
    clear(r.status);
    if (running) {
      r.status.appendChild(spinner());
      var txt = "Re-rendering";
      if (apply.stage) txt += ": " + stageName(apply.stage);
      if (apply.message) txt += (apply.stage ? ". " : ": ") + apply.message;
      r.status.appendChild(document.createTextNode(" " + txt));
    } else if (apply.message) {
      r.status.textContent = apply.message;
    }
    if (running && apply.fraction !== null) {
      r.bar.hidden = false;
      var p = Math.round(clamp(apply.fraction, 0, 1) * 100);
      r.barFill.style.width = p + "%";
      r.bar.setAttribute("aria-valuenow", String(p));
    } else {
      r.bar.hidden = true;
    }
    r.error.textContent = apply.error || "";
    renderPanelMissing();
    renderLinks();
  }

  function renderPanelMissing() {
    if (!panel) return;
    var m = panel.missing;
    clear(m);
    var still = apply.missing.filter(function (i) { var s = getSeg(i); return !s || !s.has_audio; });
    if (!still.length) return;
    var n = still.length;
    m.appendChild(h("p", { class: "ea-warn", text: n + (n === 1 ? " line has" : " lines have") + " no voice yet and will stay silent:" }));
    var ul = h("div", { class: "ea-missing-list" });
    still.forEach(function (i) {
      var b = h("button", { type: "button", class: "ea-btn ea-small ea-ghost", text: "Line " + (i + 1), "aria-label": "Go to line " + (i + 1) });
      b.addEventListener("click", function () { gotoLine(i); });
      ul.appendChild(b);
    });
    m.appendChild(ul);
  }

  function gotoLine(i) {
    var s = getSeg(i);
    if (s) seekVideo(s.start, true);
    var row = document.querySelector('.line-audio[data-index="' + i + '"]');
    if (row) {
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      try { row.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" }); } catch (e) { row.scrollIntoView(); }
      var b = row.querySelector('[data-role="regen"]');
      if (b) { try { b.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    }
  }

  function renderLinks() {
    if (!panel) return;
    var l = panel.links;
    clear(l);
    if (jobStatus() !== "done") return;
    var E = ed();
    var base = jobBase();
    l.appendChild(h("span", { class: "ea-muted", text: "Downloads:" }));
    l.appendChild(h("a", { class: "ea-btn ea-small", href: base + "/download/srt", download: "", text: "Download .srt" }));
    if (!E || !E.state || !E.state.media || E.state.media.dub_video !== false) {
      l.appendChild(h("a", { class: "ea-btn ea-small", href: base + "/download/video", download: "", text: "Download video" }));
    }
  }

  function startApply() {
    if (apply.running) return;
    stopListening();
    clearTimeout(saveTimer);
    if (saveTimer !== null) { saveTimer = null; }
    apply.error = ""; apply.message = ""; apply.missing = []; apply.stage = ""; apply.fraction = null;
    apply.running = true; apply.seenActive = false; apply.polls = 0;
    var E = ed();
    if (E && E.video) { try { E.video.pause(); } catch (e) { /* ignore */ } }
    renderApply();
    // Flush any pending volume save first so the render uses what the sliders show.
    var a = audioState();
    api("PUT", jobBase() + "/audio-settings", { voice_volume: a.voice, background_volume: a.bg, ducked_volume: a.ducked })
      .catch(noop)
      .then(function () { return api("POST", jobBase() + "/apply"); })
      .then(function (res) {
        apply.missing = (res && res.missing_audio) || [];
        if (E && E.state && E.state.job) { E.state.job.status = "running"; E.state.job.message = ""; }
        if (E && E.syncStatus) E.syncStatus();
        apply.message = "Started.";
        renderApply();
        renderAllLines();
        pollSoon(1500);
      }, function (err) {
        apply.running = false;
        apply.error = err.message;
        renderApply();
        toast(err.message, "error");
      });
  }

  function pollSoon(ms) {
    clearTimeout(apply.timer);
    apply.timer = setTimeout(poll, ms);
  }

  function poll() {
    var E = ed();
    api("GET", jobBase()).then(function (job) {
      apply.polls++;
      var active = job.status === "running" || job.queued || job.running;
      if (active) apply.seenActive = true;
      apply.stage = job.current_stage || (job.progress && job.progress.stage) || "";
      apply.message = job.message || "";
      apply.fraction = job.progress && typeof job.progress.fraction === "number" ? job.progress.fraction : null;
      if (E && E.state && E.state.job) {
        E.state.job.status = job.status;
        E.state.job.message = job.message;
        E.state.job.current_stage = job.current_stage;
        E.state.job.error = job.error;
        if (active && E.syncStatus) E.syncStatus();
      }
      var finished = !active && TERMINAL.indexOf(job.status) !== -1 && (apply.seenActive || apply.polls >= 2);
      if (!finished) { renderApply(); pollSoon(1500); return; }
      apply.running = false;
      apply.stage = ""; apply.fraction = null;
      if (job.status === "failed" || job.error) {
        apply.message = "";
        apply.error = job.error || "The re-render failed.";
        if (E && E.refresh) E.refresh().catch(noop);  // unlock editing again
        renderApply();
        renderAllLines();
        toast("Re-render failed. " + apply.error, "error");
        return;
      }
      apply.message = "Done. The subtitles and video match your latest edits.";
      var finish = function () {
        renderApply();
        renderAllLines();
        toast("Re-render finished.", "ok");
      };
      var rf = E && E.refresh ? E.refresh() : null;
      if (rf && rf.then) rf.then(finish, finish); else finish();
    }, function (err) {
      apply.polls++;
      if (apply.polls > 40) {
        apply.running = false;
        apply.error = err.message;
        renderApply();
        return;
      }
      pollSoon(3000);
    });
  }

  /* ---------------------------------------------------------------- wiring */
  function onRefreshed() {
    epoch++;
    P.cache = {}; P.failed = {};
    clearAll();
    syncSliders(true);
    updateToggle();
    renderApply();
    renderAllLines();
    previewResync();
  }

  function subscribe() {
    var E = ed();
    if (!E || !E.on || subscribe.done) return;
    subscribe.done = true;
    E.on("ready", function () {
      wirePreview();
      syncSliders(true);
      updateToggle();
      renderApply();
      previewResync();
    });
    E.on("segment-updated", function () { previewResync(); renderPanelMissing(); });
    E.on("refreshed", onRefreshed);
    E.on("seek", function () { previewResync(); });
    E.on("media-changed", function () {
      applyMute();
      clearAll();
      if (P.graph) applyBg();
      previewResync();
    });
    E.on("play", function () { previewResync(); });
    E.on("pause", function () { clearAll(); });
  }

  window.EditorAudio = {
    mountLine: mountLine,
    mountPanel: mountPanel,
    setMute: setMute,
    _debug: debug,
    _tick: tick
  };

  subscribe();
  if (window.Editor) {
    if (window.Editor.mountAudio) window.Editor.mountAudio();
    wirePreview();
  }
})();
