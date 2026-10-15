# Security and private-data boundaries

This repository contains source, tests, documentation and project artwork only. Never commit profiles, settings, local mail/live state, resident directories, credentials (including encrypted credentials), logs, shortcuts, backups or test scratch directories. .gitignore is one safeguard, not a substitute for checking the exact staged files.

Enter API keys locally through the masked app fields. Keys are protected by Windows DPAPI. Do not put them in code, environment examples, command arguments, reports, screenshots, chat, Git remotes or commit messages. Never print credential helper output or raw authentication headers while troubleshooting.

Before every publication:

1. Inspect staged filenames and diff.
2. Run tools/audit_source.py. It reports filenames/rule names, never matching secret values. It is a limited heuristic audit, not proof that arbitrary source is safe.
3. Run mocked regression tests without live API access.
4. Check that versioned local backups, installed data and artwork caches are excluded.
5. Confirm intended remote, branch and visibility. The owner explicitly approved making this audited source snapshot public. Do not assume that permits publishing new files, granting collaborators access or changing the unresolved license status.

Never automatically resend a Nomi message or public submission with uncertain results. Preserve original resident pairing, exact in-flight packets, public nonce/receipt locks and confirmed checkpoints. Queued mail is not delivered mail. Keep private abandonment distinct from a confirmed delivery.

Do not paste vulnerability details, tokens or private correspondence in public issues. Report sensitive issues directly to the repository owner without including credentials. No external security-report endpoint is configured.
