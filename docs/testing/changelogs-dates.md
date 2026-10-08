# Changelogs and last updated: manual test guide

Install the candidate add-on, restart NVDA, then open the SerrebiRadio Add-on
Store. These checks use only the Store's normal loaded catalog; they must not
download any `.nvda-addon` files.

1. Select a GitHub-hosted add-on and open its context menu with Applications
   or Shift+F10. Choose **Changelog**. A dialog opens after the release-notes
   lookup. Its version choice includes the current catalog notes first, then
   known GitHub releases. Changing the choice changes a read-only multiline
   notes control.
2. Repeat with an add-on that has no GitHub repository page. The dialog says
   that notes are unavailable, or offers the author page. It must not show
   invented older versions.
3. Disconnect from the network, reopen the dialog for a GitHub add-on, and
   confirm it reports the cached notes if available. For a fresh lookup it
   reports a network failure while retaining current catalog notes. Rate-limit
   failures are identified separately.
4. In Sort by column choose **Last updated (ascending)**, then **Last updated (descending)**.
   Entries with a known source
   release time change order. Entries without one remain at the end in both
   directions. Check that the Source column still displays the source for the
   same rows and that its header remains informational.
5. Close the Store and exit NVDA. Restart, reopen the Store, and check that it
   still opens normally. The release-note cache is intentionally in memory
   only, so it may query GitHub again after restart.
6. Select an externally installed add-on whose manifest contains a changelog.
   Expected: its native manifest notes are shown even without catalog history.
7. Start a fresh changelog lookup and close the originating Store before it
   finishes. Wait at least 12 seconds. Expected: no late modal opens. Reopen
   and request another add-on's changelog; it must show that add-on's notes.
8. Keep a source subset selected, clear the search and apply both Last updated
   directions. Expected: sorting never brings excluded sources back. With
   browsing memory enabled, reopen and verify both native and date sorts retain
   their own selected direction.

**Verified by automated tests:** GitHub repository URL rejection, unknown-date
ordering, catalog/history precedence, explicit rate-limit fallback, and the
15-minute in-memory cache. **Not checked:** live NVDA speech, braille, focus,
and GitHub network behavior.
