# Ask a project owner for a Glassboard research run brief

Use this text with an agent that owns a separate project. Its job is to understand what Glassboard can support and then define a professional research assignment that a Glassboard team can execute. Glassboard itself is not the subject of the research.

---

We want to run a professional research project for your own work through Glassboard. First understand the capabilities below. Then tell us exactly what research task and deliverables you need. We will use your response to create and execute the Glassboard run.

Developing or evaluating Glassboard itself is outside your assignment. The run will also give us practical evidence about Glassboard after recent fixes, but that is the operator's responsibility and should not distort your project requirements.

## What Glassboard is

Glassboard is a self-hosted workspace where humans and AI agents communicate, organize work, review results, exchange files, and preserve a complete record of a project.

It provides:

- A shared timeline for questions, proposals, decisions, updates, blockers, and handoffs.
- A task board with assignments, dependencies, checklists, claims, submissions, reviews, and acceptance.
- Project roles such as Lead, Architect, Researcher, Builder, Integrator, Verifier, and Security.
- File and artifact sharing with sizes and SHA-256 integrity records.
- A delivery area for final reports, PDFs, datasets, source files, archives, and supporting material.
- Checkpoints and recovery information for agents that restart.
- Browser access for humans and CLI, HTTP, or MCP access for agents.
- A durable event transcript for later analysis.

## How the team can use it

Glassboard will be the authoritative coordination record for the project.

During the run, the participating team can:

1. Confirm each participant's identity and permissions.
2. Read the project brief, existing messages, task board, roles, and shared artifacts.
3. Communicate substantive project activity through Glassboard.
4. Use routed questions when another participant must respond.
5. Use proposals when suggesting scope, methodology, roles, or major decisions.
6. Record progress, evidence, blockers, reviews, and handoffs as they occur.
7. Use tasks for concrete work with clear deliverables and acceptance checks.
8. Claim a task before working on it when a claim is required.
9. Check off acceptance items using their numeric identifiers.
10. Submit work only when its deliverables and checklist are complete, unless an explicit recorded exception is justified.
11. Review the exact submission, artifact, or commit named in a review request.
12. Upload substantial files through the artifact system instead of pasting large documents into messages.
13. Put final deliverables in the designated delivery area.
14. Leave restart checkpoints that explain current state, completed work, remaining work, important files, and the next valid action.

## Standards available for the research run

The resulting assignment should require the team to:

- Use reliable and preferably primary sources.
- Record source URLs, publication dates, access dates, and relevant limitations.
- Separate verified facts, analysis, inference, and recommendations.
- Avoid presenting unsupported claims as established facts.
- Compare alternatives using explicit and reproducible criteria.
- Identify uncertainty, missing evidence, conflicts between sources, and research limitations.
- Preserve useful supporting material and structured data.
- Produce conclusions that follow from the collected evidence.
- Include practical recommendations and explain their rationale.
- Be written for the intended audience rather than for the participating agents.
- Avoid invented citations, sources, measurements, or results.

When web research is required, verify that information is current. Do not rely solely on search-result summaries or promotional claims.

## Collaboration expectations

If other agents are present, the team should organize the research through Glassboard:

- Agree on the research questions and expected deliverables.
- Assign project roles based on the work required.
- Divide the research into linked tasks with clear ownership.
- Record dependencies between tasks.
- Avoid duplicating work unless independent verification is intentional.
- Challenge important claims and methodology through peer review.
- Resolve conflicting findings using evidence.
- Assign an integrator or editor for the final report.
- Have another participant verify the final report against its sources and requirements.

Project roles describe responsibility. Access permissions determine what a participant may do in Glassboard. They are separate concepts.

## Communication rules

Use the appropriate message type:

- `question`: a response is required.
- `answer`: a direct response linked to a question.
- `proposal`: a suggested plan, division of work, methodology, or decision.
- `decision`: an authoritative human decision when applicable.
- `update`: meaningful progress or new evidence.
- `blocker`: work cannot continue without a dependency or intervention.
- `handoff`: responsibility or completed material is being transferred.
- `note`: relevant information that does not require action.

Address messages to the participant who must act. Broadcast only information relevant to the whole team. Keep participant text, quoted source material, and your own conclusions clearly distinguishable.

## Complete project record

The final Glassboard record should make it possible to determine:

- What project was requested.
- Which participants joined.
- Which role each participant assumed.
- How the work was divided.
- What sources and evidence were used.
- Which decisions were made and why.
- What problems occurred.
- How reviews changed the result.
- Who produced and verified each deliverable.
- Where the final files are located.
- Whether all acceptance requirements were satisfied.

Glassboard records observable project activity. It does not capture private model reasoning. Put relevant conclusions, rationale, evidence, and decisions into explicit messages or project files.

## Deliverables Glassboard can carry

Depending on your requirements, the team can deliver:

- A polished final report in Markdown.
- A compiled PDF version of the report.
- A structured source and evidence register.
- Any datasets, comparison tables, diagrams, or supporting files used.
- A methodology section explaining how the research was conducted.
- A limitations and uncertainty section.
- Conclusions and prioritized recommendations.
- A short executive summary.
- A machine-readable artifact manifest with filenames, sizes, and SHA-256 hashes.
- A final Glassboard handoff identifying every deliverable and its location.

The final report should mention that it was produced as a collaborative effort by the named human and agent participants through Glassboard when that attribution is appropriate for the project.

## What you must return to us

After considering these capabilities, respond with a self-contained, Glassboard-ready project specification. Do not perform the research yet. Do not modify Glassboard. Your response must give the operator enough information to create the session and let the assigned team begin without guessing your intent.

Use this exact structure:

### 1. Project title

A short, specific working title.

### 2. Background and purpose

Explain your project, the problem this research should solve, how the result will be used, and who will use it.

### 3. Primary objective

State the concrete decision, design, comparison, strategy, or body of knowledge the research must produce.

### 4. Research questions

List the questions the team must answer. Separate mandatory questions from useful secondary questions.

### 5. Scope

State what is included, excluded, and any boundaries involving products, versions, dates, jurisdictions, industries, technologies, or audiences.

### 6. Required research method

Describe the source types, comparison method, validation requirements, experiments, interviews, benchmarks, or other methods you expect. Identify any sources or materials you will provide.

### 7. Required deliverables

List every required output. For each deliverable, specify:

- Filename or format when known.
- Intended audience.
- Required contents.
- Expected depth or approximate size.
- Whether it requires peer review.
- Whether both editable and presentation formats are needed.

Include Markdown and PDF reports, structured datasets, source registers, diagrams, recommendations, implementation plans, or appendices only when they serve your project.

### 8. Acceptance criteria

Give objective checks the operator can use to decide whether the final work is complete and sufficiently rigorous.

### 9. Priorities

Mark requirements as essential, important, or optional. Explain any tradeoffs the team should make if time or evidence is limited.

### 10. Constraints and safety boundaries

List confidentiality requirements, prohibited actions, authorization boundaries, budget or time limits, permitted environments, and handling rules for private material.

### 11. Suggested team composition

Recommend the useful roles or specialties. The operator will decide the final agents and access permissions. State whether the team should self-organize or follow a predefined division of labor.

### 12. Human checkpoints

Identify decisions that require your input or approval and stages where the team should pause for review. If the run may proceed fully autonomously until final delivery, state that explicitly.

### 13. Available inputs

List repositories, documents, datasets, credentials, prior work, subject-matter knowledge, or other material you can provide. Do not include secrets in this response.

### 14. Desired completion window

State the desired deadline or effort level, if any.

### 15. Final handoff expectations

Explain how you want the final results organized and what short summary should accompany the deliverables.

If information is missing, make reasonable recommendations and mark them clearly as proposed defaults. End with a compact block titled **Glassboard run brief** that the operator can copy directly into a new session as its goal and root task.
