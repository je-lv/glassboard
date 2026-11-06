// Keyboard navigation for session pages (E42): number keys open the session tabs, "/" focuses the
// session search, Ctrl/Cmd+K opens the jump box. The jump list comes from the board as JSON and is
// inserted as text only. Keys typed into a form field are left alone.
(function () {
  "use strict";
  var nav = document.querySelector(".subnav a[data-key]");
  if (!nav) { return; }
  var dialog = document.getElementById("jump-dialog");
  var input = dialog && dialog.querySelector("[data-jump-input]");
  var list = dialog && dialog.querySelector("[data-jump-list]");
  var items = null;
  var shown = [];
  var active = 0;

  function typing(target) {
    if (!target || !target.closest) { return false; }
    return Boolean(target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
  }

  function render() {
    var words = (input.value || "").toLowerCase().split(/\s+/).filter(Boolean);
    shown = (items || []).filter(function (item) {
      var label = item.label.toLowerCase();
      return words.every(function (w) { return label.indexOf(w) !== -1; });
    }).slice(0, 50);
    active = Math.min(active, Math.max(shown.length - 1, 0));
    list.replaceChildren();
    shown.forEach(function (item, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.id = "jump-opt-" + i;
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      var kind = document.createElement("span");
      kind.className = "jump-kind";
      kind.textContent = item.kind;
      var label = document.createElement("span");
      label.textContent = item.label;
      li.append(kind, label);
      li.addEventListener("click", function () { window.location.assign(item.href); });
      list.appendChild(li);
    });
    if (shown.length) { input.setAttribute("aria-activedescendant", "jump-opt-" + active); }
    else { input.removeAttribute("aria-activedescendant"); }
    var current = document.getElementById("jump-opt-" + active);
    if (current) { current.scrollIntoView({ block: "nearest" }); }
  }

  function openJump() {
    if (!dialog || dialog.open) { return; }
    input.value = "";
    active = 0;
    dialog.showModal();
    input.focus();
    if (items) { render(); return; }
    fetch(dialog.dataset.jumpSrc, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (j) { items = Array.isArray(j.items) ? j.items : []; render(); })
      .catch(function () { items = []; render(); });
  }

  if (dialog) {
    input.addEventListener("input", function () { active = 0; render(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { active = Math.min(active + 1, shown.length - 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
      else if (e.key === "Enter" && shown[active]) { window.location.assign(shown[active].href); e.preventDefault(); }
    });
    dialog.addEventListener("click", function (e) { if (e.target === dialog) { dialog.close(); } });
    var opener = document.querySelector("[data-jump-open]");
    if (opener) { opener.addEventListener("click", openJump); }
  }

  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); openJump(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || (dialog && dialog.open)) { return; }
    if (e.key === "/") {
      var box = document.querySelector("[data-search-box]");
      if (box) { e.preventDefault(); box.focus(); box.select(); }
      return;
    }
    if (/^[1-9]$/.test(e.key)) {
      var tab = document.querySelector(".subnav a[data-key='" + e.key + "']");
      if (tab) { e.preventDefault(); window.location.assign(tab.href); }
    }
  });
}());
