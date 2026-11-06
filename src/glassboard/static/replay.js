// Task replay (E44): step through the recorded events. Without this script every step is shown;
// with it, steps after the current one are dimmed and Play, Step, Back and Reset move through them.
(function () {
  "use strict";
  var box = document.querySelector("[data-replay]");
  if (!box) { return; }
  var controls = box.querySelector("[data-rp-controls]");
  var pos = box.querySelector("[data-rp-pos]");
  var playBtn = box.querySelector("[data-rp='play']");
  var total = box.querySelectorAll(".rp-item").length;
  var current = total;
  var timer = null;

  function show() {
    box.querySelectorAll("[data-step]").forEach(function (el) {
      var n = Number(el.dataset.step);
      el.classList.toggle("rp-hidden", n > current);
      el.classList.toggle("rp-current", n === current);
    });
    var item = box.querySelector(".rp-item[data-step='" + current + "']");
    pos.textContent = "Step " + current + " of " + total + (item ? ": " + item.innerText.replace(/\s+/g, " ").trim() : "");
    if (item && timer) { item.scrollIntoView({ block: "nearest" }); }
  }
  function stop() {
    if (timer) { window.clearInterval(timer); timer = null; }
    playBtn.textContent = "Play";
  }
  function play() {
    if (timer) { stop(); return; }
    if (current >= total) { current = 0; }
    playBtn.textContent = "Pause";
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    timer = window.setInterval(function () {
      if (current >= total) { stop(); return; }
      current += 1; show();
    }, reduced ? 1600 : 900);
  }
  box.classList.add("rp-js");
  controls.hidden = false;
  box.querySelector("[data-rp='step']").addEventListener("click", function () { stop(); current = Math.min(total, current + 1); show(); });
  box.querySelector("[data-rp='back']").addEventListener("click", function () { stop(); current = Math.max(1, current - 1); show(); });
  box.querySelector("[data-rp='reset']").addEventListener("click", function () { stop(); current = 1; show(); });
  playBtn.addEventListener("click", play);
  show();
}());
