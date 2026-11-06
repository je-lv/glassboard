// One confirmation dialog for risky actions (UI analysis F01, risk tiers). A submit button with
// data-confirm="..." asks first; data-confirm-yes and data-confirm-no name the two outcomes. The
// safe choice has the focus. Texts come from the server and are set with textContent only.
// Without JavaScript the form submits directly, and the server-side checks still apply.
(function () {
  "use strict";
  var dialog = document.getElementById("confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") { return; }
  var text = dialog.querySelector("[data-confirm-text]");
  var keep = dialog.querySelector("[data-confirm-keep]");
  var go = dialog.querySelector("[data-confirm-go]");
  var pending = null;

  document.addEventListener("submit", function (event) {
    var button = event.submitter;
    if (!button || !button.dataset.confirm) { return; }
    var form = event.target;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    event.preventDefault();
    pending = { form: form, button: button };
    text.textContent = button.dataset.confirm;
    go.textContent = button.dataset.confirmYes || "Confirm";
    keep.textContent = button.dataset.confirmNo || "Keep as is";
    go.className = button.classList.contains("danger") ? "danger" : "primary";
    dialog.showModal();
    keep.focus();
  }, true);

  go.addEventListener("click", function () {
    if (!pending) { return; }
    var p = pending;
    pending = null;
    dialog.close();
    p.form.dataset.confirmed = "1";
    if (p.form.requestSubmit) { p.form.requestSubmit(p.button); } else { p.form.submit(); }
  });
  keep.addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("click", function (event) { if (event.target === dialog) { dialog.close(); } });
  dialog.addEventListener("close", function () {
    if (pending) { pending.button.focus(); pending = null; }
  });
}());
