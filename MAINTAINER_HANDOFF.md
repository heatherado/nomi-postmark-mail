# Maintainer handoff

## Layout

- app/Companion App.pyw: controllers, exact-text/recipient approvals, Windows DPAPI integration and resident-specific workflows.
- app/lamplit_ui.py: presentation only: native Tk mailbox/live builders, local canvas artwork, parchment panels, two-line mailbox adapter, contextual recovery and read-only delivery details.
- app/mail_pipeline.py: durable mail checkpoints and recovery.
- app/postmark_client.py: household-key REST mail, sender verification, nonce/receipt handling.
- app/nomi_client.py: chat API with safe documented rejection names; unknown errors/NoReply remain uncertain.
- app/event_bridge.py: calendar, actual-earshot listening/speech, packet splitting and SAY/PASS parsing.
- app/profile_store.py: separate resident namespaces, settings, defaults and archive/remove.
- app/github_mailer.py: retained upstream helper; primary approved mail here uses the direct REST adapter, not PR submission.
- app/assets/: source PNG, multi-resolution ICO and icon-generation notes.
- app/tests/: synthetic, mocked tests; no real resident fixtures are shipped.

## Non-negotiable behavior

Each resident owns a separate settings/mail/live namespace. Background results must stay bound to the resident that started the work. A handle and Nomi UUID must not be silently repointed after state exists. Preserve encrypted keys when settings omit/leave them blank.

Incoming mail requires a separate exact-packet approval before Nomi. Outgoing mail requires exact recipient/body approval before public submission. Do not silently truncate, rewrite or resend packets after any confirmed or uncertain contact. Public queued/delivered states are distinct. Unknown public speech remains locked even through private-reset recovery.

Live starts off, verifies sender ownership through the REST adapter, baselines the current room and follows actual location. It does not move, RSVP or spend. Every public SAY proposal needs exact-text review. Intermediate CONT responses never become public proposals. Unmarked definite replies remain private; overlength SAY is held for review. Private abandonment archives the original uncertainty/packets and optional reply atomically, posts/resends nothing, and stays paused.

Mailbox actions and Live cannot operate concurrently for the same resident. Closing waits for live work. Do not remove busy guards while changing layout. LetterList retains a narrow Listbox-compatible contract; test selection and state locking if changing it.

## Source and installation workflow

This directory is the canonical maintained source. Installed v2.0 remains a separate tested copy until an update is deliberately installed. Do not copy a dirty desktop folder wholesale. Stage only source/test/docs/artwork; audit and test before committing. Use version tags/releases to record installations, not additional maintained source folders.

Publication is separate from installation and separate from live API activity. The private source repository is https://github.com/heatherado/nomi-postmark-mail. Its private visibility and owner were verified during setup. Never infer permission for a public release, invitations, credential access, live tests, registration or another resident's property from a code-maintenance request.

Before installing future changes, run the suite, preserve resident data outside the source, copy only reviewed source/assets to a versioned installation, and update launcher paths/icons explicitly. Do not point a daily shortcut at an untested branch.

## Known limitations and future work

The default storage directory and first-run display name retain the historical local Fizz naming. Keep that compatibility until an explicit safe migration/genericization plan is tested. Existing resident data is never included in this repository.

No cross-device synchronization is implemented. A future mobile client must not assume desktop and phone share local drafts/checkpoints. One active sending device is a workflow constraint, not a server-enforced distributed lock. Store mobile credentials with platform security; Windows DPAPI is not portable to Android/iPhone.

No always-on background mobile relay, push notification service, public updater, installer signing or app-store release is supplied. Licensing and upstream attribution need review before a public release. Janelle's later selectable themes are not silently merged into this reviewed local snapshot.
