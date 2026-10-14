// Dismiss informational banners for this browser tab's session.
(function () {
  "use strict";
  function bind(bannerId, buttonId, name) {
    var banner = document.getElementById(bannerId);
    var close = document.getElementById(buttonId);
    if (!banner || !close) { return; }
    var key = "glassboard:" + name + ":dismissed:" + location.origin;
    try {
      if (sessionStorage.getItem(key) === "1") { banner.hidden = true; }
    } catch (_) { /* storage may be disabled; the button still works */ }
    close.addEventListener("click", function () {
      banner.hidden = true;
      document.documentElement.classList.add("dismiss-" + name);
      try { sessionStorage.setItem(key, "1"); } catch (_) { /* tab-local dismissal still works */ }
    });
  }
  bind("plain-http-banner", "dismiss-plain-http", "plain-http");
  bind("read-only-banner", "dismiss-read-only", "read-only");
}());
