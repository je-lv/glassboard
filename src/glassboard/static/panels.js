(function () {
  "use strict";
  document.addEventListener("click", function (event) {
    var open = event.target.closest("[data-panel-open]");
    if (open) {
      var panel = document.getElementById(open.dataset.panelOpen);
      if (panel && panel.showModal) panel.showModal();
      return;
    }
    var close = event.target.closest("[data-panel-close]");
    if (close) {
      var dialog = close.closest("dialog");
      if (dialog) dialog.close();
    }
  });
}());
