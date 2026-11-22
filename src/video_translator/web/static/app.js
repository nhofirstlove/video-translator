/* Video Translator front end. No dependencies. All data is inserted with textContent. */
(function () {
  "use strict";

  var ALLOWED = [".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".ts"];
  var STAGE_LABELS = {
    extract_audio: "Extracting audio",
    transcribe: "Listening to the Chinese speech",
    translate: "Translating to Vietnamese",
    review: "Waiting for your review",
    subtitles: "Writing subtitles",
    synthesize: "Creating the Vietnamese voice",
    align: "Fitting the voice to the timing",
    mix: "Mixing voice with the original sound",
    render: "Building the final video"
  };
  var STATUS_LABELS = {
    pending: "Not started",
    running: "Running",
    awaiting_review: "Needs your review",
    paused: "Paused",
    done: "Finished",
    failed: "Failed",
    queued: "Waiting"
  };
  var CHECK_LABELS = {
    Python: "Python",
    ffmpeg: "ffmpeg (video tool)",
    ffprobe: "ffprobe (video tool)",
    Hardware: "Computer",
    Device: "Processing device",
    GEMINI_API_KEY: "Gemini key (translation)",
    ELEVENLABS_API_KEY: "ElevenLabs key (voice)",
    Config: "Settings file"
  };

  // ---------- helpers ----------
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

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    if (options.json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.json);
    }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) throw new Error(errorText(data, res.status));
        return data;
      });
    });
  }

  function errorText(data, status) {
    if (data && typeof data.detail === "string") return data.detail;
    if (data && Array.isArray(data.detail) && data.detail.length) {
      return data.detail.map(function (d) { return d.msg || "invalid value"; }).join("; ");
    }
    return "Request failed (" + status + ").";
  }

  var toastTimer = null;
  function toast(message) {
    var node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 5000);
  }

  function formatTime(seconds) {
    var total = Math.max(0, seconds);
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total - h * 3600 - m * 60;
    var sec = (s < 10 ? "0" : "") + s.toFixed(1);
    var min = (h > 0 && m < 10 ? "0" : "") + m;
    return (h > 0 ? h + ":" : "") + min + ":" + sec;
  }

  function hasAllowedExtension(name) {
    var lower = name.toLowerCase();
    return ALLOWED.some(function (ext) { return lower.slice(-ext.length) === ext; });
  }

  // ---------- setup panel ----------
  function loadSetup() {
    $("setup-summary").textContent = "Checking...";
    api("/api/doctor").then(function (data) {
      var list = $("setup-list");
      list.textContent = "";
      var problems = 0;
      data.checks.forEach(function (check) {
        if (check.status !== "OK") problems += 1;
        list.appendChild(el("li", {}, [
          el("span", { class: "badge " + check.status, text: check.status }),
          el("span", { class: "check-name", text: CHECK_LABELS[check.name] || check.name }),
          el("span", { class: "muted", text: check.message })
        ]));
      });
      $("setup-summary").textContent = problems === 0
        ? "Everything is ready."
        : problems + (problems === 1 ? " item needs" : " items need") + " attention before you can translate.";
    }).catch(function (err) {
      $("setup-summary").textContent = "Could not run the setup check: " + err.message;
    });
    api("/api/config/summary").then(function (cfg) {
      var parts = [];
      parts.push("Review before dubbing: " + (cfg.review ? "on" : "off"));
      parts.push("Chunks: " + cfg.chunk_minutes + " min");
      var el11 = cfg.elevenlabs || {};
      if (el11.characters_used_this_month != null) {
        parts.push("ElevenLabs this month: " + el11.characters_used_this_month + " / " +
          el11.monthly_character_budget + " characters");
      } else {
        parts.push("ElevenLabs budget: " + el11.monthly_character_budget + " characters per month");
      }
      $("config-summary").textContent = parts.join("  |  ");
    }).catch(function () { $("config-summary").textContent = ""; });
  }

  // ---------- upload ----------
  var chosenFile = null;

  function setFile(file) {
    chosenFile = file;
    $("upload-error").textContent = "";
    $("file-name").textContent = file ? file.name : "";
    if (file && !hasAllowedExtension(file.name)) {
      $("upload-error").textContent = "Unsupported file type. Please choose one of: " +
        ALLOWED.map(function (e) { return e.slice(1); }).join(", ") + ".";
      chosenFile = null;
    }
    $("upload-btn").disabled = !chosenFile;
  }

  function setupUpload() {
    var input = $("file-input");
    var zone = $("drop-zone");
    input.addEventListener("change", function () { setFile(input.files[0] || null); });
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.remove("over"); });
    });
    zone.addEventListener("drop", function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) setFile(files[0]);
    });
    $("upload-form").addEventListener("submit", function (e) {
      e.preventDefault();
      if (chosenFile) upload(chosenFile);
    });
  }

  function upload(file) {
    var form = new FormData();
    form.append("file", file);
    var xhr = new XMLHttpRequest();
    var wrap = $("upload-progress-wrap");
    var bar = $("upload-bar");
    var meter = $("upload-progress");
    $("upload-btn").disabled = true;
    $("upload-error").textContent = "";
    wrap.hidden = false;
    bar.style.width = "0%";
    $("upload-pct").textContent = "Uploading 0%";

    xhr.upload.addEventListener("progress", function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = pct + "%";
      meter.setAttribute("aria-valuenow", String(pct));
      $("upload-pct").textContent = pct < 100 ? "Uploading " + pct + "%" : "Saving the file...";
    });
    xhr.addEventListener("load", function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (err) { data = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        wrap.hidden = true;
        $("file-input").value = "";
        setFile(null);
        toast("Upload finished. Translation started.");
        refreshJobs();
      } else {
        $("upload-error").textContent = errorText(data, xhr.status);
        $("upload-btn").disabled = !chosenFile;
        wrap.hidden = true;
      }
    });
    xhr.addEventListener("error", function () {
      $("upload-error").textContent = "Upload failed. Is the program still running?";
      $("upload-btn").disabled = !chosenFile;
      wrap.hidden = true;
    });
    xhr.open("POST", "/api/jobs?start=true");
    xhr.send(form);
  }

  // ---------- jobs ----------
  var cards = {}; // job id -> {node, parts, key, reviewRow}
  var pollTimer = null;
  var dirtyJobs = {}; // job id -> number of changed rows

  function anyDirty() {
    return Object.keys(dirtyJobs).some(function (id) { return dirtyJobs[id] > 0; });
  }

  window.addEventListener("beforeunload", function (e) {
    if (anyDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  function displayStatus(job) {
    return job.queued ? "queued" : job.status;
  }

  function stageLabel(stage) {
    return STAGE_LABELS[stage] || stage || "";
  }

  function refreshJobs() {
    clearTimeout(pollTimer);
    return api("/api/jobs").then(function (jobs) {
      $("jobs-error").textContent = "";
      renderJobs(jobs);
      var active = jobs.some(function (j) { return j.running || j.queued; });
      pollTimer = setTimeout(refreshJobs, active ? 2000 : 8000);
    }).catch(function (err) {
      $("jobs-error").textContent = "Cannot reach the program: " + err.message;
      pollTimer = setTimeout(refreshJobs, 5000);
    });
  }

  function renderJobs(jobs) {
    var container = $("jobs");
    $("jobs-empty").hidden = jobs.length > 0;
    var seen = {};
    jobs.forEach(function (job, position) {
      seen[job.id] = true;
      var card = cards[job.id];
      if (!card) {
        card = createCard(job);
        cards[job.id] = card;
      }
      updateCard(card, job);
      var current = container.children[position];
      if (current !== card.node) container.insertBefore(card.node, current || null);
    });
    Object.keys(cards).forEach(function (id) {
      if (!seen[id]) {
        cards[id].node.remove();
        delete cards[id];
        delete dirtyJobs[id];
      }
    });
  }

  function createCard(job) {
    var parts = {
      title: el("h3", { class: "job-title" }),
      badge: el("span", { class: "badge" }),
      message: el("p", { class: "job-message" }),
      error: el("p", { class: "job-error", role: "alert" }),
      progress: el("div", { class: "progress-wrap" }),
      bar: el("div", { class: "progress-bar" }),
      stage: el("span", { class: "muted small-text" }),
      actions: el("div", { class: "job-actions" }),
      review: el("div", { class: "review", hidden: true })
    };
    var meter = el("div", { class: "progress", role: "progressbar", "aria-label": "Job progress",
      "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" }, [parts.bar]);
    parts.meter = meter;
    parts.progress.appendChild(meter);
    parts.progress.appendChild(parts.stage);
    var node = el("article", { class: "job", "aria-label": "Job " + job.filename }, [
      el("div", { class: "job-head" }, [parts.title, parts.badge]),
      el("div", { class: "job-body" }, [
        parts.message, parts.error, parts.progress, parts.actions, parts.review
      ])
    ]);
    return { node: node, parts: parts, key: null, reviewJob: null };
  }

  function updateCard(card, job) {
    var p = card.parts;
    var status = displayStatus(job);
    p.title.textContent = job.filename;
    p.badge.className = "badge " + status;
    p.badge.textContent = STATUS_LABELS[status] || status;

    var message = job.message;
    if (job.running && job.current_stage) message = stageLabel(job.current_stage) + "...";
    if (job.status === "pending" && !job.queued && !message) message = "Ready to start.";
    p.message.textContent = message || "";
    p.message.hidden = !message;
    p.error.textContent = job.error || "";
    p.error.hidden = !job.error || job.status === "done";

    var showProgress = job.running || job.queued || (job.status !== "done" && job.progress.index > 0);
    p.progress.hidden = !showProgress;
    var pct = Math.round((job.progress.fraction || 0) * 100);
    p.bar.style.width = pct + "%";
    p.meter.setAttribute("aria-valuenow", String(pct));
    p.stage.textContent = job.progress.total
      ? "Step " + Math.min(job.progress.index + (job.running ? 1 : 0), job.progress.total) + " of " + job.progress.total
      : "";

    // Rebuild the buttons only when the state changes, so focus is not lost while polling.
    var key = [status, job.downloads.video, job.downloads.srt].join("|");
    if (card.key !== key) {
      card.key = key;
      buildActions(card, job);
    }

    var wantReview = job.status === "awaiting_review" && !job.queued;
    if (wantReview && card.reviewJob !== job.id) {
      card.reviewJob = job.id;
      buildReview(card, job);
    } else if (!wantReview && card.reviewJob) {
      card.reviewJob = null;
      p.review.hidden = true;
      p.review.textContent = "";
      dirtyJobs[job.id] = 0;
    }
  }

  function buildActions(card, job) {
    var actions = card.parts.actions;
    actions.textContent = "";
    if (job.queued || job.running) return;
    if (job.status === "pending") {
      actions.appendChild(actionButton("Start", "primary", job, "resume"));
    } else if (job.status === "paused") {
      actions.appendChild(actionButton("Resume", "primary", job, "resume"));
    } else if (job.status === "failed") {
      actions.appendChild(actionButton("Try again (resume)", "primary", job, "resume"));
    } else if (job.status === "done") {
      if (job.downloads.video) {
        actions.appendChild(el("a", { class: "btn primary", href: "/api/jobs/" + job.id + "/download/video",
          download: "", text: "Download video" }));
      }
      if (job.downloads.srt) {
        actions.appendChild(el("a", { class: "btn", href: "/api/jobs/" + job.id + "/download/srt",
          download: "", text: "Download subtitles (.srt)" }));
      }
      if (!job.downloads.video && !job.downloads.srt) {
        actions.appendChild(el("span", { class: "muted", text: "Finished, but no output file was found." }));
      }
    }
  }

  function actionButton(label, kind, job, action) {
    var btn = el("button", { type: "button", class: "btn " + kind, text: label });
    btn.addEventListener("click", function () {
      btn.disabled = true;
      api("/api/jobs/" + job.id + "/" + action, { method: "POST" }).then(function () {
        refreshJobs();
      }).catch(function (err) {
        toast(err.message);
        btn.disabled = false;
      });
    });
    return btn;
  }

  // ---------- review table ----------
  function buildReview(card, job) {
    var review = card.parts.review;
    review.hidden = false;
    review.textContent = "";
    review.appendChild(el("p", { class: "muted", text: "Loading the translation..." }));
    api("/api/jobs/" + job.id + "/segments").then(function (segments) {
      if (card.reviewJob !== job.id) return;
      renderReview(card, job, segments);
    }).catch(function (err) {
      review.textContent = "";
      review.appendChild(el("p", { class: "error-text", text: "Could not load the translation: " + err.message }));
      card.reviewJob = null; // retry on the next poll
    });
  }

  function autosize(area) {
    area.style.height = "auto";
    area.style.height = Math.max(area.scrollHeight + 2, 52) + "px";
  }

  function renderReview(card, job, segments) {
    var review = card.parts.review;
    review.textContent = "";
    var original = {};
    var areas = {};
    var rows = {};
    segments.forEach(function (s) { original[s.index] = s.text_vi; });

    var note = el("span", { class: "dirty-note", role: "status" });
    var saveBtn = el("button", { type: "button", class: "btn", text: "Save edits", disabled: true });
    var approveBtn = el("button", { type: "button", class: "btn primary", text: "Approve and continue" });
    var bar = el("div", { class: "review-bar" }, [
      saveBtn, approveBtn, note
    ]);

    function changedIndexes() {
      return Object.keys(areas).filter(function (i) { return areas[i].value !== original[i]; });
    }
    function refreshDirty() {
      var changed = changedIndexes();
      dirtyJobs[job.id] = changed.length;
      saveBtn.disabled = changed.length === 0;
      note.textContent = changed.length ? changed.length + " unsaved " + (changed.length === 1 ? "change" : "changes") : "";
      Object.keys(rows).forEach(function (i) {
        rows[i].classList.toggle("changed", areas[i].value !== original[i]);
      });
    }

    function save() {
      var changed = changedIndexes();
      if (!changed.length) return Promise.resolve();
      for (var k = 0; k < changed.length; k++) {
        if (!areas[changed[k]].value.trim()) {
          areas[changed[k]].focus();
          return Promise.reject(new Error("Line " + (Number(changed[k]) + 1) + " is empty. Write a translation or restore the original."));
        }
      }
      var edits = changed.map(function (i) { return { index: Number(i), text_vi: areas[i].value }; });
      return api("/api/jobs/" + job.id + "/segments", { method: "PUT", json: { edits: edits } }).then(function () {
        changed.forEach(function (i) { original[i] = areas[i].value.trim(); areas[i].value = original[i]; });
        refreshDirty();
      });
    }

    saveBtn.addEventListener("click", function () {
      saveBtn.disabled = true;
      save().then(function () { toast("Edits saved."); }).catch(function (err) {
        toast(err.message);
        refreshDirty();
      });
    });
    approveBtn.addEventListener("click", function () {
      approveBtn.disabled = true;
      save().then(function () {
        return api("/api/jobs/" + job.id + "/approve", { method: "POST" });
      }).then(function () {
        dirtyJobs[job.id] = 0;
        refreshJobs();
      }).catch(function (err) {
        toast(err.message);
        approveBtn.disabled = false;
      });
    });

    var tbody = el("tbody");
    segments.forEach(function (seg) {
      var area = el("textarea", { class: "vi", lang: "vi", rows: "2",
        "aria-label": "Vietnamese text for line " + (seg.index + 1) });
      area.value = seg.text_vi;
      area.addEventListener("input", function () { autosize(area); refreshDirty(); });
      areas[seg.index] = area;
      var viCell = el("td", {}, [area]);
      (seg.flags || []).forEach(function (flag) {
        viCell.appendChild(el("span", { class: "flag", text: flag.replace(/_/g, " ") }));
      });
      var row = el("tr", { class: "seg-row" }, [
        el("td", { class: "seg-time", text: formatTime(seg.start) + " - " + formatTime(seg.end) }),
        el("td", { class: "seg-zh", lang: "zh", text: seg.text_zh }),
        viCell
      ]);
      rows[seg.index] = row;
      tbody.appendChild(row);
    });

    var table = el("table", { class: "seg-table" }, [
      el("caption", { class: "visually-hidden", text: "Translation review" }),
      el("thead", {}, [el("tr", {}, [
        el("th", { scope: "col", text: "Time" }),
        el("th", { scope: "col", text: "Chinese (original)" }),
        el("th", { scope: "col", text: "Vietnamese (editable)" })
      ])]),
      tbody
    ]);

    review.appendChild(el("h4", { text: "Review the Vietnamese translation (" + segments.length + " lines)" }));
    review.appendChild(el("p", { class: "muted small-text",
      text: "Fix any mistakes, then press Approve and continue. The voice is generated only after you approve." }));
    review.appendChild(bar);
    review.appendChild(table);
    review.appendChild(el("div", { class: "review-bar" }, [el("span", { class: "spacer" })]));
    Object.keys(areas).forEach(function (i) { autosize(areas[i]); });
    refreshDirty();
  }

  // ---------- start ----------
  document.addEventListener("DOMContentLoaded", function () {
    $("setup-refresh").addEventListener("click", loadSetup);
    setupUpload();
    loadSetup();
    refreshJobs();
  });
})();
