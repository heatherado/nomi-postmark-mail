# Initial private source baseline

The clean source copy was retested separately from the installed version using Windows/Python 3.14:

    Ran 120 tests
    OK

Tests include synthetic resident isolation, exact approval/rejection, mail receipts and uncertain-delivery locks, legacy recovery, private abandonment, Nomi error redaction/classification, packet sizing, Tk card selection, conditional controls and minimum-window geometry. No live Nomi or public Postmark calls were used. No real resident data is included.

The staged-source audit checked the exact Git index for private/runtime filenames, personal Windows paths, common token/private-key patterns and unexpected binary files. Matching values are never printed. This is a limited heuristic audit, supplemented by reviewed allowlisted file selection and staged-file inspection; it is not a universal secret detector.

Ignore rules were checked for settings, resident state, shortcut backups, temporary live-preview records and environment files. The installed v2.0 source/assets were copied by explicit allowlist. Generated test scratch directories, cache files, shortcut backups and earlier private Git history were excluded.

GitHub confirmed owner heatherado and private visibility before the initial upload. No collaborators were invited, public visibility enabled or license selected. Upstream provenance and the unresolved license status are documented separately.

The new source repository does not retarget existing daily launchers or migrate user records. The Windows app still uses its existing isolated data directory. Future source changes must be tested and explicitly installed; mobile and cross-device synchronization are not implemented here.
