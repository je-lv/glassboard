(function () {
  "use strict";
  var toggle = document.getElementById("wb-focus-toggle");
  var stripBox = document.getElementById("run-strip");
  var strip = stripBox && stripBox.querySelector("details[data-persist='run-strip']");
  if (strip) {
    try { strip.open = sessionStorage.getItem("glassboard-workbench-strip") === "1"; } catch (_) {}
    stripBox.addEventListener("toggle", function (event) {
      if (event.target.matches("details[data-persist='run-strip']")) {
        try { sessionStorage.setItem("glassboard-workbench-strip", event.target.open ? "1" : "0"); } catch (_) {}
      }
    }, true);
  }
  if (!toggle) return;
  var key = "glassboard-workbench-focus";
  try { if (sessionStorage.getItem(key) === "1") document.body.classList.add("wb-focus"); } catch (_) {}
  function label() { toggle.textContent = document.body.classList.contains("wb-focus") ? "Show details" : "Focus on activity"; }
  label();
  toggle.addEventListener("click", function () {
    document.body.classList.toggle("wb-focus");
    try { sessionStorage.setItem(key, document.body.classList.contains("wb-focus") ? "1" : "0"); } catch (_) {}
    label();
  });
}());
