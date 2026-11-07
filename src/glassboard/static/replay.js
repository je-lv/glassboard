// Task replay (E44, B68): step through the recorded events. Without this script every step is shown;
// with it, steps after the current one are dimmed and Play/Pause, Step, Back and Reset move through them.
// The page itself never scrolls: only the diagram box and the step list scroll, through their own
// scrollTop. A scroll by the user inside either box stops the auto-follow until Play is pressed again.
// Keys (ignored while typing in a form field or while a dialog is open): Space plays or pauses,
// Left and Right step, Esc stops. keys.js owns the number keys, "/" and Ctrl/Cmd+K; none overlap.
(function () {
  "use strict";
  var box = document.querySelector("[data-replay]");
  if (!box) { return; }
  var controls = box.querySelector("[data-rp-controls]");
  var pos = box.querySelector("[data-rp-pos]");
  var now = box.querySelector("[data-rp-now]");
  var keysHint = box.querySelector("[data-rp-keys]");
  var playBtn = box.querySelector("[data-rp='play']");
  var diagram = box.querySelector("[data-rp-diagram]");
  var list = box.querySelector("[data-rp-list]");
  var speeds = Array.prototype.slice.call(box.querySelectorAll("[data-rp-ms]"));
  var total = box.querySelectorAll(".rp-item").length;
  var current = total;
  var timer = null;
  var follow = true;
  var ms = 900;

  // Scroll an inner box so that el is in view; never touches the page scroll position.
  function reveal(container, el, centre) {
    if (!container || !el) { return; }
    var c = container.getBoundingClientRect();
    var r = el.getBoundingClientRect();
    if (centre) {
      container.scrollTop += (r.top + r.height / 2) - (c.top + container.clientHeight / 2);
    } else if (r.top < c.top) {
      container.scrollTop += r.top - c.top;
    } else if (r.bottom > c.top + container.clientHeight) {
      container.scrollTop += r.bottom - (c.top + container.clientHeight);
    }
  }

  function show(scroll) {
    box.querySelectorAll("[data-step]").forEach(function (el) {
      var n = Number(el.dataset.step);
      el.classList.toggle("rp-hidden", n > current);
      el.classList.toggle("rp-current", n === current);
    });
    var item = box.querySelector(".rp-item[data-step='" + current + "']");
    var text = item ? Array.prototype.map.call(item.childNodes, function (n) { return n.textContent; })
      .join(" ").replace(/\s+/g, " ").trim() : "";
    pos.textContent = "Step " + current + " of " + total;
    now.textContent = text;
    now.title = text;
    if (scroll) {
      reveal(list, item, false);
      reveal(diagram, box.querySelector(".rp-step[data-step='" + current + "']"), true);
    }
  }
  function setPlaying(on) {
    playBtn.textContent = on ? "Pause" : "Play";
    playBtn.setAttribute("aria-label", on ? "Pause" : "Play");
  }
  function tick() {
    if (current >= total) { stop(); return; }
    current += 1;
    show(follow);
  }
  function start() {
    if (timer) { window.clearInterval(timer); }
    timer = window.setInterval(tick, ms);
  }
  function stop() {
    if (timer) { window.clearInterval(timer); timer = null; }
    setPlaying(false);
  }
  function play() {
    if (timer) { stop(); return; }
    if (current >= total) { current = 0; }
    follow = true;
    setPlaying(true);
    start();
  }
  function move(delta) {
    stop();
    current = Math.max(1, Math.min(total, current + delta));
    show(true);
  }
  function setSpeed(btn) {
    ms = Number(btn.dataset.rpMs) || 900;
    speeds.forEach(function (b) { b.setAttribute("aria-pressed", b === btn ? "true" : "false"); });
    if (timer) { start(); }
  }

  function userScrolled() { follow = false; }
  [diagram, list].forEach(function (el) {
    if (!el) { return; }
    el.addEventListener("wheel", userScrolled, { passive: true });
    el.addEventListener("touchmove", userScrolled, { passive: true });
    el.addEventListener("pointerdown", function (e) { if (e.target === el) { userScrolled(); } });   // scrollbar drag
    el.addEventListener("keydown", function (e) {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].indexOf(e.key) !== -1) { userScrolled(); }
    });
  });

  function typing(target) {
    if (!target || !target.closest) { return false; }
    return Boolean(target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
  }
  function activatable(target) {
    return Boolean(target && target.closest && target.closest("button, a[href], summary"));
  }
  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented || typing(e.target)) { return; }
    if (document.querySelector("dialog[open]")) { return; }
    if (e.key === " " || e.key === "Spacebar") {
      if (activatable(e.target)) { return; }      // Space on a focused button or link keeps its own meaning
      e.preventDefault(); play();
    } else if (e.key === "ArrowRight") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); move(-1); }
    else if (e.key === "Escape") { if (timer) { e.preventDefault(); stop(); } }
  });

  box.classList.add("rp-js");
  controls.hidden = false;
  now.hidden = false;
  keysHint.hidden = false;
  speeds.forEach(function (b) { b.addEventListener("click", function () { setSpeed(b); }); });
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var initial = speeds.filter(function (b) { return b.dataset.rpSpeedName === (reduced ? "slow" : "normal"); })[0];
  if (initial) { setSpeed(initial); }
  box.querySelector("[data-rp='step']").addEventListener("click", function () { move(1); });
  box.querySelector("[data-rp='back']").addEventListener("click", function () { move(-1); });
  box.querySelector("[data-rp='reset']").addEventListener("click", function () { stop(); current = 1; show(true); });
  playBtn.addEventListener("click", play);
  setPlaying(false);
  show(false);
}());
