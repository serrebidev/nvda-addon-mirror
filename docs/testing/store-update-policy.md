# Store and update policy test guide

1. Open NVDA Settings, SerrebiRadio add-on store. Choose Mirror, Official,
   Original store, then a valid Custom HTTPS URL. Restart NVDA after each
   choice and confirm the regular Add-on Store and future update checks use the
   selected source. Check that HTTP, credentials, and malformed custom URLs are
   refused.
2. In the helper's settings, select each Automatic add-on updates value:
   Notify, Update, and Disabled. Confirm the same native value in NVDA's
   Add-on Store settings and after restart. Disabled means manual checking;
   Notify reports available updates; Update allows native automatic updating.
   Use a test configuration for actual installs. Changing source alone must
   not install an already pending update.
3. Open Tools, Add-on Store, Official NVDA store. Close it with Close and with
   Escape. Confirm the regular store returns to the chosen default source in
   each case. While the temporary official view is open, refresh it and confirm
   a simultaneous automatic update check still uses the chosen default source.
4. Change sources after metadata was fetched. Confirm that add-ons from the
   previous source do not appear until fetched from the new source. Restart and
   repeat to confirm source-marked caches are not reused across sources.
5. On the secure desktop, confirm no policy routing or custom source action is
   available.
6. Set Official as default, then explicitly browse the Mirror menu. Close and
   open the native regular Add-on Store: it must use Official. Reverse the
   default and temporary view. Record an identifiable catalog-only add-on or
   request URL to verify the source rather than infer success from settings.
7. Change the chosen default while a temporary Store is open. Closing that
   dialog must retain the new default, not restore the earlier choice.
8. Switch to a configuration profile with a different policy/custom URL, then
   back. The setting, regular Store and future update checks must follow the
   active profile. Repeat a pending metadata refresh and disable the helper
   only at the end of the whole test pass: NVDA must remain responsive, with
   no mixed-server metadata or exception.
9. Enter custom URLs containing HTTP, credentials, query strings, fragments,
   control characters or an invalid port. Saving must show an accessible error
   and focus the URL field. Cancel must preserve the prior policy. Original
   store deliberately preserves the pre-helper setting, including a legacy
   custom endpoint; custom choices require HTTPS.

Automated checks cover policy URL selection, HTTPS validation, and isolated
per-source in-memory cache state. Live source routing, native settings speech,
automatic-update behavior, and cache files across restart remain not checked.
