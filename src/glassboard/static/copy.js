// Copy buttons: <button class="copy" data-copy="ID"> copies the text of element ID.
// Reads textContent only; nothing here builds HTML.
(function () {
  "use strict";
  function selectText(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  document.querySelectorAll("button.copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.dataset.copy);
      if (!el) { return; }
      var done = function () { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 2000); };
      // The clipboard API needs a secure context (https or localhost); elsewhere, select the text for Ctrl+C.
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(el.textContent).then(done, function () { selectText(el); });
      } else {
        selectText(el);
        btn.textContent = "Selected: press Ctrl+C";
      }
    });
  });
}());
