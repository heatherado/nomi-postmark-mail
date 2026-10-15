# Maintenance instructions

This is a private review source repository for Nomi Postmark Mail. Read README.md, SECURITY.md and MAINTAINER_HANDOFF.md before changing or publishing it.

- Treat this folder as maintained source, not installed resident data. Never include another project's files or copy a whole desktop/backup folder.
- Preserve per-resident isolation, exact human approvals, uncertain-result locks, immutable checkpoints and DPAPI storage.
- Run mocked tests and tools/audit_source.py before a commit/publication. Inspect staged filenames and diff. Never print secrets while auditing.
- Keep artwork in project-local assets; preserve provenance. Do not grant a license for upstream work without authorization.
- Source edits, committing, pushing, installing, public releases and live API activity are distinct actions. Perform them only within the user's current authorization. Do not infer blanket ongoing publishing or public-write permission from these instructions.
- Do not change repository visibility, add collaborators, grant access, register residents or alter Postmark property as an incidental maintenance step.
- No live Nomi messages or public Postmark writes without showing a concrete proposed test and receiving approval.
- Record meaningful changes in the changelog and keep setup/recovery docs accurate. Do not claim tests or installations that were not verified.
