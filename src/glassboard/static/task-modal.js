// B38: a task opens over the board in one shared dialog, with its own address.
// Each card links to the task page (/s/<session>/t/<n>); without JavaScript that link simply opens
// the full page. With it, the page is fetched, its main content is placed in the dialog, and the
// address bar shows the task URL, so it can be copied, reloaded, opened in a new tab, and closed
// with Back. Same-origin HTML from the board only; scripts and embeds in a response are refused.
(function () {
  "use strict";
  var dialog = document.getElementById("task-dialog");
  if (!dialog || typeof dialog.showModal !== "function" || !window.fetch || !window.history.pushState) { return; }
  var body = dialog.querySelector("[data-task-dialog-body]");
  var opener = null;
  var openerHref = null;
  var loading = null;

  function taskPath(href) {
    var url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin || !/^\/s\/[^/]+\/t\/\d+$/.test(url.pathname)) { return null; }
    return url.pathname;
  }

  function mainOf(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var main = doc.querySelector("main");
    if (!main || main.querySelector("script, base, meta, link, iframe, object, embed")) { return null; }
    var crumb = main.querySelector(".crumb");
    if (crumb) { crumb.remove(); }
    return Array.from(main.childNodes).map(function (node) { return document.importNode(node, true); });
  }

  function show(path, push) {
    if (loading) { loading.abort(); }
    loading = new AbortController();
    return fetch(path, { credentials: "same-origin", headers: { "Accept": "text/html" }, cache: "no-store",
                         signal: loading.signal }).then(function (response) {
      if (!response.ok || new URL(response.url).pathname !== path) { throw new Error("not a task page"); }
      return response.text();
    }).then(function (html) {
      var nodes = mainOf(html);
      if (!nodes) { throw new Error("refused"); }
      body.replaceChildren.apply(body, nodes);
      if (push) { window.history.pushState({ taskModal: path }, "", path); }
      if (!dialog.open) { dialog.showModal(); }
      var heading = body.querySelector("h1");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus();
        dialog.setAttribute("aria-label", heading.textContent.replace(/\s+/g, " ").trim());
      }
    }).catch(function (error) {
      if (error.name !== "AbortError") { window.location.assign(path); }
    });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-task-modal]");
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
    var path = taskPath(link.href);
    if (!path) { return; }
    event.preventDefault();
    opener = link;
    openerHref = link.getAttribute("href");
    show(path, true);
  });

  // The whole kanban card opens its task (B65). Links, buttons and form controls inside it keep their own action, and a
  // click that ends a text selection does nothing. The title link stays the card's one focus stop (and works without JS).
  document.addEventListener("click", function (event) {
    var card = event.target.closest("[data-card-link]");
    if (!card || event.defaultPrevented || event.button !== 0) { return; }
    if (event.target.closest("a, button, input, select, textarea, summary, label, [contenteditable]")) { return; }
    var selection = window.getSelection && window.getSelection();
    if (selection && !selection.isCollapsed && card.contains(selection.anchorNode)) { return; }
    var link = card.querySelector("a[data-card-target]");
    if (!link) { return; }
    if (event.metaKey || event.ctrlKey) { window.open(link.href, "_blank", "noopener"); return; }
    link.click();
  });

  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) { dialog.close(); }        // a click on the backdrop
  });

  dialog.addEventListener("close", function () {
    body.replaceChildren();
    if (window.history.state && window.history.state.taskModal) { window.history.back(); }
    // The live board may have replaced the card while the dialog was open: focus its new link.
    var target = opener && opener.isConnected ? opener :
      (openerHref ? document.querySelector('a[data-task-modal][href="' + openerHref.replace(/"/g, "") + '"]') : null);
    if (target) { target.focus(); }
  });

  window.addEventListener("popstate", function (event) {
    if (event.state && event.state.taskModal) { show(event.state.taskModal, false); }
    else if (dialog.open) { dialog.close(); }
  });
}());
