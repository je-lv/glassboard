// Apply tab-local dismissals before the body is painted, including on navigation/reload.
(function () {
  "use strict";
  try {
    var prefix = "glassboard:";
    var suffix = ":dismissed:" + location.origin;
    if (sessionStorage.getItem(prefix + "plain-http" + suffix) === "1") {
      document.documentElement.classList.add("dismiss-plain-http");
    }
    if (sessionStorage.getItem(prefix + "read-only" + suffix) === "1") {
      document.documentElement.classList.add("dismiss-read-only");
    }
  } catch (_) { /* storage may be disabled */ }
}());
