// Keep the native disclosure control; use it to open a full dialog when supported.
(function () {
  "use strict";
  if (typeof HTMLDialogElement === "undefined" || !HTMLDialogElement.prototype.showModal) return;
  document.querySelectorAll("summary[data-open-task]").forEach(function (summary) {
    var dialog = document.getElementById(summary.dataset.openTask);
    if (!dialog) return;
    summary.setAttribute("aria-haspopup", "dialog");
    summary.addEventListener("click", function (event) {
      event.preventDefault();
      dialog.showModal();
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
  });
}());
