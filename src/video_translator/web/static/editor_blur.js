/* Editor blur box: a blurred box over the burned-in Chinese subtitles, with the Vietnamese
 * subtitles drawn on top of it. Plain browser JS, no build step. window.Editor comes from editor.js.
 * Sizes are PIXELS OF THE ORIGINAL VIDEO; the box on the player is drawn scaled. The blur seen
 * here is only an approximation: the exact picture comes from "Apply and re-render".
 * All server text goes into the DOM with textContent only. */
(function () {
  "use strict";

  var EDITABLE = { awaiting_review: 1, done: 1, paused: 1, failed: 1 };
  var MIN_BOX = 16;
  var SIGMA_PER_STRENGTH = 0.4;
  var SAVE_DELAY = 500;

  function ed() { return window.Editor || null; }
  function $(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function int(v, fallback) {
    if (v === null || v === undefined || v === "") return fallback;
    v = Math.round(Number(v));
    return isFinite(v) ? v : fallback;
  }

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

  var S = null;           // current settings (video pixels): enabled, x, y, width, height, strength, tint, font_size
  var defaults = null;
  var videoSize = null;   // {width, height} of the original video
  var customised = false;
  var mounted = false;
  var saveTimer = 0, saveSeq = 0, dragging = false;
  var els = {};           // panel inputs
  var boxEl = null, textEl = null;
  var stageEl = null;

  // ---------- geometry ----------
  function frame() {
    var v = ed() && ed().video;
    if (videoSize && videoSize.width) return videoSize;
    if (v && v.videoWidth) return { width: v.videoWidth, height: v.videoHeight };
    return null;
  }
  function scale() {
    var f = frame();
    return f && stageEl && stageEl.clientWidth ? stageEl.clientWidth / f.width : 0;
  }
  function sanitize() {
    var f = frame();
    S.width = Math.max(MIN_BOX, int(S.width, 1100));
    S.height = Math.max(MIN_BOX, int(S.height, 130));
    if (f) {
      S.width = Math.min(S.width, f.width);
      S.height = Math.min(S.height, f.height);
      S.x = clamp(int(S.x, Math.round((f.width - S.width) / 2)), 0, f.width - S.width);
      S.y = clamp(int(S.y, f.height - S.height - Math.round(f.height * 0.06)), 0, f.height - S.height);
    }
  }
  function editable() {
    var st = ed() && ed().state && ed().state.job;
    return !!(st && EDITABLE[st.status]) && !ed().isDub();
  }

  // ---------- the box over the video ----------
  function buildBox() {
    stageEl = $("ed-stage");
    if (!stageEl || boxEl) return;
    textEl = h("div", { class: "eb-text", lang: "vi", "aria-hidden": "true" });
    boxEl = h("div", { class: "eb-box", hidden: true, "aria-hidden": "true" }, [textEl]);
    ["n", "s", "e", "w", "ne", "nw", "se", "sw"].forEach(function (dir) {
      var handle = h("div", { class: "eb-handle eb-" + dir });
      handle.dataset.dir = dir;
      boxEl.appendChild(handle);
    });
    stageEl.appendChild(boxEl);
    boxEl.addEventListener("pointerdown", onPointerDown);
    if (window.ResizeObserver) new ResizeObserver(layout).observe(stageEl);
    window.addEventListener("resize", layout);
    ed().video.addEventListener("loadedmetadata", layout);
    ed().video.addEventListener("timeupdate", updateText);
    ed().video.addEventListener("seeked", updateText);
    ed().video.addEventListener("play", loop);
  }

  function layout() {
    if (ed() && ed().emit) ed().emit("blur-changed");
    if (!boxEl || !S) return;
    var k = scale();
    var show = S.enabled && k > 0 && !ed().isDub();
    boxEl.hidden = !show;
    stageEl.classList.toggle("eb-on", show);
    if (!show) return;
    boxEl.style.left = (S.x * k) + "px";
    boxEl.style.top = (S.y * k) + "px";
    boxEl.style.width = (S.width * k) + "px";
    boxEl.style.height = (S.height * k) + "px";
    var px = Math.max(0.5, S.strength * SIGMA_PER_STRENGTH * k);
    var filter = "blur(" + px.toFixed(1) + "px)";
    boxEl.style.backdropFilter = filter;
    boxEl.style.webkitBackdropFilter = filter;
    boxEl.style.background = "rgba(0,0,0," + S.tint + ")";
    boxEl.classList.toggle("eb-ro", !editable());
    textEl.style.fontSize = Math.max(6, S.font_size * k) + "px";
    textEl.style.webkitTextStroke = "0";
    textEl.style.textShadow = outlineShadow(Math.max(1, Math.round(S.font_size / 14)) * k);
    updateText();
  }
  function outlineShadow(r) {
    var parts = [], i;
    for (i = 0; i < 8; i++) {
      var a = i * Math.PI / 4;
      parts.push((Math.cos(a) * r).toFixed(1) + "px " + (Math.sin(a) * r).toFixed(1) + "px 0 #000");
    }
    return parts.join(",");
  }

  function currentLine(t) {
    var segs = (ed().state && ed().state.segments) || [];
    for (var i = 0; i < segs.length; i++) {
      if (t >= segs[i].start && t < segs[i].end) return segs[i];
    }
    return null;
  }
  function updateText() {
    if (!textEl || boxEl.hidden) return;
    var seg = currentLine(ed().video.currentTime || 0);
    var text = seg && $("ed-opt-subs") && $("ed-opt-subs").checked ? (seg.text_vi || "") : "";
    if (textEl.textContent !== text) textEl.textContent = text;
  }
  function loop() {
    updateText();
    if (!ed().video.paused && !ed().video.ended) requestAnimationFrame(loop);
  }

  // ---------- dragging ----------
  function onPointerDown(ev) {
    if (!editable() || ev.button > 0) return;
    var dir = ev.target && ev.target.dataset ? ev.target.dataset.dir : "";
    var f = frame(), k = scale();
    if (!f || !k) return;
    ev.preventDefault();
    dragging = true;
    boxEl.setPointerCapture(ev.pointerId);
    var start = { cx: ev.clientX, cy: ev.clientY, x: S.x, y: S.y, w: S.width, h: S.height };
    function move(e) {
      var dx = (e.clientX - start.cx) / k, dy = (e.clientY - start.cy) / k;
      var x = start.x, y = start.y, w = start.w, hh = start.h;
      if (!dir) {
        x = clamp(start.x + dx, 0, f.width - w);
        y = clamp(start.y + dy, 0, f.height - hh);
      } else {
        if (dir.indexOf("e") >= 0) w = clamp(start.w + dx, MIN_BOX, f.width - start.x);
        if (dir.indexOf("s") >= 0) hh = clamp(start.h + dy, MIN_BOX, f.height - start.y);
        if (dir.indexOf("w") >= 0) {
          var nx = clamp(start.x + dx, 0, start.x + start.w - MIN_BOX);
          w = start.w + (start.x - nx); x = nx;
        }
        if (dir.indexOf("n") >= 0) {
          var ny = clamp(start.y + dy, 0, start.y + start.h - MIN_BOX);
          hh = start.h + (start.y - ny); y = ny;
        }
      }
      S.x = Math.round(x); S.y = Math.round(y); S.width = Math.round(w); S.height = Math.round(hh);
      layout(); syncInputs();
    }
    function up(e) {
      dragging = false;
      boxEl.removeEventListener("pointermove", move);
      boxEl.removeEventListener("pointerup", up);
      boxEl.removeEventListener("pointercancel", up);
      try { boxEl.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      scheduleSave();
    }
    boxEl.addEventListener("pointermove", move);
    boxEl.addEventListener("pointerup", up);
    boxEl.addEventListener("pointercancel", up);
  }

  // ---------- panel ----------
  function numberField(key, label, min, max, unit) {
    var input = h("input", { type: "number", min: min, max: max, step: 1, id: "eb-" + key, inputmode: "numeric" });
    input.addEventListener("input", function () { onField(key, input); });
    els[key] = input;
    return h("label", { class: "eb-field" }, [h("span", { text: label + (unit ? " (" + unit + ")" : "") }), input]);
  }
  function rangeField(key, label, min, max, step, fmt) {
    var input = h("input", { type: "range", min: min, max: max, step: step, id: "eb-" + key });
    var value = h("output", { class: "eb-value", for: "eb-" + key });
    input.addEventListener("input", function () { onField(key, input); value.textContent = fmt(Number(input.value)); });
    els[key] = input; els[key + "Out"] = value; input.fmt = fmt;
    return h("label", { class: "eb-field eb-range" }, [h("span", { text: label }), input, value]);
  }

  function mountPanel() {
    var panel = $("blur-panel");
    if (!panel || mounted) return;
    mounted = true;
    els.enabled = h("input", { type: "checkbox", id: "eb-enabled" });
    els.enabled.addEventListener("change", function () {
      S.enabled = els.enabled.checked;
      if (S.enabled) { sanitize(); syncInputs(); }
      layout(); updateHint(); scheduleSave();
    });
    els.status = h("p", { class: "eb-status muted small-text", role: "status", "aria-live": "polite" });
    els.hint = h("p", { class: "eb-hint muted small-text" });
    els.save = h("button", { type: "button", class: "btn small", text: "Save as default for new videos" });
    els.reset = h("button", { type: "button", class: "btn small", text: "Use the default again" });
    els.save.addEventListener("click", saveDefaults);
    els.reset.addEventListener("click", resetToDefault);

    var grid = h("div", { class: "eb-grid" }, [
      numberField("x", "Left", 0, 20000, "px"), numberField("y", "Top", 0, 20000, "px"),
      numberField("width", "Width", MIN_BOX, 20000, "px"), numberField("height", "Height", MIN_BOX, 20000, "px"),
      numberField("font_size", "Vietnamese text size", 8, 300, "px")
    ]);
    var ranges = h("div", { class: "eb-ranges" }, [
      rangeField("strength", "Blur strength", 1, 100, 1, function (v) { return String(v); }),
      rangeField("tint", "Dark layer over the blur", 0, 90, 5, function (v) { return v ? v + "%" : "off"; })
    ]);
    panel.appendChild(h("section", { class: "eb-panel", "aria-labelledby": "eb-title" }, [
      h("h2", { id: "eb-title", class: "eb-title", text: "Blur the Chinese subtitles" }),
      h("label", { class: "ed-check" }, [els.enabled, h("span", { text: "Blur a box over the video and burn in the Vietnamese subtitles" })]),
      els.hint, grid, ranges,
      h("div", { class: "eb-actions" }, [els.save, els.reset]),
      els.status
    ]));
  }

  function updateHint() {
    if (!els.hint) return;
    els.hint.textContent = S && S.enabled
      ? "Drag the box on the video to move it, drag its edges or corners to resize it. The blur here is only an approximation. " +
        "Press \"Apply and re-render\" to make the real video: it is re-encoded, so it takes much longer than before."
      : "Off: the video is copied unchanged and only a separate .srt file has the Vietnamese subtitles.";
  }

  function syncInputs() {
    if (!els.x || !S) return;
    ["x", "y", "width", "height", "font_size"].forEach(function (k) {
      if (document.activeElement !== els[k]) els[k].value = S[k] === null || S[k] === undefined ? "" : String(S[k]);
    });
    els.strength.value = String(S.strength);
    els.tint.value = String(Math.round(S.tint * 100));
    els.strengthOut.textContent = els.strength.fmt(S.strength);
    els.tintOut.textContent = els.tint.fmt(Math.round(S.tint * 100));
    els.enabled.checked = !!S.enabled;
    var ro = !editable();
    ["enabled", "x", "y", "width", "height", "font_size", "strength", "tint", "save", "reset"].forEach(function (k) { els[k].disabled = ro; });
    els.reset.hidden = !customised;
    updateHint();
  }

  function onField(key, input) {
    var v = Number(input.value);
    if (!isFinite(v)) return;
    if (key === "tint") S.tint = clamp(v, 0, 90) / 100;
    else if (key === "strength") S.strength = clamp(Math.round(v), 1, 100);
    else if (key === "font_size") S.font_size = clamp(Math.round(v), 8, 300);
    else S[key] = Math.round(v);
    if (key === "x" || key === "y" || key === "width" || key === "height") sanitize();
    layout(); scheduleSave();
  }

  // ---------- saving ----------
  function say(msg, kind) {
    if (!els.status) return;
    els.status.textContent = msg;
    els.status.classList.toggle("eb-bad", kind === "error");
  }
  function body() {
    return {
      enabled: !!S.enabled, x: int(S.x, null), y: int(S.y, null),
      width: int(S.width, 1100), height: int(S.height, 130),
      strength: int(S.strength, 70), tint: Math.round(S.tint * 100) / 100, font_size: int(S.font_size, 40)
    };
  }
  function scheduleSave() {
    clearTimeout(saveTimer);
    say("Saving...");
    saveTimer = setTimeout(save, SAVE_DELAY);
  }
  function save() {
    var seq = ++saveSeq;
    sanitize();
    ed().api("PUT", "/api/jobs/" + encodeURIComponent(ed().jobId) + "/blur-settings", body()).then(function (data) {
      if (seq !== saveSeq) return;
      applyData(data, true);
      say("Saved for this video.");
    }, function (err) { say(err.message || "Could not save the blur settings.", "error"); });
  }
  function saveDefaults() {
    sanitize();
    ed().api("PUT", "/api/blur-defaults", body()).then(function (data) {
      defaults = data.defaults;
      ed().toast("Saved as the default for new videos.", "ok");
    }, function (err) { ed().toast(err.message || "Could not save the default.", "error"); });
  }
  function resetToDefault() {
    clearTimeout(saveTimer);
    saveSeq++;
    ed().api("DELETE", "/api/jobs/" + encodeURIComponent(ed().jobId) + "/blur-settings").then(function (data) {
      applyData(data, true);
      say("This video follows the default again.");
    }, function (err) { say(err.message || "Could not reset.", "error"); });
  }

  // ---------- data ----------
  function applyData(data, fromSave) {
    if (!data || !data.settings) return;
    if (dragging) return;
    S = Object.assign({}, data.settings);
    defaults = data.defaults || defaults;
    videoSize = data.video || videoSize;
    customised = !!data.customised;
    if (!fromSave) sanitize();
    syncInputs(); layout();
  }

  function init() {
    var E = ed();
    if (!E || !E.state) return;
    buildBox(); mountPanel();
    applyData(E.state.blur);
  }

  window.EditorBlur = { settings: function () { return S ? Object.assign({}, S) : null; } };

  var E0 = ed();
  if (E0) {
    E0.on("ready", init);
    E0.on("refreshed", function () { if (mounted) applyData(ed().state.blur); });
    E0.on("media-changed", function () { layout(); syncInputs(); });
    if (E0.state && E0.state.job && E0.state.job.id) init();  // loaded after the editor was ready
  }
})();
