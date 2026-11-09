# Issue tracker: Forgejo

Issues and specs for this repo live as Forgejo issues at
`git.serrebiradio.com/serrebi/nvda-addon-mirror`. Use the `tea` CLI
(`tea login add -n serrebi --url https://git.serrebiradio.com`) or the
Forgejo REST API (`https://git.serrebiradio.com/api/v1`) for all operations.

## Conventions

- **Create an issue**: `tea issue create --title "..." --body "..." --repo serrebi/nvda-addon-mirror`. Use a heredoc for multi-line bodies.
- **Read an issue**: `tea issue view <number> --repo serrebi/nvda-addon-mirror`, or `GET /repos/serrebi/nvda-addon-mirror/issues/<number>`.
- **List issues**: `tea issue list --state open --repo serrebi/nvda-addon-mirror` with appropriate `--label` filters, or `GET /repos/serrebi/nvda-addon-mirror/issues?state=open`.
- **Link issues**: Forgejo has no sub-issues or native dependencies. Put `Part of #<parent>` at the top of the child body, and a `Blocked by: #<n>, #<n>` line at the top when blocked.
- **Comment on an issue**: `tea issue comment <number> --body "..." --repo serrebi/nvda-addon-mirror`.
- **Apply / remove labels**: `tea label` management plus issue edit, or the API (`POST /repos/serrebi/nvda-addon-mirror/issues/<number>/labels`).
- **Close**: `tea issue close <number> --repo serrebi/nvda-addon-mirror --comment "..."`.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `tea pr` equivalents:

- **Read a PR**: `tea pr view <number>` for the discussion and diff.
- **List external PRs for triage**: `tea pr list --state open`, filtering out the owner's own.
- **Comment / label / close**: `tea pr comment`, issue-label API, `tea pr close`.

Forgejo shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `tea pr view 42` and fall back to `tea issue view 42`.

## When a skill says "publish to the issue tracker"

Create a Forgejo issue.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `tea issue create --label wayfinder:map`.
- **Child ticket**: an issue with `Part of #<map>` at the top of its body (Forgejo has no sub-issues). Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: a `Blocked by: #<n>, #<n>` line at the top of the child body (Forgejo has no native issue dependencies). A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (search bodies for `Part of #<map>`), drop any with an open blocker (an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: assign the ticket to yourself, the session's first write.
- **Resolve**: comment the answer on the ticket, then close it, then append a context pointer to the map's Decisions-so-far.
