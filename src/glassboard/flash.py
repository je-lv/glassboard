"""Action feedback for the dashboard (B34, owner fix F1): every form action ends in a toast.

A dashboard POST that succeeds redirects with `?ok=<code>`; one that fails redirects back to the
page it came from with `?err=<code>`. Codes map to the fixed messages below. Text from the URL is
never shown, unknown success codes show nothing, unknown error codes show the default message, and
no secret or client-supplied text ever appears in a toast (R6, E1)."""
from __future__ import annotations

import contextvars
import re
import urllib.parse

OK = {
    "done": "Done.",
    "dismissed": "Dismissed.",
    "password_changed": "Password changed. Sign in again with the new password.",
    "settings_saved": "Settings saved.",
    "identity_created": "Identity created.",
    "admin_granted": "Admin access granted.",
    "admin_revoked": "Admin access removed.",
    "deactivated": "Identity deactivated. Its tokens and sign-ins no longer work.",
    "sessions_revoked": "Browser sign-ins ended.",
    "password_set": "Password set.",
    "token_revoked": "Token revoked. It no longer works.",
    "backup_created": "Encrypted backup created.",
    "workspace_archived": "Workspace archived.",
    "workspace_purged": "Workspace purged.",
    "role_saved": "Job role saved.",
    "joined": "You joined the session.",
    "approval_granted": "Approval granted.",
    "approval_denied": "Approval denied.",
    "approval_recorded": "Your decision was recorded.",
    "hold_upheld": "Hold upheld.",
    "hold_proceed": "Hold overruled: the work may proceed.",
    "standdown_requested": "Stand-down requested. The agent releases its work and answers.",
    "standdown_lifted": "Stand-down lifted. The agent may claim work again.",
    "quiet_saved": "Quiet hours saved.",
    "channel_created": "Channel created.",
    "channel_mode": "Channel setting saved.",
    "artifact_uploaded": "File uploaded.",
    "role_assigned": "Job role updated.",
    "session_created": "Session created.",
    "members_updated": "Team updated.",
    "agent_created": "Agent created.",
    "task_created": "Task created.",
    "tasks_created": "Tasks created from the proposal.",
    "session_state": "Session state changed.",
    "posted": "Posted.",
    "task_acknowledged": "Acknowledged.",
    "task_reviewed": "Review recorded.",
    "task_accepted": "Task accepted.",
    "task_returned": "Task returned to its claimant.",
    "task_reassigned": "Task reassigned.",
    "task_cancelled": "Task cancelled.",
    "task_reopened": "Task reopened.",
    "task_released": "Claim released.",
    "task_edited": "Task saved.",
}

DEFAULT_ERR = "That did not work."
ERR = {
    "invalid": "That was not valid (kind, length or fields).",
    "invalid_request": "That was not valid; check the fields.",
    "invalid_preflight": "This message no longer matches its recorded draft check. Check it again.",
    "recipient_not_member": "Some recipients are not members of this session.",
    "forbidden": "You may not do that here (for example, only human Session Managers post decisions or manage "
                 "tasks in the browser).",
    "not_found": "Something you referred to does not exist.",
    "rate_limited": "Too many requests; wait a moment.",
    "payload_too_large": "That is larger than the board accepts.",
    "idempotency_key_reused": "This form was already used for something else; reload the page.",
    "invalid_transition": "The task or session cannot make that change from its current state.",
    "version_conflict": "The task changed since this page was loaded; reload and try again.",
    "submission_mismatch": "The task has a different current submission; reload before reviewing it.",
    "checklist_incomplete": "Complete the checklist before submitting, or record an explicit incomplete-checklist "
                            "reason.",
    "session_paused": "The session is paused.",
    "session_archived": "The session is archived.",
    "secret_in_content": "That text contains a token or fence; remove it.",
    "field_not_patchable": "Ownership and state cannot be edited; use reassign, cancel or reopen.",
    "task_not_claimable": "That task cannot be claimed now.",
    "dependency_cycle": "Those dependencies or that parent would form a cycle.",
    "task_waiting": "That task depends on tasks that are not done yet.",
    "already_resolved": "That was already decided.",
    "already_requested": "That agent already has a stand-down that nobody lifted.",
    "channel_exists": "A channel with that name already exists.",
    "channel_archived": "That channel is archived.",
    "recipient_not_in_channel": "Inside a channel, To names channel members only.",
    "not_a_channel_member": "You are not a member of that channel.",
    "handle_taken": "That handle already exists or is reserved; pick another.",
    "resource_held": "Someone already holds a lease on that resource.",
    "archive_failed": "The workspace archive could not be created or verified.",
    "purge_failed": "The workspace was not purged. Its state or verified archive may have changed.",
    "confirmation_required": "Type the exact requested value and select the confirmation box.",
    "wrong_password": "Your current password was not accepted.",
    "invalid_password": "Use a password of 16 to 1024 characters.",
    "last_admin": "At least one active admin must remain.",
    "self_access": "You cannot remove your own admin access here.",
    "inactive": "That identity is inactive.",
    "read_only": "This sign-in is read-only; changes are disabled.",
    "unauthenticated": "Your sign-in has ended; sign in again.",
    "backup_failed": "The backup could not be created. Nothing was written.",
    "unavailable": "The board could not record the change. Check the result before retrying.",
    "unsupported_media_type": "That file type is not accepted.",
    "ambiguous": "That matches more than one thing; be more specific.",
}

# Success code per dashboard POST route (route path as registered).
ROUTE_OK = {
    "/attention/{s}/{seq}/dismiss": "dismissed",
    "/admin/password": "password_changed",
    "/admin/settings/boards": "settings_saved",
    "/admin/settings/run-evidence": "settings_saved",
    "/admin/access/create": "identity_created",
    "/admin/access/change": "done",            # the handler names the exact action
    "/admin/access/password": "password_set",
    "/admin/credentials/revoke": "token_revoked",
    "/admin/backups/create": "backup_created",
    "/admin/workspaces/archive": "workspace_archived",
    "/admin/workspaces/purge": "workspace_purged",
    "/owner/roles/{name}": "role_saved",
    "/owner/roles": "role_saved",
    "/owner/s/{s}/join": "joined",
    "/s/{s}/approvals/{seq}": "approval_recorded",   # the handler names grant or deny
    "/s/{s}/holds/{seq}": "approval_recorded",       # the handler names uphold or proceed
    "/s/{s}/standdowns": "standdown_requested",
    "/s/{s}/standdowns/{seq}/lift": "standdown_lifted",
    "/s/{s}/quiet-hours": "quiet_saved",
    "/s/{s}/channels": "channel_created",
    "/s/{s}/channels/{name}/mode": "channel_mode",
    "/s/{s}/artifacts/upload": "artifact_uploaded",
    "/s/{s}/job-role": "role_assigned",
    "/new-session": "session_created",
    "/s/{s}/members": "members_updated",
    "/agents": "agent_created",                # the handler names a revoke
    "/s/{s}/t/{n}/{op}": "done",               # task_<op>, below
    "/s/{s}/tasks": "task_created",
    "/s/{s}/tasks-from-drafts": "tasks_created",
    "/s/{s}/state": "session_state",
    "/s/{s}/post": "posted",
}
TASK_OP_OK = {"acknowledge": "task_acknowledged", "review": "task_reviewed", "accept": "task_accepted",
              "return": "task_returned", "reassign": "task_reassigned", "cancel": "task_cancelled",
              "reopen": "task_reopened", "release": "task_released", "edit": "task_edited"}
# Sign-in pages speak for themselves; these routes never get a toast.
NO_TOAST = {"/login", "/admin/login", "/logout"}
# These routes answer with their own result page (a one-time token, an invite, a draft check).
RESULT_PAGE = {"/admin/credentials/create", "/agents/token", "/s/{s}/invite", "/s/{s}/preflight"}

_CODE = re.compile(r"^[a-z_]{1,40}$")
CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gb_flash", default=None)


def err_text(code: str | None) -> str:
    return ERR.get(code or "", DEFAULT_ERR)


def _strip(query: str) -> str:
    return urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
                                   if k not in ("ok", "err")])


def for_page(path: str, query: str) -> dict | None:
    """The toast for a GET page, from its own query string, or None."""
    params = urllib.parse.parse_qs(query)
    ok = (params.get("ok") or [""])[0]
    err = (params.get("err") or [""])[0]
    ok_text = OK.get(ok) if _CODE.match(ok) else None
    err_msg = err_text(err) if err else None
    if not ok_text and not err_msg:
        return None
    rest = _strip(query)
    return {"ok": ok_text, "err": err_msg, "clean": path + ("?" + rest if rest else "")}


def ok_code(route: str, path_params: dict, chosen: str | None) -> str | None:
    if route in NO_TOAST or route in RESULT_PAGE:
        return None
    if chosen in OK:
        return chosen
    if route == "/s/{s}/t/{n}/{op}":
        return TASK_OP_OK.get(str(path_params.get("op")), "done")
    return ROUTE_OK.get(route, "done")


def with_code(location: str, key: str, code: str) -> str:
    """Add `key=code` to a same-site redirect target, before any fragment, unless it already has a
    toast code."""
    base, hash_, frag = location.partition("#")
    parts = urllib.parse.urlsplit(base)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(k in ("ok", "err") for k, _ in pairs):
        return location
    pairs.append((key, code))
    rebuilt = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(pairs)))
    return rebuilt + (hash_ + frag if hash_ else "")


def back_with_error(referer: str, host: str, code: str) -> str | None:
    """Where a failed form should return: the same-origin page it was posted from, with `err=<code>`."""
    if not referer:
        return None
    ref = urllib.parse.urlsplit(referer)
    if ref.netloc != host or not ref.path.startswith("/") or ref.path.startswith(("/api/", "/mcp", "//")):
        return None
    code = code if _CODE.match(code or "") else "invalid_request"
    rest = _strip(ref.query)
    return with_code(ref.path + ("?" + rest if rest else ""), "err", code)
