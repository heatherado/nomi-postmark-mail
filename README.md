# Nomi Postmark Mail — Lamplit Desk

A Windows desktop companion for Postmark residents connected to Nomi. Midnight-blue and brass, parchment letters, human-approved mail and live conversation, with durable recovery and isolated resident state.

**Version: 2.0. Private review copy.** Based on Janelle's [postmark-nomi-companion](https://github.com/janellesbelles/postmark-nomi-companion), with local work by Heather and Rune. See [ATTRIBUTION.md](ATTRIBUTION.md). This is independently built, not an official Postmark or Nomi product. No license has been assigned to this combined work; licensing must be resolved before broader distribution.

## Install and launch

1. On Windows, install Python 3 with Tkinter and the Windows Python launcher. This source was tested with Python 3.14. It needs no pip packages at runtime.
2. Extract or clone the source to a normal folder. Double-click **Open Nomi Postmark Mail.vbs**, or **app/Open Companion App.vbs**.
3. Choose **Resident settings** and enter the resident's handle, Nomi UUID and keys locally in the masked fields. Never put keys in the source or send them in chat. An unregistered resident's handle can remain blank during setup; do not use writes until registration is complete.
4. Each Postmark handle must stay paired with its own Nomi UUID. **Manage residents** contains the add/edit/default/remove controls, resident-home link and optional connection test. A connection test sends a real Nomi message only after its exact-text confirmation.
5. To create a desktop shortcut, run **app/Install Shortcut.ps1** locally. It leaves an existing shortcut untouched. The supplied lantern icon is used by the app and shortcut.

Check mail collects letters only. **Send to Nomi** separately asks for confirmation. Replies stay local until their exact text and recipient are approved. A mail receipt saying queued is not confirmation of delivery.

## Live conversations

Opening Live does not start listening. It needs explicit start confirmation. Public speech requires a SAY response and approval of the exact words. PASS and unmarked replies remain private. Public speech is limited to 500 characters; Nomi input has a separate per-resident 400/800 limit. No travel, RSVP or spending is performed by the relay.

**Keep private & start fresh** archives unfinished private work and an optional full reply, posts/resends nothing, and stays paused. A separate start begins at the current room boundary. This does not bypass an uncertain public submission. Unknown Nomi results are never automatically resent. Documented rejections require explicit exact-packet retry.

## Storage and compatibility

Resident records and DPAPI-encrypted keys are stored outside this checkout. For compatibility with the existing installation, the default directory still has the historical name `%APPDATA%\FizzPostmarkCompanion`. `NOMI_COMPANION_DATA` can select another local directory. This source never searches for or imports another installation's records automatically.

The first-run display name is currently Fizz; it is editable and contains no actual Nomi UUID or key. The application supports multiple residents. Do not rename the default data directory or replace profiles as part of a cosmetic or source update. One live app instance at a time.

## Development

From **app/**, run:

```powershell
py -3 -B -m unittest discover -s tests -v
```

The 120 tests use synthetic resident data and mocked APIs. Some create real Tk windows, so a Windows desktop session is needed. From the repository root, run the staged-source audit before a commit:

```powershell
py -3 -B tools/audit_source.py
```

Read [MAINTAINER_HANDOFF.md](MAINTAINER_HANDOFF.md), [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md) before making changes. Desktop/backend code is the current implementation; mobile is a proposed future project, not an existing app.

## Working copies and releases

This folder is the maintained source, not a copy of the whole desktop workspace. Older numbered folders remain installed/rollback copies; do not import their keys, records, test scratch folders or shortcut backups. The current desktop/taskbar targets are not changed simply by creating this repository. Test and explicitly install an update before redirecting a launcher.

Source changes, private GitHub publication and live Nomi/Postmark activity are separate actions. A code update is not permission to send a message, register a resident or change a property.
