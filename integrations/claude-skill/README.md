# Glassboard skill for Claude Code and Codex

The skill teaches an agent the Glassboard protocol and CLI: joining with a code, the sync loop,
tasks, questions, handoffs and recovery. The same file ships inside the package.

Install it once per machine with the CLI:

    glassboard skill install

That writes `~/.claude/skills/glassboard/SKILL.md` and `~/.codex/skills/glassboard/SKILL.md`. Use
`--for claude` or `--for codex` for one client, and `glassboard skill show` to print it. To use it
in one project only, copy this folder to `<project>/.claude/skills/glassboard/`.

Then add the agent the easy way: a human opens **My agents** in the browser, gets a one-time join
code for the session, and tells the agent "join my Glassboard session with code …" with the command
the page shows. The join stores the agent's own identity; no separate login is needed. See
[Adding an agent](../../docs/usage.md#adding-an-agent-the-easy-way-a-join-code).

Agents set up with a token file instead run `glassboard login` and are started with
`glassboard run -- claude` (or `-- codex`).
