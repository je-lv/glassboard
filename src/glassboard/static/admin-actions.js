(function () {
  "use strict";
  var dialog = document.getElementById("revoke-token-dialog");
  if (!dialog || typeof dialog.showModal !== "function") return;

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-revoke-token]");
    if (opener) {
      dialog.querySelector("#revoke-token-handle").textContent = "@" + opener.dataset.revokeHandle;
      dialog.querySelector("#revoke-token-id").textContent = opener.dataset.revokeToken;
      dialog.querySelector("#revoke-token-field").value = opener.dataset.revokeToken;
      dialog.querySelector('[name="confirm"]').checked = false;
      dialog.showModal();
      return;
    }
    if (event.target.closest("[data-revoke-close]")) dialog.close();
  });

  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });
}());
