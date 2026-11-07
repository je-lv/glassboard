// B58: the Chat tab. Keeps the newest message in view, sets the reply target from a message's Reply or
// Ask link, completes handles in To, and sends with Enter (Shift+Enter adds a line). Without this script
// the same links reload the page with the reply target filled in by the server.
(function () {
  "use strict";

  var stream = document.getElementById("chat-stream");
  if (!stream) { return; }
  var form = document.querySelector("[data-chat-composer]");
  var jump = document.querySelector("[data-chat-jump]");
  var pinned = true;
  // The browser would put a reload back where the reader was; Chat opens at the newest message instead.
  if ("scrollRestoration" in window.history) { window.history.scrollRestoration = "manual"; }

  // B66: on wide screens the page is a fixed frame and only the message list scrolls; on narrow screens the list
  // has its own scroll box too. Either way the page itself never jumps: we scroll the list, not the window.
  function atBottom() {
    return stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 48;
  }

  function toBottom() {
    stream.scrollTop = stream.scrollHeight;
    if (jump) { jump.hidden = true; }
  }

  // B67: the channel list (unread counts, modes) comes inside each stream refresh; move it into the side panel.
  var channelsBox = document.querySelector("[data-chat-channels-box]");
  function syncChannels() {
    var live = stream.querySelector("template[data-chat-channels-live]");
    if (!live || !channelsBox) { return; }
    var openArchived = channelsBox.querySelector("details[data-persist][open]");
    channelsBox.replaceChildren(live.content.cloneNode(true));
    if (openArchived) {
      var d = channelsBox.querySelector("details[data-persist]");
      if (d) { d.open = true; }
    }
  }

  // On load: the newest message, unless the address points at a message.
  var hash = window.location.hash;
  if (!hash || hash === "#chat-end") { toBottom(); }
  else if (hash === "#composer" && form) {
    toBottom();
    var body = form.querySelector("[data-chat-body]");
    if (body) { body.focus({ preventScroll: true }); }
  } else {
    var target = /^#m\d+$/.test(hash) ? document.getElementById(hash.slice(1)) : null;
    if (target) { stream.scrollTop = target.offsetTop - 16; }
  }

  stream.addEventListener("scroll", function () {
    if (atBottom() && jump) { jump.hidden = true; }
  }, { passive: true });

  // The newest message's number: "New messages below" shows only when a refresh really brought one.
  function lastSeq() {
    var all = stream.querySelectorAll(".chat-msg[data-seq]");
    return all.length ? Number(all[all.length - 1].dataset.seq) : 0;
  }
  var seenSeq = 0;
  stream.addEventListener("glassboard:before-update", function () { pinned = atBottom(); seenSeq = lastSeq(); });
  stream.addEventListener("glassboard:updated", function () {
    syncChannels();
    if (pinned) { toBottom(); }
    else if (jump && lastSeq() > seenSeq) { jump.hidden = false; }
  });
  if (jump) { jump.addEventListener("click", toBottom); }

  if (!form) { return; }
  var re = form.querySelector("[data-chat-re]");
  var chip = form.querySelector("[data-chat-chip]");
  var chipLabel = form.querySelector("[data-chat-chip-label]");
  var kind = form.querySelector("[data-chat-kind]");
  var to = form.querySelector("[data-chat-to]");
  var bodyBox = form.querySelector("[data-chat-body]");
  var list = form.querySelector("[data-chat-members]");
  var me = (document.querySelector(".who-name") || { textContent: "" }).textContent.replace(/^@/, "");
  var handles = list ? Array.from(list.options).map(function (o) { return o.value; }) : [];

  function setKind(value) {
    if (!kind) { return; }
    Array.from(kind.options).forEach(function (o) { o.selected = o.value === value; });
  }

  function setTarget(seq, actor, type, ask) {
    re.value = String(seq);
    chipLabel.textContent = "@" + actor + " #" + seq;
    chip.hidden = false;
    if (to && actor !== me) { to.value = actor; }
    setKind(ask ? "question" : (type === "question" ? "answer" : "note"));
    bodyBox.focus();
  }

  stream.addEventListener("click", function (e) {
    var link = e.target.closest("a[data-chat-reply], a[data-chat-ask]");
    if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    var msg = link.closest(".chat-msg");
    if (!msg || !/^\d+$/.test(msg.dataset.seq || "")) { return; }
    e.preventDefault();
    setTarget(msg.dataset.seq, msg.dataset.actor || "", msg.dataset.type || "", link.hasAttribute("data-chat-ask"));
  });

  // The @ beside a member in the side panel writes to that member.
  document.addEventListener("click", function (e) {
    var link = e.target.closest("a[data-chat-to-member]");
    if (!link || !to || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    e.preventDefault();
    to.value = link.getAttribute("data-chat-to-member");
    bodyBox.focus({ preventScroll: true });
  });

  var clear = form.querySelector("[data-chat-clear]");
  if (clear) {
    clear.addEventListener("click", function (e) {
      e.preventDefault();
      re.value = "";
      chipLabel.textContent = "";
      chip.hidden = true;
      bodyBox.focus();
    });
  }

  // To: complete the handle after the last comma; members already named are not offered again.
  if (to && list) {
    to.addEventListener("input", function () {
      var parts = to.value.split(",");
      var done = parts.slice(0, -1).map(function (p) { return p.trim().replace(/^@/, ""); }).filter(Boolean);
      var prefix = done.length ? done.join(", ") + ", " : "";
      list.replaceChildren.apply(list, handles.filter(function (h) { return done.indexOf(h) === -1; }).map(function (h) {
        var o = document.createElement("option");
        o.value = prefix + h;
        o.textContent = "@" + h;
        return o;
      }));
    });
  }

  var sending = false;
  window.addEventListener("pageshow", function () { sending = false; });
  form.addEventListener("submit", function (e) {
    if (sending) { e.preventDefault(); return; }
    sending = true;
  });

  bodyBox.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey || e.isComposing) { return; }
    e.preventDefault();
    if (!bodyBox.value.trim()) { return; }
    var send = form.querySelector("[data-chat-send]");
    if (form.reportValidity()) { form.requestSubmit(send); }
  });
}());
