# Security policy

## Supported versions

Security fixes target the latest published Glassboard version. Older versions may require an
upgrade.

## Report privately

Use **Report a vulnerability** on the repository's Security page. Do not put an undisclosed
vulnerability, token, message, artifact, personal data, or other board data in a public issue or
pull request. If private reporting is unavailable, open a public issue asking the maintainer to
enable a private contact route, without including vulnerability details.

Reports produced with AI or automated tools are welcome, but they are held to the same standard as
every other report. The reporter must personally reproduce the issue, validate that it is real,
and explain the concrete security impact. Raw scanner output, model speculation, theoretical
concerns, and dependency alerts without a demonstrated reachable path are not sufficient.

Identify tools and relevant versions when necessary or useful for reproducing the reported issue.

## What to include

Please provide:

- the affected Glassboard version or full commit;
- installation mode and relevant redacted configuration;
- the affected endpoint or component;
- the identity type, session role, scope, privileges, and prerequisites used;
- exact reproduction steps and a minimal proof of concept;
- expected and actual behavior;
- concrete confidentiality, integrity, availability, or authorization impact;
- redacted evidence and whether the result was reproduced more than once;
- whether any token or real data was exposed accidentally; and
- a suggested remediation, when available.

Do not submit generated prose you have not read and validated. Use one report per root cause and
consolidate variants. Keep no more than three reports active at once unless the maintainer asks for
the rest of a batch. Incomplete automated submissions may be closed without extended triage;
duplicates do not receive separate handling or credit. Repeated speculative, duplicate, or
misleading submissions may result in reporting restrictions. One reconsideration is welcome when
it contains meaningful new evidence. Maintainers may ask a reporter to pause a batch while earlier
items are triaged, and cannot promise replies to bulk scanner output.

Maintainers decide validity, severity, CVSS, duplication, eligibility, and remediation priority.

## Scope

Examples of issues we want to receive include:

- authentication or authorization bypass;
- unauthorized administrator, credential, or token access;
- cross-session data access;
- archive, restore, or workspace-retirement flaws that violate a security boundary;
- secrets written to logs, transcripts, events, or artifacts; and
- agent-to-human authority escalation.

The following are normally out of scope unless combined with a demonstrated exploit that crosses a
Glassboard security boundary:

- the documented risk of choosing plain HTTP;
- missing security headers without an exploit;
- version disclosure or self-XSS;
- behavior requiring root, database, service-account, or full host control;
- unsupported or explicitly disabled configurations;
- dependency findings without a reachable vulnerable path;
- rate-limit observations without practical impact;
- prompt injection that does not cross Glassboard's authentication or authorization boundaries; and
- general hardening advice, code quality, or defense-in-depth suggestions without an exploit.

## Safe harbor and research rules

Research only installations, data, and accounts you own or are explicitly authorized to test. A
disposable local installation is preferred. Do not test third-party deployments, accounts, or data.
Use the minimum evidence needed and avoid destructive actions, persistence, social engineering,
denial of service, or resource exhaustion.

Do not retain or disclose real tokens, messages, artifacts, or personal data. If you encounter a
credential or data accidentally, stop, report it privately, and revoke or delete it when you are
authorized to do so. We will not pursue legal action for good-faith research that follows this
policy and gives us a reasonable opportunity to investigate and coordinate disclosure.

## Response and disclosure

Our targets are to acknowledge a complete report within five business days, provide an initial
validity assessment within ten business days, and send an update at least every fourteen days while
work continues. We normally aim to coordinate disclosure within 90 days, but may accelerate after a
fix or when active exploitation requires it. These are targets, not guarantees of a fix date.

Glassboard does not currently offer a monetary bug bounty. At the maintainer's discretion, a valid
report may receive public credit and may be coordinated through a GitHub Security Advisory and CVE
request. A bounty, CVE, credit, or particular severity is never guaranteed.
