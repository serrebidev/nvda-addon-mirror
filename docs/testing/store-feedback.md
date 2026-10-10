# Add-on Store customization and history acceptance

Test the helper built from this branch with NVDA. The manifest remains a development version; this PR does not publish a release.

## Settings and list behavior

- Save settings using Enter, close the Store, and reopen Settings. Cancel must discard drafts.
- Configure tab order, default channel and sorting, then reopen the Store. Verify enabled browsing memory takes precedence over defaults.
- Add and remove installed or available add-ons from Favourites. Use Ctrl+1 through Ctrl+5 to select tabs in their configured order.
- Enable sorting only on Enter. Arrow keys stage a choice; Enter applies it. Check contextual column announcements and both sorting directions.
- Confirm missing values sort last in both directions. Last updated uses the source's catalog date, with its meaning explained in details; unavailable first-publication dates are not presented as facts.
- Press Ctrl+C in the add-on list to copy the selected add-on's share information. Text controls retain normal copy behavior.

## Changelog sources and network use

With All available sources selected, opening Changelog must make no GitHub request. The picker shows supported GitHub sources without counts; locally available catalog/manifest notes appear only when present. Cancel makes no request. Enter loads only the chosen source. Empty or unavailable remote sources report that after selection.

Releases and commits load one page per action. Load older history explicitly and revisit cached pages without another request. Repository changelog files are discovered generically from one bounded tree listing, then read one file per action. Read next changelog file advances through files; it does not imply chronological ordering. No add-on-specific filename exceptions are used.

GitHub rate limits pause requests across repositories and sources until the indicated cooldown expires. Usable cached and local notes remain available. Check truthful partial-history status and ordinary keyboard, speech and braille navigation.

Close the Store during a request, or choose another add-on before completion. Obsolete viewers must not appear. Escape and Cancel should restore logical focus.

## Verification boundaries

The development candidate passed 477 warning-as-error tests, style/diff checks, independent code review and archive/source identity checks. Automated tests cover request budgets, pagination, cache reuse, cooldowns, settings lifecycle and native-shaped host integration. User feedback reported the candidate working well. Minimum/current NVDA GUI compatibility, braille and long-session behavior still require explicit acceptance; automated tests do not prove these.
