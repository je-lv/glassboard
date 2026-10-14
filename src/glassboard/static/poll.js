// Progressive enhancement for live server fragments and query/cursor navigation.
// The server remains authoritative: this script only installs same-origin, escaped HTML fragments.
(function () {
  "use strict";

  var controllers = new Map();
  var status = document.createElement("span");
  status.className = "fragment-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  document.body.appendChild(status);

  function publicKey(url) {
    return url.pathname + url.search + url.hash;
  }

  function samePublicLocation(url) {
    return publicKey(url) === publicKey(new URL(window.location.href));
  }

  function expectedKind(box) {
    return box.id === "timeline" ? "timeline" :
      (box.id === "observe-live" ? "observe" : "poll");
  }

  function preserveDetails(box) {
    return Array.from(box.querySelectorAll("details[data-persist][open]"))
      .map(function (detail) { return detail.dataset.persist; });
  }

  function restoreDetails(box, open) {
    box.querySelectorAll("details[data-persist]").forEach(function (detail) {
      detail.open = open.indexOf(detail.dataset.persist) !== -1;
    });
  }

  function safeFragment(html) {
    var parsed = new DOMParser().parseFromString(html, "text/html");
    if (parsed.querySelector("script, base, meta, link, iframe, object, embed")) { return null; }
    return Array.from(parsed.body.childNodes).map(function (node) {
      return document.importNode(node, true);
    });
  }

  function announce(message) {
    status.textContent = "";
    window.setTimeout(function () { status.textContent = message; }, 0);
  }

  function controller(box) {
    if (controllers.has(box)) { return controllers.get(box); }
    var state = { abort: null, serial: 0, loading: false };
    controllers.set(box, state);
    return state;
  }

  function request(box, fragmentUrl, options) {
    options = options || {};
    var state = controller(box);
    var expected = new URL(fragmentUrl, window.location.href);
    if (expected.origin !== window.location.origin) { return Promise.resolve(false); }
    if (state.abort) { state.abort.abort(); }
    state.abort = new AbortController();
    state.serial += 1;
    var serial = state.serial;
    state.loading = true;
    if (!options.quiet) {
      box.setAttribute("aria-busy", "true");
      box.classList.add("fragment-loading");
    }

    return fetch(expected.href, {
      credentials: "same-origin",
      headers: { "Accept": "text/html", "X-Glassboard-Fragment": expectedKind(box) },
      cache: "no-store",
      redirect: "follow",
      signal: state.abort.signal
    }).then(function (response) {
      var finalUrl = new URL(response.url);
      var contentType = response.headers.get("Content-Type") || "";
      var kind = response.headers.get("X-Glassboard-Fragment");
      if (!response.ok || finalUrl.origin !== window.location.origin ||
          finalUrl.pathname !== expected.pathname ||
          !contentType.toLowerCase().startsWith("text/html") || kind !== expectedKind(box)) {
        throw new Error("fragment response rejected");
      }
      return response.text();
    }).then(function (html) {
      if (serial !== state.serial || (options.quiet && html === box._lastServerHtml)) { return false; }
      var nodes = safeFragment(html);
      if (!nodes) { throw new Error("unsafe fragment response"); }
      var open = preserveDetails(box);
      box.replaceChildren.apply(box, nodes);
      box._lastServerHtml = html;
      restoreDetails(box, open);
      if (options.publicUrl) {
        var publicUrl = new URL(options.publicUrl, window.location.href);
        if (options.history === "replace") {
          window.history.replaceState({ glassboardFragment: expectedKind(box) }, "", publicKey(publicUrl));
        } else if (!samePublicLocation(publicUrl)) {
          window.history.pushState({ glassboardFragment: expectedKind(box) }, "", publicKey(publicUrl));
        }
      }
      if (typeof options.after === "function") { options.after(); }
      if (!options.quiet) { announce(options.announcement || "View updated."); }
      return true;
    }).catch(function (error) {
      if (error.name === "AbortError") { return false; }
      if (options.fallback) { window.location.assign(options.fallback); }
      return false;
    }).finally(function () {
      if (serial === state.serial) {
        state.loading = false;
        box.removeAttribute("aria-busy");
        box.classList.remove("fragment-loading");
      }
    });
  }

  function timelineUrls(publicUrl) {
    var box = document.getElementById("timeline");
    if (!box || !box.dataset.fragment || !box.dataset.public) { return null; }
    var target = new URL(publicUrl, window.location.href);
    var publicBase = new URL(box.dataset.public, window.location.href);
    if (target.origin !== window.location.origin || target.pathname !== publicBase.pathname) { return null; }
    var allowed = new URLSearchParams();
    ["f", "t", "before", "order"].forEach(function (name) {
      if (target.searchParams.has(name)) { allowed.set(name, target.searchParams.get(name)); }
    });
    var fragment = new URL(box.dataset.fragment, window.location.href);
    fragment.search = allowed.toString();
    target.search = allowed.toString();
    return { box: box, fragment: fragment, publicUrl: target };
  }

  function syncTimelineControls(url) {
    var target = new URL(url, window.location.href);
    var filter = target.searchParams.get("f") || "all";
    var order = target.searchParams.get("order") || "oldest";
    document.querySelectorAll("[data-timeline-filter]").forEach(function (link) {
      var active = link.dataset.timelineFilter === filter;
      link.classList.toggle("on", active);
      if (active) { link.setAttribute("aria-current", "page"); }
      else { link.removeAttribute("aria-current"); }
      var href = new URL(link.href);
      href.searchParams.set("order", order);
      link.href = publicKey(href);
    });
    var form = document.querySelector("form[data-timeline-task]");
    if (form) {
      var orderInput = form.querySelector("input[name='order']");
      var taskInput = form.querySelector("input[name='t']");
      if (orderInput) { orderInput.value = order; }
      if (taskInput) { taskInput.value = target.searchParams.get("t") || ""; }
    }
  }

  function navigateTimeline(publicUrl, historyMode, keyboard) {
    var urls = timelineUrls(publicUrl);
    if (!urls) { return false; }
    request(urls.box, urls.fragment, {
      publicUrl: urls.publicUrl,
      history: historyMode,
      fallback: urls.publicUrl.href,
      announcement: "Activity updated.",
      after: function () {
        urls.box.dataset.src = urls.fragment.pathname + urls.fragment.search;
        syncTimelineControls(urls.publicUrl);
        if (keyboard) {
          var current = urls.box.querySelector(".timeline-order a[aria-current='true']");
          if (current) { current.focus(); }
        }
      }
    });
    return true;
  }

  function observeUrls(publicUrl) {
    var box = document.getElementById("observe-live");
    if (!box || !box.dataset.fragment || !box.dataset.public) { return null; }
    var target = new URL(publicUrl, window.location.href);
    var publicBase = new URL(box.dataset.public, window.location.href);
    if (target.origin !== window.location.origin || target.pathname !== publicBase.pathname) { return null; }
    var fragment = new URL(box.dataset.fragment, window.location.href);
    if (target.searchParams.has("at")) { fragment.searchParams.set("at", target.searchParams.get("at")); }
    target.search = fragment.search;
    return { box: box, fragment: fragment, publicUrl: target, live: !fragment.searchParams.has("at") };
  }

  function navigateObserve(publicUrl, historyMode, keyboard) {
    var urls = observeUrls(publicUrl);
    if (!urls) { return false; }
    if (samePublicLocation(urls.publicUrl) && historyMode !== "replace") { return true; }
    request(urls.box, urls.fragment, {
      publicUrl: urls.publicUrl,
      history: historyMode,
      fallback: urls.publicUrl.href,
      announcement: urls.live ? "Following the live run." : "Replay event updated.",
      after: function () {
        if (urls.live) { urls.box.dataset.src = urls.fragment.pathname; }
        else { delete urls.box.dataset.src; }
        if (keyboard) {
          var wanted = publicKey(urls.publicUrl);
          var match = Array.from(urls.box.querySelectorAll("a[href]")).find(function (link) {
            return publicKey(new URL(link.getAttribute("href"), window.location.href)) === wanted;
          });
          if (match) { match.focus(); }
        }
      }
    });
    return true;
  }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey) { return; }
    var link = event.target.closest("a[href]");
    if (!link || link.getAttribute("target") || link.hasAttribute("download")) { return; }
    var target = new URL(link.getAttribute("href"), window.location.href);
    var handled = navigateTimeline(target, "push", event.detail === 0) ||
                  navigateObserve(target, "push", event.detail === 0);
    if (handled) { event.preventDefault(); }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("form[data-timeline-task]") || form.method.toLowerCase() !== "get") { return; }
    var target = new URL(form.action, window.location.href);
    var values = new FormData(form);
    values.forEach(function (value, key) { if (String(value)) { target.searchParams.set(key, String(value)); } });
    if (navigateTimeline(target, "push", true)) { event.preventDefault(); }
  });

  window.addEventListener("popstate", function () {
    if (navigateTimeline(window.location.href, "replace", false)) { return; }
    navigateObserve(window.location.href, "replace", false);
  });

  function refresh() {
    if (document.visibilityState !== "visible") { return; }
    document.querySelectorAll("#timeline[data-src], #run-status[data-src], #run-strip[data-src], #observe-live[data-src]")
      .forEach(function (box) {
        var state = controller(box);
        if (state.loading) { return; }
        request(box, box.dataset.src, { quiet: true });
      });
  }

  window.setInterval(refresh, 5000);
}());
