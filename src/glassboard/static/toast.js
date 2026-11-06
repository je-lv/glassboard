// Action feedback toasts (B34). The server renders the message; this file only tidies up:
// the address bar loses ?ok=/?err= so a reload does not repeat the toast, success fades after
// eight seconds, and the close link removes a toast without reloading. Without JavaScript the
// toasts stay visible and the close link reloads the clean page.
(function () {
  var box = document.getElementById("toasts");
  if (!box) return;
  var clean = box.getAttribute("data-clean");
  if (clean && window.history && history.replaceState) {
    history.replaceState(history.state, "", clean + window.location.hash);
  }
  function drop(toast) {
    toast.classList.add("toast-hide");
    window.setTimeout(function () {
      toast.remove();
      if (!box.querySelector(".toast")) box.remove();
    }, 250);
  }
  box.querySelectorAll(".toast").forEach(function (toast) {
    var close = toast.querySelector(".toast-close");
    if (close) close.addEventListener("click", function (e) { e.preventDefault(); drop(toast); });
    if (toast.classList.contains("toast-ok")) {
      var timer = window.setTimeout(function () { drop(toast); }, 8000);
      toast.addEventListener("mouseenter", function () { window.clearTimeout(timer); });
      toast.addEventListener("focusin", function () { window.clearTimeout(timer); });
    }
  });
})();
