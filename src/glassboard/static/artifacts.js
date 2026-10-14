(function () {
  "use strict";
  var form = document.getElementById("artifact-upload");
  if (!form) return;
  var status = document.getElementById("artifact-upload-status");
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var input = document.getElementById("artifact-file");
    var file = input.files[0];
    if (!file) return;
    if (file.size > 16 * 1024 * 1024) { status.textContent = "The file exceeds 16 MiB."; return; }
    var q = new URLSearchParams();
    q.set("name", document.getElementById("artifact-name").value || file.name);
    q.set("media_type", file.type || "application/octet-stream");
    var task = document.getElementById("artifact-task").value;
    q.set("purpose", document.getElementById("artifact-purpose").value);
    var description = document.getElementById("artifact-description").value;
    if (task) q.set("task", task);
    if (description) q.set("description", description);
    status.textContent = "Uploading…";
    try {
      var response = await fetch(form.dataset.endpoint + "?" + q.toString(), {
        method: "POST", credentials: "same-origin", body: file,
        headers: {"X-Glassboard-CSRF": form.dataset.csrf, "X-Glassboard-Recovery": form.dataset.recovery,
                  "Idempotency-Key": form.dataset.op, "Content-Type": "application/octet-stream"}
      });
      var result = await response.json();
      if (!response.ok) throw new Error((result.error && result.error.message) || "Upload failed.");
      status.textContent = "Uploaded " + result.artifact + ". Reloading…";
      window.location.reload();
    } catch (error) { status.textContent = error.message || "Upload failed."; }
  });
}());
