// Browser notifications for humans: every 30 s, while logged in, ask the board how many items need
// attention (a number only) and notify when it grows. Nothing leaves the LAN; no participant text is
// ever shown in a notification. The user turns it on with the "Notify me" button on the home page.
(function () {
  "use strict";
  if (!("Notification" in window)) { return; }
  var key = "glassboard-attention-count";
  var button = document.getElementById("notify-enable");
  if (button && Notification.permission === "default") {
    button.hidden = false;
    button.addEventListener("click", function () {
      Notification.requestPermission().then(function () { button.hidden = true; });
    });
  }
  function check() {
    fetch("/attention-count", { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || typeof j.count !== "number") { return; }
        var last = parseInt(sessionStorage.getItem(key) || "0", 10);
        sessionStorage.setItem(key, String(j.count));
        if (j.count > last && Notification.permission === "granted") {
          new Notification("Glassboard", { body: j.count + " item(s) need your attention", tag: "glassboard-attention" });
        }
      })
      .catch(function () { /* the next tick tries again */ });
  }
  check();
  setInterval(check, 30000);
}());
