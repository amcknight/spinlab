// SpinLab dashboard polling logic
(function () {
  "use strict";

  var POLL_MS = 1000;

  // DOM refs
  var modeIdle = document.getElementById("mode-idle");
  var modeRef = document.getElementById("mode-reference");
  var modePractice = document.getElementById("mode-practice");
  var sessionTimer = document.getElementById("session-timer");
  var csGoal = document.getElementById("cs-goal");
  var csDifficulty = document.getElementById("cs-difficulty");
  var csAttempts = document.getElementById("cs-attempts");
  var miTier = document.getElementById("mi-tier");
  var queueList = document.getElementById("queue-list");
  var recentList = document.getElementById("recent-list");
  var statsLine = document.getElementById("stats-line");

  function showMode(mode) {
    modeIdle.hidden = mode !== "idle";
    modeRef.hidden = mode !== "reference";
    modePractice.hidden = mode !== "practice";
  }

  function tierClass(ef, reps) {
    if (!reps || reps === 0) return "tier-new";
    if (ef < 1.8) return "tier-struggling";
    if (ef < 2.5) return "tier-normal";
    return "tier-strong";
  }

  function tierLabel(ef, reps) {
    if (!reps || reps === 0) return "New";
    if (ef < 1.8) return "Struggling";
    if (ef < 2.5) return "Normal";
    return "Strong";
  }

  function ratingClass(rating) {
    if (!rating) return "";
    return "rating-" + rating;
  }

  function formatTime(ms) {
    if (!ms) return "\u2014";
    return (ms / 1000).toFixed(1) + "s";
  }

  function elapsedStr(startedAt) {
    if (!startedAt) return "";
    // DB stores UTC without "Z" suffix — append it so JS parses as UTC
    var ts = startedAt.endsWith("Z") ? startedAt : startedAt + "Z";
    var start = new Date(ts);
    var diff = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
    var m = Math.floor(diff / 60);
    var s = diff % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function update(data) {
    showMode(data.mode);

    // Session timer
    if (data.session && data.session.started_at) {
      sessionTimer.textContent = elapsedStr(data.session.started_at);
    } else {
      sessionTimer.textContent = "";
    }

    if (data.mode !== "practice") return;

    // Current split
    var cs = data.current_split;
    if (cs) {
      var label = cs.description || cs.id;
      csGoal.textContent = label + (cs.goal && cs.goal !== "normal" ? " (" + cs.goal + ")" : "");
      var tc = tierClass(cs.ease_factor, cs.repetitions);
      csDifficulty.className = "cs-difficulty " + tc;
      csDifficulty.textContent = tierLabel(cs.ease_factor, cs.repetitions);
      csAttempts.textContent = "Attempts: " + (cs.attempt_count || 0);

      // Model insight: ease factor + repetitions
      miTier.className = "mi-line " + tc;
      miTier.textContent = tierLabel(cs.ease_factor, cs.repetitions) +
        " \u2014 Ease " + (cs.ease_factor || 2.5).toFixed(2) +
        ", " + (cs.repetitions || 0) + " reps";
    }

    // Queue
    queueList.innerHTML = "";
    (data.queue || []).forEach(function (q) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = q.description || q.goal || q.id;
      var diff = document.createElement("span");
      diff.className = tierClass(q.ease_factor, q.repetitions);
      diff.textContent = tierLabel(q.ease_factor, q.repetitions);
      li.appendChild(name);
      li.appendChild(diff);
      queueList.appendChild(li);
    });

    // Recent results
    recentList.innerHTML = "";
    (data.recent || []).forEach(function (r) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = r.description || r.goal;
      var info = document.createElement("span");
      info.className = ratingClass(r.rating);
      info.textContent = formatTime(r.time_ms) + " " + (r.rating || "");
      li.appendChild(name);
      li.appendChild(info);
      recentList.appendChild(li);
    });

    // Session stats
    if (data.session) {
      var sa = data.session.splits_attempted || 0;
      var sc = data.session.splits_completed || 0;
      statsLine.textContent = sc + "/" + sa + " cleared | " +
        elapsedStr(data.session.started_at);
    }
  }

  function poll() {
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(update)
      .catch(function () { /* silently retry next tick */ });
  }

  poll();
  setInterval(poll, POLL_MS);
})();
