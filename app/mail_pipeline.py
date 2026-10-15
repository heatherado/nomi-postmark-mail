"""Generic durable Postmark-to-Nomi mail pipeline.

Inject the resident identity, state path, and Nomi send callable at runtime.
This module never approves or submits mail; github_mailer.py owns that boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import time
import urllib.error
import urllib.request
from nomi_client import UncertainNomiResult


GITHUB_API = "https://api.github.com"
POSTMARK_REPO = "postmark-town/postmark"
LETTER_MAX_BYTES = 256_000


def request_json(url, *, headers=None, timeout=30):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "postmark-nomi-mail/1.0", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class AtomicState:
    def __init__(self, path):
        self.path = Path(path)

    def exists(self):
        return self.path.exists()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".mail-state-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            # Windows security/indexing processes can briefly hold a just-written
            # file. Retry only the local atomic rename; never repeat a network call.
            for attempt in range(6):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def inbox_index(resident_handle, *, request=request_json):
    url = (
        f"{GITHUB_API}/repos/{POSTMARK_REPO}/contents/"
        f"WHITE_PAGES/{resident_handle}/inbox?ref=main"
    )
    found = []
    for item in request(url):
        if item.get("type") == "file" and str(item.get("name", "")).endswith(".md"):
            found.append((item["name"][:-3], item["download_url"]))
        elif item.get("type") == "dir":
            children = request(item["url"])
            letter = next((child for child in children if child.get("name") == "letter.md"), None)
            if letter:
                found.append((item["name"], letter["download_url"]))
    return found


def parse_letter_text(text, resident_handle):
    if len(text.encode("utf-8")) > LETTER_MAX_BYTES:
        raise ValueError("Letter is too large for this bridge.")
    parts = text.split("---\n", 2)
    if len(parts) != 3 or parts[0] != "":
        raise ValueError("Letter has no YAML frontmatter.")
    fields = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip("\"'")
    body = parts[2].strip()
    if not all(fields.get(key) for key in ("id", "from", "to")) or not body:
        raise ValueError("Letter is missing required fields or body.")
    if fields["to"] != resident_handle:
        raise ValueError("Letter is addressed to another resident.")
    return fields, body


def fetch_letter(url, resident_handle):
    request = urllib.request.Request(url, headers={"User-Agent": "postmark-nomi-mail/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(LETTER_MAX_BYTES + 1)
    if len(raw) > LETTER_MAX_BYTES:
        raise ValueError("Letter is too large for this bridge.")
    return parse_letter_text(raw.decode("utf-8"), resident_handle)


def format_nomi_messages(fields, body, message_limit):
    limit = int(message_limit)
    if limit not in (400, 800):
        raise ValueError("Nomi message limit must be 400 or 800 characters.")
    intro = f"Postmark from {fields['from']}. "
    one = intro + "Quoted mail. Draft a reply; your human reviews before mailing.\n\n" + body
    if len(one) <= limit:
        return [one]

    longest_instruction = "FINAL: draft a reply; your human reviews before mailing."
    worst_wrapper = f"{intro}[30/30]. Quoted mail. {longest_instruction}\n\n"
    chunk_size = limit - len(worst_wrapper)
    if chunk_size < 100:
        raise ValueError("Sender name leaves too little room for a safe Nomi message.")

    chunks = []
    remaining = body
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, chunk_size + 1)
        if cut < chunk_size // 2:
            cut = remaining.rfind(" ", 0, chunk_size + 1)
        if cut < chunk_size // 2:
            cut = chunk_size
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if len(chunks) > 30:
        raise ValueError("Letter needs over 30 Nomi messages; handle it manually.")

    messages = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        instruction = (
            "Wait for FINAL."
            if index < total
            else "FINAL: draft a reply; your human reviews before mailing."
        )
        message = f"{intro}[{index}/{total}]. Quoted mail. {instruction}\n\n{chunk}"
        if len(message) > limit:
            raise AssertionError("Calculated Nomi message exceeds its configured limit.")
        messages.append(message)
    return messages


def is_ambiguous_nomi_error(error):
    if isinstance(error, UncertainNomiResult):
        return True
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
        if "timed out" in str(reason).lower():
            return True
    text = str(error).lower()
    if "timed out" in text or "timeout" in text:
        return True
    return any(f"http {code}" in text for code in range(500, 600))


class MailPipeline:
    def __init__(
        self,
        *,
        resident_handle,
        state_path,
        nomi_send,
        message_limit=400,
        part_pause=4.0,
        discover=None,
        fetch=None,
    ):
        self.resident_handle = resident_handle
        self.state = AtomicState(state_path)
        self.nomi_send = nomi_send
        self.message_limit = int(message_limit)
        self.part_pause = float(part_pause)
        self.discover = discover or (lambda: inbox_index(resident_handle))
        self.fetch = fetch or (lambda source: fetch_letter(source, resident_handle))

    def initialize(self, *, import_existing=False):
        if self.state.exists():
            raise RuntimeError("Mailbox is already initialized; refusing to reset its baseline.")
        discovered = list(self.discover())
        self.state.write({"version": 1, "seen": [] if import_existing else sorted({key for key, _source in discovered}), "letters": {}})
        return self.poll(discovered=discovered) if import_existing else {}

    def entries(self):
        state = self.state.read()
        changed = False
        for entry in state.get('letters', {}).values():
            if entry.get('status') == 'contacting_nomi':
                entry['status'] = 'response_unknown' if entry.get('active_nomi_part') else 'needs_attention'
                entry['error'] = 'Interrupted Nomi work. Check the normal chat before any uncertain resend.'
                changed = True
            elif entry.get('status') == 'submitting':
                entry['status'] = 'needs_send_check'
                entry['error'] = 'Interrupted submission. Inspect Postmark before retrying.'
                changed = True
            elif entry.get('status') == 'reading':
                entry['status'] = 'needs_attention'
                changed = True
        if changed:
            self.state.write(state)
        return state.get('letters', {})

    def poll(self, *, discovered=None):
        state = self.state.read()
        for key, source in (self.discover() if discovered is None else discovered):
            if key in state["seen"]:
                continue
            state["seen"].append(key)
            state["letters"][key] = {"status": "reading", "source": source}
            self.state.write(state)
            self._stage_delivery(state, key)
        return state["letters"]

    def _stage_delivery(self, state, key):
        entry = state['letters'][key]
        try:
            fields, body = self.fetch(entry['source'])
            messages = format_nomi_messages(fields, body, self.message_limit)
            entry.update(fields=fields, body=body, nomi_messages=messages,
                         nomi_parts=len(messages), nomi_message_limit=self.message_limit, status='awaiting_nomi_confirmation')
            entry.pop('error', None)
        except Exception as error:
            entry.update(status='needs_attention', error=str(error))
        self.state.write(state)
        return entry

    def prepare_delivery(self, key):
        state = self.state.read()
        entry = state['letters'][key]
        if entry.get('archived_at') or entry.get('status') != 'needs_attention' or entry.get('nomi_messages'):
            raise ValueError('Only an unread failed letter can be prepared again.')
        if int(entry.get('nomi_parts_completed', 0) or 0) > 0:
            raise ValueError('Confirmed parts exist but original packets are missing. Paste the final reply; do not rechunk or restart the letter.')
        return self._stage_delivery(state, key)

    def repack_unstarted(self):
        """Only explicit plan changes repack staged letters that never started."""
        state = self.state.read()
        for entry in state['letters'].values():
            if entry.get('status') != 'awaiting_nomi_confirmation' or entry.get('nomi_parts_completed') or entry.get('active_nomi_part') or entry.get('submission'):
                continue
            if not entry.get('fields') or entry.get('body') is None:
                continue
            messages = format_nomi_messages(entry['fields'], entry['body'], self.message_limit)
            entry.update(nomi_messages=messages, nomi_parts=len(messages), nomi_message_limit=self.message_limit)
        self.state.write(state)

    def confirm_delivery(self, key):
        state = self.state.read()
        if state['letters'][key].get('archived_at') or state['letters'][key].get('status') != 'awaiting_nomi_confirmation':
            raise ValueError('This letter is not waiting for Nomi delivery approval.')
        return self._prepare_and_send(state, key, completed=0)

    def _prepare_and_send(self, state, key, *, completed):
        entry = state["letters"][key]
        if entry.get('archived_at'):
            raise ValueError('Restore the archived letter before any delivery or recovery.')
        try:
            if entry.get('nomi_messages'):
                fields, body = entry['fields'], entry['body']
                messages = entry['nomi_messages']
            else:
                if completed:
                    raise ValueError('Original packets are missing after confirmed delivery. Recover manually; never restart or rechunk.')
                fields, body = self.fetch(entry["source"])
                messages = format_nomi_messages(fields, body, self.message_limit)
            previous_total = int(entry.get("nomi_parts", 0) or 0)
            if completed and previous_total != len(messages):
                raise RuntimeError(
                    "Letter now splits into a different number of parts; manual recovery is required."
                )
            if completed >= len(messages):
                raise RuntimeError(
                    "Every letter part is confirmed. Paste the Nomi's final reply instead of resending."
                )
            entry.update(
                {
                    "fields": fields,
                    "body": body,
                    "status": "contacting_nomi",
                    "nomi_parts": len(messages),
                    "nomi_messages": messages,
                }
            )
            entry.pop("error", None)
            entry.pop("timeout_detail", None)
            self.state.write(state)

            draft = ""
            reply_id = None
            for index, message in enumerate(messages[completed:], completed + 1):
                entry["active_nomi_part"] = index
                self.state.write(state)
                draft, reply_id = self.nomi_send(message)
                entry["nomi_parts_completed"] = index
                entry.pop("active_nomi_part", None)
                self.state.write(state)
                if index < len(messages) and self.part_pause > 0:
                    time.sleep(self.part_pause)

            entry.update(
                {"draft": draft, "nomi_reply_id": reply_id, "status": "pending"}
            )
            entry.pop("ambiguous_part", None)
            self.state.write(state)
            return entry
        except Exception as error:
            if is_ambiguous_nomi_error(error) and entry.get("status") == "contacting_nomi":
                entry["status"] = "response_unknown"
                ambiguous = entry.get("active_nomi_part")
                if ambiguous:
                    entry["ambiguous_part"] = ambiguous
                if ambiguous and ambiguous == entry.get("nomi_parts"):
                    entry["error"] = (
                        "The final part may have arrived. Check the normal Nomi chat and paste its reply if present."
                    )
                else:
                    entry["error"] = (
                        "A part may have arrived. Check the normal Nomi chat before any resend."
                    )
                entry["timeout_detail"] = str(error)
            else:
                entry["status"] = "needs_attention"
                entry["error"] = str(error)
            self.state.write(state)
            raise

    def _resume_checkpoint(self, state, entry, *, next_part=None, human_checked_chat=False):
        if entry.get('archived_at'):
            raise ValueError('Restore archived mail before recovery.')
        saved = int(entry.get('nomi_parts_completed', 0) or 0)
        if next_part is None:
            return saved
        total = int(entry.get('nomi_parts', 0) or 0)
        next_part = int(next_part)
        if total < 1 or not 1 <= next_part <= total + 1:
            raise ValueError(f'Next part must be between 1 and {total + 1}.')
        completed = next_part - 1
        if completed < saved:
            raise ValueError('Confirmed checkpoints cannot move backward.')
        if completed > saved:
            if not human_checked_chat:
                raise ValueError('Inspect normal Nomi chat before advancing a checkpoint.')
            entry.setdefault('manual_checkpoint_history', []).append({'previous_completed':saved, 'human_confirmed_through':completed, 'recorded_at':time.time()})
            entry['nomi_parts_completed'] = completed
            entry.pop('active_nomi_part', None)
            self.state.write(state)
        if completed == total:
            entry.update(status='response_unknown', error='All parts were confirmed in normal chat. Paste the final reply; nothing was resent.')
            self.state.write(state)
        return completed

    def continue_after_definite_failure(self, key, *, next_part=None, human_checked_chat=False):
        state = self.state.read()
        entry = state["letters"][key]
        if entry.get("status") != "needs_attention":
            raise ValueError("Only a definite failure can use safe continuation.")
        completed = self._resume_checkpoint(state, entry, next_part=next_part, human_checked_chat=human_checked_chat)
        if entry.get('status') == 'response_unknown':
            return entry
        return self._prepare_and_send(state, key, completed=completed)

    def resend_after_human_check(self, key, *, human_checked_chat=False, next_part=None):
        if not human_checked_chat:
            raise ValueError("The normal Nomi chat must be checked before resending an uncertain part.")
        state = self.state.read()
        entry = state["letters"][key]
        if entry.get("status") != "response_unknown":
            raise ValueError("This letter is not waiting on an uncertain Nomi response.")
        completed = self._resume_checkpoint(state, entry, next_part=next_part, human_checked_chat=True)
        if completed == int(entry.get('nomi_parts', 0)):
            return entry
        return self._prepare_and_send(state, key, completed=completed)

    def recover_reply(self, key, reply_text):
        reply_text = (reply_text or "").strip()
        if not reply_text:
            raise ValueError("Paste a non-empty Nomi reply.")
        state = self.state.read()
        entry = state["letters"][key]
        if entry.get('archived_at') or entry.get('status') in {'queued', 'delivered', 'submitting', 'needs_send_check', 'pr_open'} or entry.get('submission'):
            raise ValueError('Submitted replies are locked; recovery cannot change the approved envelope.')
        if entry.get("draft"):
            entry.setdefault("draft_history", []).append({"text": entry["draft"]})
        entry["draft"] = reply_text
        if entry.get("status") not in {"submitting", "needs_send_check", "pr_open"} and not entry.get("pr"):
            entry["status"] = "pending"
        entry["recovered_manually"] = True
        for field in ("error", "timeout_detail", "active_nomi_part", "ambiguous_part"):
            entry.pop(field, None)
        self.state.write(state)
        return entry

    def save_draft(self, key, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("The draft is empty.")
        state = self.state.read()
        entry = state["letters"][key]
        if entry.get("status") != "pending":
            raise ValueError("Only a held pending draft can be edited.")
        entry["draft"] = text
        entry["edited_by_human"] = True
        self.state.write(state)
        return entry

    def reject(self, key):
        state = self.state.read()
        entry = state["letters"][key]
        if entry.get("status") != "pending":
            raise ValueError("Only a held pending draft can be rejected.")
        entry["status"] = "rejected"
        self.state.write(state)
        return entry

    def clear_submitted(self, key):
        state = self.state.read()
        entry = state['letters'][key]
        if entry.get('status') not in {'queued', 'delivered', 'pr_open'}:
            raise ValueError('Only a confirmed submission can be cleared; uncertain work stays visible.')
        entry['cleared_from_mailbox_at'] = time.time()
        self.state.write(state)
        return entry

    def archive(self, key):
        state = self.state.read()
        entry = state['letters'][key]
        if entry.get('submission') or entry.get('pr') or entry.get('status') not in {'awaiting_nomi_confirmation', 'needs_attention', 'response_unknown', 'pending', 'rejected'}:
            raise ValueError('Only inactive, unsubmitted inbound mail can be archived.')
        entry['archived_at'] = time.time()
        self.state.write(state)
        return entry

    def restore(self, key):
        state = self.state.read()
        entry = state['letters'][key]
        if not entry.get('archived_at'):
            raise ValueError('This letter is not archived.')
        entry.pop('archived_at')
        entry['restored_at'] = time.time()
        self.state.write(state)
        return entry
