<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:b0e6a836cec34070259b8e04e689986e41de718a931c74d54f23290475138966 -->

# Maintenance Policy

## Recurring Battle Cats game-data updates

This repository is intended to be maintained continuously, not updated only once.

- Keep the Japanese Battle Cats character encyclopedia / cat roster data aligned with the latest game-data release the project supports.
- Verify that the selected game-data version is actually available from the upstream BCSFE game-data source before changing the pinned version. Do not assume every game patch changes encyclopedia data.
- Update the BCSFE dependency when required for compatibility with the target data version.
- Preserve existing application behavior, save compatibility, unlocked/owned cat state, and Render deployment support.
- Extend existing saves by adding only missing encyclopedia entries; do not overwrite existing cat records or duplicate entries.
- Build the character-name catalog from a fresh `SaveFile(cc=jp, gv=target_version)`, not from the old `template_save.sav`.
- Version the character-name cache schema as well as the game-data version.
- Keep the game-data version in one shared source of truth and expose it in the app state where appropriate.
- Add self-tests for version selection, roster completion, preservation of existing records, and duplicate prevention.
- Update the README and this maintenance note when the update process or supported version changes.

## Current recorded baseline

- Repository: `tenma2066-tech/CatsDaiko5`
- Main branch: `main`
- Character encyclopedia data target last recorded: `15.7.2 (JP)`
- Character-name catalog loader: fresh target-version save; old cache schema rejected
- Latest Japanese game release checked on 2026-11-11: Ver.16.0.0 (released 2026-11-09). The public BCData/BCSFE data source currently exposes JP data only through 15.7.2; do not change `GAME_DATA_VERSION` to 16.0.0 or label the catalog 16.0.0 until an exact 16.0.0 JP dataset is published and successfully loaded. The public Battle Cats DB lists new Ver.16.0 entries at unit numbers 883-887 (plus a spirit entry at 888); include these in the full-catalog comparison when the authoritative dataset becomes available.
- Regression example: Rock Kitten / ロックこねこ, DB number 874, internal ID 873, introduced in Ver.15.6
- BCSFE dependency last recorded: `3.6.0`
- Automated JP catalog check completed successfully on 2026-11-11 using a fresh BCSFE 3.6.0 catalog: source latest `15.7.2`, 882 entries, extraction via `Cat.get_names_cls`; all current `ERROR_CAT_IDS` were present in that catalog. This confirms the published BCSFE data baseline, not Ver.16.0.0 game data.

## Full-catalog update procedure (required on every data update)

For every new target game-data release:

1. Verify that the matching Japanese game-data files are available and identify their version.
2. Verify the compatible BCSFE version.
3. Build the catalog from a fresh target-version save using `SaveFile(cc=jp, gv=target_version)`. Never rely on the legacy `template_save.sav` as the source of the current catalog.
4. Retrieve and inspect the complete current-version character data (IDs, names, alternate/form names, and relevant encyclopedia entries).
5. Compare the full set of current IDs and names with the previous supported catalog. Identify every addition, removal, changed name, added form/variant, and ordering or ID change.
6. Update roster completion and character-name search/catalog logic while preserving existing save records and unlock/ownership state and avoiding duplicates.
7. Invalidate/version caches when either the game-data version or catalog schema/source changes. Ensure the app cannot label stale catalog contents as current.
8. Add regression tests for the full catalog: expected IDs/count or a generated comparison manifest where practical. A canary such as ロックこねこ is not proof of full catalog parity.
9. Update the shared version constant, README, this maintenance note, and record the source release and verification results in the commit.

## Regression and verification rules

- Confirm the current-version game-data source is used, not the legacy template save.
- Compare the entire catalog against the target-version Japanese upstream data, including IDs, names, alternate/form names, and additions.
- A version label changing, one canary passing, or a GitHub commit existing is not sufficient evidence of a complete update.
- Run repository self-tests after each code update.
- Verify the Render build/deployment and real save behavior when those environments are available. Clearly record checks that could not be run.

## Automatic BCSFE dependency updates

- `.github/workflows/auto-update.yml` checks PyPI every Monday at 12:00 JST and supports manual execution.
- It updates only within the currently pinned major version.
<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:76b2c7d10b6332ee670bb68fb48d854026dc4a270760736e4fd70bd1cf1783d4 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:fbd1cc4b7a27b85f2be8c48c67a1e2e832349496b8a66ac86ff321cb4c39e867 -->

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:59e6d0868193e5aa50875f8051679b2a3cd87d99cff189ace15f5a7812c625fa -->

- The workflow installs the candidate BCSFE version and runs `smoke_bcsfe.py`, `selftest.py`, the audit and repair tests, `test_parallel.py`, `test_improvements.py`, and `test_github_auth_store.py`. It commits the dependency pin only if all checks pass.

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->

<!-- /coderabbit-ai-generated:v1 -->
- A successful mocked self-test or smoke test does not prove live PONOS server compatibility.

## Account copy / clone behavior
- Current experimental flow downloads the source save, serializes and reloads it into a distinct `SaveFile`, then issues a transfer code for the original account and calls `ServerHandler.create_new_account()` on the copy before issuing the copy's code.
- The copy's new inquiry/account ID is checked to differ from the source before the copied save is uploaded. The dry-run path only verifies local independent deserialization; it does not create a server account or issue codes.
- This is based on BCSFE's public `ServerHandler.create_new_account()` and `get_codes()` methods. PONOS does not document or officially guarantee this two-account workflow.
- **Real-device verification passed on 2026-11-08 (user-reported):** the original account and copied account both accepted their respective transfer-code pairs; after restarting the game, the user could log in to both accounts independently and continue playing. This confirms the end-to-end workflow on the tested device/account setup, but does not guarantee future compatibility after game or server changes.
- Keep the two returned code pairs separate and secure. Prefer a recoverable backup before experimenting, and never publish transfer/confirmation codes in logs, screenshots, issues, or commits.

## Save-data integrity audit (BAN-risk reduction project)

The first implementation stage focuses on objective structural integrity. It does not infer private server-side enforcement rules, identify hidden "marked" states, disguise modifications, or promise BAN avoidance.

- `save_audit.py` contains a read-only auditor for known BCSFE save structures. It also flags active favourites, storage entries, and lineup slots that still reference a known error-character ID, even if that character is not currently marked as owned.
- Current checks: character container/list integrity; known error-character IDs from the app's explicit exclusion list when owned (warning only); null, malformed, duplicate, out-of-range, and position-mismatched character IDs; core character field types; upgrade object shape; talent record shape and duplicate talent IDs; character storage item shape/type/reference, active favourites and lineup references to records whose owned flag is off (warning-only; no automatic repair); favourites and new-character flag maps; lineup slot shape and character references; non-negative item/currency fields; battle-item amount types/ranges; Gamatoto timer/flags/helper IDs/collaboration dictionaries; Ototo material amounts, timer/flags, cannon records and selected-part row shape; mission dictionaries, entry types, and nonnegative progress requirements; the 10-chapter/51-stage main-story structure and its stage/progress fields; nested event map/group/star/subchapter/stage records plus event metadata dictionaries; shared chapter/star/stage trees for tower, challenge, uncanny and catamin maps; EX-stage clear records; zombie-outbreak chapter/stage flags and timers; and Aku, gauntlet, collaboration-gauntlet and enigma-clear stage groups; item-reward claimed flags and reward metadata; timed-score stage values; Legend Quest, Zero Legends and dojo-chapter progression; dojo ranking/lock fields; Cat Shrine flags/timestamps; Behemoth Culling groups; gacha seeds, roll flags, step-up maps/durations; Enigma stage metadata; Talent Orb ID/value consistency; Wildcat Slots/Cat Scratcher event maps; map-reset timestamps; and top-level save list/dictionary types for enemy guides, unlock flags, event capsules, achievements, order IDs, first-lock flags and announcements.
- Known BCSFE structures were checked against public BCSFE-Python model definitions (Cat, Cats, StorageItem, Upgrade, BattleItems, LineUps, SaveFile, Gamatoto/Helpers, Ototo/Cannons/BaseMaterials, and Missions). Rules remain limited to structural invariants and conservative value checks.
- The auditor returns structured findings with severity and stable codes. It never mutates save data. Unusual form values and unknown storage item types are warnings, not automatic deletion instructions.
- `save_repair.py` plans and applies a narrow repair for IDs in the app's explicit `engine.ERROR_CAT_IDS` exclusion list when the record is owned or actively referenced by favourites (`True` only), storage, or lineups. An inactive favourite (`False`) does not trigger a reset.
- The repair uses BCSFE's `Cat.remove(reset=True, save_file=save)`, clears direct favourites/storage/lineup references, and preserves the positional cat list. Reference inspection accepts integer wrapper objects and tuple-backed reference collections where the contained records are mutable. Do not delete a cat row: BCSFE maps list positions to internal IDs.
- Before repair, the app serializes a backup. The transaction helper then repairs, serializes, reloads into a fresh `SaveFile`, and reruns the structural audit. Verification explicitly fails if either a known error character remains owned or any active favourite/storage/lineup reference to a known error ID remains. On failure it deserializes the original backup and returns that restored save to the handler; if restoration itself fails, the error is explicitly reported. Upload proceeds normally only after successful verification. This is a fail-safe for an unresolved integrity failure, not the feature's goal: the goal is to identify and safely correct known anomalies so normal use can continue.
- Automatic repair currently applies only to normal edit/write jobs. Clone mode intentionally preserves the source save exactly and does not run this repair.
- `test_save_audit.py` covers structural checks; `test_save_repair.py` covers repair planning, reset, reference cleanup, and fail-closed behavior.
- GitHub Actions runs both test modules alongside the existing app self-test. The job status API exposes the audit report, repair actions, and post-repair audit.
- This remains a partial audit and repair scope. Other anomalies are reported or block writing; they are not rewritten without an authoritative reconstruction rule. Hidden server-side account flags cannot be determined, and BAN avoidance is not guaranteed.

### Next implementation stages

1. Verify the auditor and repair path against actual BCSFE 3.6.0 save objects and current supported game data.
2. Extend read-only checks to remaining map families (tower, challenge, outbreak, gauntlet, enigma, Aku, timed-score, item-reward), gacha, and version-gated save sections using verified BCSFE schemas. Main-story, event-stage, tower, challenge, uncanny, catamin, EX-stage, zombie-outbreak, Aku, gauntlet, collaboration-gauntlet, enigma-clear, item-reward, timed-score, Legend Quest, Zero Legends, dojo, Cat Shrine Behemoth Culling, gacha, Enigma, Talent Orbs, Wildcat Slots, Cat Scratcher and map-reset and typed top-level collection structures now have initial schema-based audits.
3. Add repair rules only for objectively invalid structures with an authoritative reconstruction source. Keep ambiguous records unchanged for manual review.
4. Expand round-trip and rollback tests to cover every repair rule and supported game-version family.
5. Never treat unusual-but-valid, newly introduced, event, or collaboration data as invalid solely because it is absent from a stale local catalog.


### Additional cross-reference checks (v1.12)

- The read-only audit now warns when an active favourite or lineup slot points to a character record whose `unlocked` flag is 0. This is a consistency signal only: it does not block the normal save flow and is never auto-cleared, because game-version-specific edge cases may retain these references.
- Regression tests cover both warning cases and the normal owned-character case. Storage references are deliberately excluded from this heuristic because storage semantics may differ from the owned flag.

## Runtime catalog refresh and session ownership (v1.13)

- The manual latest-catalog action discovers Japanese versions from the configured BCSFE metadata source, sorts versions numerically, and requires an exact dataset match before publication. Failed builds retain the prior catalog. The pinned baseline remains 15.7.2; this change adds runtime discovery rather than declaring a newly verified pinned release.
- Normal edit workers select the verified catalog dataset without changing save serialization versions; existing records and ownership remain preserved by roster completion. Clone and integrity-only jobs retain their existing dataset behavior.
- Job access is bound to a random login-session token, not password ID or IP. Logout/relogin rotates ownership. Password revocation still invalidates the session.
- Code issuance is intentionally changed from app/SDK retries to one SDK attempt; ambiguous outcomes are reported and partial clone results retained. HTTP failure logs omit request and response data.


<!-- /coderabbit-ai-generated:v1 -->

## Parallel job accounting and status display (v1.14)

- The shared slot list is used by worker jobs and the short-lived warmup reservation. Never count the "warmup" sentinel as a running worker job.
- /api/state and /status/<job_id> take slot/queue snapshots while holding _jobs_lock. Report running jobs, warmup reservations, occupied/free slots, queue length, and per-slot states separately.
- The UI distinguishes the current session's own job from other server jobs. It must not label warmup as another parallel job or describe server-wide activity as a slot consumed by the current user's job.
- Keep one Gunicorn web worker for the in-memory scheduler. Increasing Gunicorn workers without moving scheduler state to shared storage would create independent queues and slot limits per process and invalidate global accounting.
- test_parallel.py retains spawned-process overlap/isolation tests and covers warmup accounting and per-job slot reporting. Mocked tests do not replace a live Render concurrency check.


## Menu-scoped save edits

- Shared post-edit cleanup may clear ad/pop-up notification flags, but must not set every cat's `gatya_seen` or `catguide_collected` flags. Those are character-specific game progress fields and must remain unchanged unless an explicit, dedicated menu is added and tested for that behavior.
- Regression coverage in `test_improvements.py` verifies that an unrelated item menu (XP) does not mark all characters as seen in gacha or the cat encyclopedia, while existing notification cleanup remains scoped to notification fields.

### 2026-11-08 implementation record

- Fixed the shared cleanup regression: unrelated edit menus no longer mark every character as gacha-seen or encyclopedia-collected.
- PR #7 merged into `main`; merge commit: `207b7f5d8856d139612ab52a3323a82fa214612`.
- PR #7 also includes PR #6's GitHub Actions checkout hardening (`persist-credentials: false`); PR #6 was closed as redundant.
- GitHub Actions run `42483423948` passed all seven workflow steps, including improvement regression tests.
- CodeRabbit's review did not complete because the PR was closed after merge. Render deployment and live in-game save behavior were not verified in this change.

## Owned-only gacha/encyclopedia visibility repair

- The explicit "未所持キャラのガチャ・図鑑記録を整理" option is available under the app's "その他" menu. It is not part of shared post-edit cleanup and does not run automatically.
- Only a character whose record ID matches its list position and whose ownership flag is a readable integer `unlocked == 0` is eligible. Owned characters (`unlocked == 1`), unknown ownership states, null/misaligned records, and malformed flag values are not changed.
- The repair changes only `gatya_seen` from 1 to 0 and/or `catguide_collected` from true to false on eligible unowned records. It never deletes/reorders cat rows or edits upgrades, forms, talents, favourites, storage references, lineups, gacha seeds, or roll counters.
- The operation serializes a backup, applies the two flag-only changes, serializes/reloads the save, compares core progression fields for every owned character, and runs the structural audit. If any verification fails, it attempts to deserialize the original backup and aborts the upload.
- The job log reports unowned records checked, changed records, each flag count, and ambiguous records skipped. The application version is 1.15.
- Regression tests cover unowned cleanup, owned-character preservation, unknown ownership/flag states, ID-position mismatch, serialize/reload verification, and backup restoration.
- This is a local save-flag normalization only. It cannot erase server-side history and is not a BAN-avoidance or account-safety guarantee.

### 2026-11-08 implementation record

- Added `repair_unowned_character_visibility_flags` and round-trip verification in `save_repair.py`.
- Added the explicit "その他" menu option and verified upload gate in `app.py`.
- Added regression tests in `test_save_repair.py` and user-facing documentation in `README.md`.
- Runtime deployment and live in-game behavior still require verification after Render deploys the latest `main`.


## Unlocked-lineup-only cross-reference audit

- The read-only unowned-character cross-reference warning checks only the first `lineups.unlocked_slots` presets. BCSFE stores additional locked lineup presets in the same list; their default or stale slot values are not active equipped-character references and must not generate repeated warnings.
- Structural validation of lineup collection shape and out-of-range character IDs still covers all stored presets. A real unowned-character reference in any unlocked preset continues to produce a warning.
- Regression tests cover an unowned reference in an unlocked preset, an ignored reference in a locked preset, and checking every unlocked preset.


## Concise user-facing logs

- Keep routine successful operations quiet. The job log should show only a few meaningful phase changes and the final result.
- Do not log raw combo/debug snapshots, repeated quantity echoes, routine roster-fill counts, routine error-character cleanup counts, or raw exception payloads.
- Deduplicate consecutive identical messages and retain only the latest 60 user-facing log lines.
- In ordinary mode, suppress advisory findings completely. In explicit 「データ保全確認」 mode, show one audit summary and place advisory findings in the warning panel; individual log lines are reserved for critical findings.
- Critical findings always remain visible and block upload. Keep the full audit report in job state; reducing log verbosity must never reduce the actual audit or change its safety decision.
- Regression tests verify ordinary/detailed audit output, duplicate suppression, and the bounded log tail.

## Character ID and error-character list maintenance

- Normalize both save-record IDs and picture-book `cat_id` values through the same strict integer-ID helper, including BCSFE wrappers exposing `.value`.
- Individual unlock, bulk unlock, and roster completion must use the same picture-book ID normalization.
- If the picture-book data cannot be read, fail closed: do not bulk-unlock characters without a verified visible-ID set.
- Keep one immutable `ERROR_CAT_IDS` source for individual unlock, bulk unlock, cleanup, and integrity repair. Do not duplicate the list in a second local constant.
- Treat the list as a historical project exclusion list, not proof of server-side BAN status. Before adding/removing IDs, compare with the latest JP catalog, document evidence, and add regression coverage. The weekly check flags IDs absent from the current published catalog for manual review; it does not silently rewrite the list.
- Regression tests cover wrapped IDs on both the save and picture-book sides, hidden entries, known exclusions, and roster completion.

## Continuous verification and data updates

- `.github/workflows/ci.yml` runs the smoke test, self-test, audit, repair, parallel, improvement, and GitHub-auth tests on pushes and pull requests.
- `.github/workflows/game-data-check.yml` checks the newest JP game-data version published by the pinned BCSFE source every Monday and on demand. It builds a fresh catalog using the same `_build_names` path as the app and reports the version, count, extraction method, and stale exclusion IDs.
- A newer published version is a review signal, not permission to change the pinned version automatically. Perform the full-catalog comparison procedure above and update the app only after the full regression suite passes.
- CI and the scheduled catalog check do not prove live PONOS server compatibility or real-device behavior.


## Combo encyclopedia preservation during visibility repair

- The owned-only gacha/character-encyclopedia cleanup must not change the independent combo encyclopedia state.
- Preserve combo_unlocks and combo_unlocked_10k_ur from the pre-repair save across serialization/reload. Restore these fields onto the verified save before upload and fail closed if they cannot be retained.
- Regression coverage simulates a reload path that drops combo fields and verifies the original combo state is restored.
- The ordinary-run self-test must expect advisory audit summaries to be hidden; detailed audit logging is asserted in integrity-only mode.


## Per-character customization and mobile UI (2026-11-13)

- The character management screen supports selecting multiple specific characters and setting each character independently.
- Optional per-character fields: base level, plus level, form target (1st/2nd/3rd), and all available talents / ultra talents.
- Empty fields must leave that aspect unchanged. The submitted payload includes every selected character so an empty customization still means unlock-only.
- Character unlock still uses the verified picture-book eligibility check and the existing error-character exclusion.
- Base-level and plus-level requests are checked against the current character-specific BCSFE limits before writing. Reject over-limit requests for that character and report the failure; do not silently clamp.
- Form changes must respect the selected character's available forms. Talent changes must use the character's available talent definitions and maximum levels.
- Keep the per-character controls usable on narrow mobile screens. Include a clear preview / summary before execution and regression tests proving unselected characters remain unchanged.
