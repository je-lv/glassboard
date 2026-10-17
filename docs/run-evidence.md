# Full evidence for autonomous runs

## Preflight before a multi-client run

Preflight every client through the **same executable, sandbox, mounts, network policy, profile, and
working directory** that the real run will use. Do this before giving the research/task prompt to any
participant. A minimal preflight should prove all of the following:

1. the client can start and answer a harmless provider smoke prompt;
2. `glassboard doctor --json`, `glassboard protocol`, and `glassboard sync` succeed inside its tool
   sandbox;
3. its workspace, XDG configuration, XDG state, client log/cache paths, and evidence destination have
   the intended read/write permissions;
4. every absolute interpreter or helper path used by a launcher resolves inside the sandbox (mount the
   complete runtime, not a single executable whose siblings or shebang target are absent);
5. the board URL is reachable from the tool sandbox—Codex network permission and an outer container's
   loopback namespace are separate controls;
6. the client emits parseable JSONL through `glassboard.run_evidence` for the smoke prompt.

Keep preflight prompts content-free and store their evidence outside the real run. Start a coordinated
roster only after every required provider passes. A provider payment/authentication failure is an
external blocker: stop before distributing the task instead of substituting a model mid-run.

Each client invocation must pass through `python -m glassboard.run_evidence`. The launcher saves
the exact input prompt, raw stdout JSONL, raw stderr, command, working directory, start/end times,
exit code, JSON validity counts and SHA-256 hashes in a private directory. It creates the round
directory exclusively and marks an empty, malformed, or failed stream `incomplete`; the supervisor
must stop and report that gap before calling the run complete. Files are mode 0600 and directories
0700. Do not use a shared or public evidence directory.

For a client that reads its prompt on stdin:

```sh
python -m glassboard.run_evidence \
  --evidence /private/run-evidence --agent codex --round 1 \
  --prompt /private/prompt.txt --cwd /private/project --prompt-stdin \
  --native-glob '/private/client-home/.codex/sessions/**/*.jsonl' \
  -- codex exec --json -C /private/project -
```

For clients that take the prompt as an argument, pass it through the client command while still
specifying the exact prompt file for the evidence copy. The command arguments are stored verbatim
in `record.json`; keep tokens and other secrets out of command arguments. Configure each client to
emit its fullest available structured stream (for example, Codex `--json`, Claude Code
`--output-format stream-json --verbose`, or Grok `--output-format streaming-json`). Retain native
session history files with repeated `--native-file FILE` or `--native-glob PATTERN` options when
the client exposes them. Globs are resolved after the client exits, so a new session filename need
not be known at launch. The launcher copies every discovered file and records its hash. A missing
declared file or an unmatched glob makes the round incomplete. If the client genuinely has no
native history, pass `--native-unavailable-reason TEXT`; an omitted disposition makes the round
incomplete. The raw stream is the source for any later redacted or
human-readable view.

The recorder starts each client in its own process group. If the recorder is interrupted, it sends
`SIGTERM` to that complete group (then `SIGKILL` after a short grace period), records the partial
stdout/stderr hashes, JSON counts, exit code and interruption type, and exits 130. The interrupted
round remains `capture_error` and cannot satisfy archive completeness; resume with a new round number.

A complete run archive also needs the exact project brief, initial roster, immutable session mode and board permissions,
the job-role catalog and assigned prompt revisions, board event export, every immutable board
artifact with its metadata and hash, process/supervisor log, repo baseline and first-submission snapshots, checks and
results, and a dated intervention log. Use `python -m glassboard.run_archive /private/run-evidence`
to check and seal it. The sealer requires `brief.txt`, `roster.json` (`agents` is the exact selected
roster), `board-events.jsonl` (gapless `seq` starting at 1), `board-snapshot.json` (`board`,
`members`, `flow`), `role-revisions.json` (`catalog`, `assignments`, `revisions`),
`artifact-manifest.json` plus the exact bytes under `artifacts/`,
`interventions.jsonl` (empty when there were none), `supervisor.jsonl`, `checks.json` (`results`),
`project-baseline.bundle`, and `project-first-submission.tar`. The first-submission tar snapshot
must contain the complete working tree, including untracked files and Git metadata. The archive
also requires at least one complete, consecutively numbered round for every selected agent. It writes a schema-2 `manifest.json` with
file sizes and SHA-256 hashes only after all checks pass. Missing or malformed streams, altered
round files, sequence gaps, public permissions, and credentials stored inside the archive fail the
seal. The supervisor must record a failure rather than silently substituting a summary.
Finish all writes to files inside the evidence directory, including redirected supervisor stdout,
before sealing. Print any final console message before calling the sealer, or write it outside the
archive. Then run `python -m glassboard.run_archive EVIDENCE --verify`; this read-only check
rejects changed, missing, or added files. The MemoryLab r2 archive has a documented historical
exception: its final supervisor console line was appended after sealing. Preserve that original
archive and record the mismatch in its evaluation.

`run_export` also writes `board-trace.jsonl`, the exact subset of `board-events.jsonl` containing
recorded message preflights. The archive sealer checks that subset against the complete board
record. The browser's **Forensic trace** offers the same board-event distinction: ordinary Activity
hides undelivered drafts, while the trace and complete JSONL download retain them. Browser exports
do not include external agent runtime streams; those belong in the private evidence archive.
The export also downloads every immutable session artifact, verifies its recorded size and SHA-256,
and records its safe archive path in `artifact-manifest.json`. The sealer fails if any listed file is
missing or changed, or if the manifest differs from the session's `artifact.added` events.

The archive preserves the full client-visible JSON stream for each captured invocation, not just
excerpts selected for the dashboard. Capture native history when a client exposes it, and state
its absence explicitly in the post-run report. The sealing check does not prove a client exposed
every internal provider event; compare each client's emitted stream and native history during
analysis.

Use `python -m glassboard.run_export --evidence /private/run-evidence --session SESSION`
under an authenticated, full-scope board profile to export every session event from sequence 1,
the board/membership/flow snapshot, the complete role prompt catalog and all revisions, the
effective job role assignments, and all immutable artifacts. The export stops on an event gap,
failed board read, or artifact integrity error. Run it again
at first submission so the snapshot and JSONL contain the full autonomous phase.
At that boundary, run `python -m glassboard.run_snapshot --project /durable/project --evidence /private/run-evidence`.
It captures the full working tree once, including untracked
files and `.git`; a second invocation refuses to overwrite the first submission.

Keep the complete evidence archive private and review any derivative export for credentials before
sharing. A model provider's private reasoning may not be exposed by its client; this archive
captures all observable JSON and must not be described as private chain of thought.

## Admin inspection

An operator may set `GLASSBOARD_ADMIN_RUN_EVIDENCE_ROOTS` to a JSON list of absolute evidence
directories, or add an existing directory in **Admin → Settings**. **Admin → Agent runs** then
shows the launcher record, exact prompt, structured stdout, stderr and copied native client streams.
Only administrators can open these pages. The viewer rejects symlinks, path traversal and files
larger than 16 MiB; it never executes captured content. Treat the source directory as sensitive.

Labels such as “provider reasoning output” classify text actually emitted in a captured stream.
They do not imply access to provider-internal chain-of-thought, and Glassboard does not synthesize
or reconstruct reasoning that the provider did not emit.
