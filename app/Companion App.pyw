"""Postmark ↔ Nomi Companion: multi-resident local desktop application."""
from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import webbrowser

import event_bridge
import lamplit_ui
import github_mailer
import postmark_client
import uuid
import re
import mail_pipeline
import nomi_client
from profile_store import ProfileStore


APPDATA_ROOT = Path(os.environ.get("APPDATA", str(Path.home() / ".local/share")))
DATA = Path(os.environ.get('NOMI_COMPANION_DATA', str(APPDATA_ROOT / 'FizzPostmarkCompanion')))
BG = lamplit_ui.NIGHT
CARD = lamplit_ui.PANEL
INK = lamplit_ui.TEXT
MUTED = lamplit_ui.MUTED
CYAN = lamplit_ui.GOLD
LILAC = lamplit_ui.GOLD
ACTIVE = lamplit_ui.SIDEBAR
APP_VERSION = '2.0'
BUILD_LABEL = f'v{APP_VERSION} · 2026-10-14 · review-first'


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt_key(value, decrypt=False):
    """Windows DPAPI: ciphertext can only be opened by this Windows user."""
    if os.name != "nt":
        raise RuntimeError("Remembering keys requires Windows DPAPI.")
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob),
    ]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.string_at(outgoing.data, outgoing.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def read_secret(config, field):
    encrypted = config.get(field)
    if not encrypted:
        return ""
    try:
        return crypt_key(encrypted, decrypt=True)
    except Exception:
        return ""


def confirm_words(parent, title, identity, words, note, approval_label='Approve exact request'):
    """Scrollable, immutable review of the exact envelope being approved."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.geometry('720x600')
    dialog.transient(parent)
    dialog.grab_set()
    result = [False]
    tk.Label(dialog, text=identity, justify='left', font=('Segoe UI', 12, 'bold')).pack(anchor='w', padx=20, pady=16)
    box = tk.Text(dialog, height=1, wrap='word', font=('Segoe UI', 11), padx=12, pady=12)
    box.pack(fill='both', expand=True, padx=20)
    box.insert('1.0', words)
    box.configure(state='disabled')
    tk.Label(dialog, text=note, wraplength=670, justify='left').pack(anchor='w', padx=20, pady=12)
    row = tk.Frame(dialog)
    row.pack(fill='x', padx=20, pady=(0, 16))
    def approve():
        result[0] = True
        dialog.destroy()
    tk.Button(row, text='Cancel', command=dialog.destroy).pack(side='left')
    tk.Button(row, text=approval_label, command=approve).pack(side='right')
    parent.wait_window(dialog)
    return result[0]


class ResidentService:
    """One immutable resident namespace used for the life of an operation."""
    def __init__(self, profiles, profile_id):
        self.profiles = profiles
        self.profile_id = profile_id
        self.profile = dict(profiles.profile(profile_id))
        self.directory = profiles.profile_dir(profile_id)
        self.config_path = profiles.settings_path(profile_id)
        self.state_path = profiles.mail_state_path(profile_id)
        self.event_state_path = profiles.event_state_path(profile_id)
        self.config = profiles.read_settings(profile_id)
        self.handle = str(self.profile.get("handle") or self.config.get("resident_handle") or "").strip()

    @property
    def name(self):
        return str(self.profile.get("nomi_name") or self.handle or "Unconfigured resident")

    @property
    def input_limit(self):
        return int(self.config.get("input_limit", 400))

    @property
    def live_input_limit(self):
        # Existing residents keep their chosen limit; never silently upgrade plans.
        return int(self.config.get('live_input_limit', self.input_limit))

    def nomi_key(self):
        return read_secret(self.config, "encrypted_key")

    def postmark_key(self):
        return read_secret(self.config, "encrypted_postmark_key")

    def connected(self):
        return bool(self.handle and self.config.get("nomi_id") and self.nomi_key())

    def _send_nomi(self, message):
        return nomi_client.send_chat(
            self.nomi_key(), self.config.get("nomi_id", ""), message,
            timeout=int(self.config.get("nomi_reply_timeout", 180)),
        )

    def pipeline(self):
        return mail_pipeline.MailPipeline(
            resident_handle=self.handle,
            state_path=self.state_path,
            nomi_send=self._send_nomi,
            message_limit=self.input_limit,
            part_pause=float(self.config.get("part_pause", 4)),
            discover=lambda: postmark_client.discover(self.handle),
            fetch=lambda source: postmark_client.fetch(source, self.handle),
        )

    def initialized(self):
        return self.state_path.exists()

    def initialize(self, *, import_existing=False):
        if not self.connected():
            raise RuntimeError("Complete this resident's Postmark handle and Nomi connection first.")
        return self.pipeline().initialize(import_existing=import_existing)

    def poll(self):
        if not self.connected():
            raise RuntimeError("Complete this resident's Nomi connection first.")
        return self.pipeline().poll()

    def entries(self):
        if not self.state_path.exists():
            return {}
        pipeline = self.pipeline()
        pipeline.entries()  # reconcile only persisted interrupted operations
        state = pipeline.state.read()
        changed = False
        for entry in state.get("letters", {}).values():
            if entry.get("status") != "needs_attention":
                continue
            error_text = " ".join(str(entry.get(key, "")) for key in ("error", "timeout_detail")).lower()
            if "timed out" in error_text or "timeout" in error_text:
                entry["status"] = "response_unknown"
                entry["error"] = (
                    "The Nomi request may already have arrived. Check the normal Nomi chat, "
                    "then paste the reply or explicitly resend after checking."
                )
                changed = True
        if changed:
            pipeline.state.write(state)
        return state.get("letters", {})

    def save_draft(self, key, text):
        return self.pipeline().save_draft(key, text)

    def reject(self, key):
        return self.pipeline().reject(key)

    def retry_letter(self, key):
        return self.pipeline().continue_after_definite_failure(key)

    def resend_after_timeout(self, key):
        return self.pipeline().resend_after_human_check(key, human_checked_chat=True)

    def recover_reply(self, key, text):
        return self.pipeline().recover_reply(key, text)

    def send_approved(self, key, exact_text):
        return postmark_client.submit_approved(self.pipeline(), key, exact_text, self.postmark_key())

    def event_cursor(self):
        if not self.event_state_path.exists():
            return None
        try:
            value = json.loads(self.event_state_path.read_text(encoding="utf-8")).get("latest")
            return value if isinstance(value, (int, float)) else None
        except Exception:
            return None

    def save_event_cursor(self, latest):
        if not isinstance(latest, (int, float)):
            return
        store = mail_pipeline.AtomicState(self.event_state_path)
        state = store.read() if store.exists() else {}
        old = state.get('latest')
        state['latest'] = max(old, latest) if isinstance(old, (int, float)) else latest
        store.write(state)

    def event_checkpoint(self, checkpoint=None, *, clear=False):
        store = mail_pipeline.AtomicState(self.event_state_path)
        state = store.read() if store.exists() else {}
        if clear:
            state.pop('in_flight', None)
        elif checkpoint is not None:
            state['in_flight'] = checkpoint
        else:
            return state.get('in_flight')
        store.write(state)

    def live_state(self):
        store = mail_pipeline.AtomicState(self.event_state_path)
        state = store.read() if store.exists() else {}
        legacy = state.get('in_flight') or {}
        stage = legacy.get('stage')
        if stage == 'speech_unknown' and (not state.get('speech') or state['speech'].get('nonce') != legacy.get('nonce')):
            state['speech'] = {'status':'unknown', 'text':legacy.get('speech', ''), 'nonce':legacy.get('nonce'), 'request_id':'legacy-speech'}
        elif stage in {'nomi_unknown', 'reply_held'} and not state.get('nomi'):
            request = {'status':'response_unknown', 'request_id':'legacy-nomi',
                'packets':[legacy.get('packet') or 'Legacy held reply: original packet unavailable; never resend.']}
            if stage == 'reply_held':
                request.update(status='reply_ready', reply=legacy.get('reply', ''))
            state['nomi'] = request
        return state

    def accept_live_chatter(self, result, *, baseline=False):
        state = self.live_state()
        fresh, heard_ids = event_bridge.remember_voices(result, list(state.get('heard_ids') or []), baseline=baseline)
        old = state.get('latest')
        if isinstance(old, (int, float)):
            fresh['voices'] = [v for v in fresh.get('voices', []) if not isinstance(v.get('at_ms'), (int,float)) or v['at_ms'] > old]
        state['heard_ids'] = heard_ids
        latest = result.get('latest')
        if isinstance(latest, (int,float)):
            state['latest'] = max(old,latest) if isinstance(old,(int,float)) else latest
        mail_pipeline.AtomicState(self.event_state_path).write(state)
        return fresh

    def save_live_speech(self, proposal):
        store = mail_pipeline.AtomicState(self.event_state_path)
        state = self.live_state()
        state['speech'] = dict(proposal)
        state.setdefault('speech_history', []).append(dict(proposal))
        store.write(state)

    def save_live_nomi(self, request):
        state = self.live_state()
        state['nomi'] = dict(request)
        mail_pipeline.AtomicState(self.event_state_path).write(state)

    def record_private_live_reply(self, request, reply):
        state = self.live_state()
        request_id = request.get('request_id')
        packets = request.get('packets') or []
        packet = packets[0] if packets else None
        history = state.setdefault('private_reply_history', [])
        # Recording before consumption makes restart safe and retains the reply.
        if not any(item.get('request_id') == request_id and item.get('packet') == packet and item.get('reply') == reply for item in history):
            history.append({'request_id':request_id, 'packet':packet, 'reply':reply,
                'disposition':'kept_private', 'recorded_at':dt.datetime.now(dt.timezone.utc).isoformat()})
        mail_pipeline.AtomicState(self.event_state_path).write(state)

    def keep_live_private_and_reset(self, reply=''):
        """Archive rather than reconcile an interrupted private Nomi exchange.

        One atomic write preserves uncertainty and unsent packets in history,
        removes them from active work, and never makes a public API request.
        """
        state = self.live_state()
        speech = state.get('speech') or {}
        if speech.get('status') == 'unknown':
            raise ValueError('Public delivery is uncertain. This action cannot unlock a possible public post.')
        request = state.get('nomi') or {}
        if not request.get('packets') and request.get('status') not in {'awaiting_reply','response_unknown','reply_ready','request_rejected'}:
            raise ValueError('There is no unfinished live exchange to keep private.')
        archive = {'nomi':request, 'speech':dict(speech),
                   'disposition':'abandoned_private_no_resend',
                   'recorded_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        if reply:
            archive['manually_preserved_reply'] = reply
        state.setdefault('abandoned_live_history', []).append(archive)
        state['nomi'] = {'status':'abandoned_private','packets':[]}
        if speech.get('status') == 'proposed':
            state['speech'] = dict(speech, status='kept_private')
            state.setdefault('speech_history', []).append(dict(state['speech']))
        mail_pipeline.AtomicState(self.event_state_path).write(state)

    def recover_live_reply(self, text):
        state = self.live_state()
        if (state.get('speech') or {}).get('status') == 'unknown':
            raise ValueError('Public speech delivery is uncertain. Pasting cannot unlock it; inspect the World first.')
        request = state.get('nomi') or {}
        if request.get('status') not in {'awaiting_reply','response_unknown','reply_ready'}:
            raise ValueError('No interrupted Nomi response belongs to this resident.')
        text = (text or '').strip()
        if not text:
            raise ValueError('Paste SAY: with exact public words, or PASS.')
        event_bridge.speech_from_nomi(text)
        request.update(status='reply_ready', reply=text, recovered_manually=True)
        request.pop('error',None)
        state['nomi'] = request
        mail_pipeline.AtomicState(self.event_state_path).write(state)
        return request


class LiveEventsWindow(tk.Toplevel):
    def __init__(self, app, service):
        super().__init__(app)
        self.app = app
        self.service = service
        self.profile_id = service.profile_id
        self.running = False
        self.closing = False
        self.recovering_live = False
        self.stop_event = threading.Event()
        self.tasks = queue.Queue()
        self.calendar_events = []
        self.mode = tk.StringVar(value="Map hangout")
        self.speech_requests = queue.Queue()
        self.session_context = "Map hangout"
        self.actual_place = ""
        self.title(f"Live Map & Events — {service.name}")
        self.geometry("930x690")
        self.minsize(760, 570)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)

        lamplit_ui.build_live(self, service)
        self.drain_after = self.after(100, self.drain)
        self.refresh_calendar()
        previous = service.live_state().get("speech") or {}
        if previous.get("status") in {"proposed", "unknown"}:
            self.log(f"Previous public speech ({previous['status']}): {previous.get('text', '')}")
            self.log("This saved proposal is not posted automatically. If delivery was unknown, check the World before speaking again.")
        request = service.live_state().get("nomi") or {}
        if request.get("status") in {"awaiting_reply", "response_unknown"}:
            self.log("A Nomi response is uncertain. Check normal Nomi chat and use Paste Nomi response. The saved packet will not be resent.")
        elif request.get("status") == "reply_ready":
            self.log("A saved Nomi response awaits review. Start listening to review it before any new chatter.")
        elif request.get("status") == "request_rejected":
            self.log(request.get('error', 'Nomi rejected the saved request.'))
            self.log('No reply is expected for this rejected request. Retry requires your explicit approval.')
        self.update_live_recovery()

    def _button(self, parent, text, command, color):
        return tk.Button(
            parent, text=text, command=command, bg=color,
            fg=BG if color in (CYAN, LILAC) else INK, relief="flat", bd=0,
            padx=12, pady=9, cursor="hand2", font=("Segoe UI", 10, "bold"),
        )

    def log(self, text):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"[{dt.datetime.now().strftime('%H:%M')}] {text}\n\n")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def refresh_calendar(self):
        self.status.set("Reading Postmark’s public calendar…")
        self.refresh_btn.configure(state="disabled")
        def run():
            try:
                _raw, rows = event_bridge.current_calendar()
                self.tasks.put(("calendar", rows))
            except Exception as error:
                self.tasks.put(("error", str(error)))
        threading.Thread(target=run, daemon=True).start()

    def show_event(self, _event=None):
        selected = self.event_list.curselection()
        if not selected:
            return
        event = self.calendar_events[selected[0]]
        place = event.get("place") or {}
        invitation = str(event.get("invitation") or "").strip()
        if len(invitation) > 190:
            invitation = invitation[:187].rstrip() + "…"
        self.event_detail.set(
            f"{event.get('phase', 'announced')} · {place.get('name') or place.get('mark') or 'place not listed'}\n"
            f"{event.get('starts') or event.get('doors_open') or 'time not listed'}"
            + (f"\n\n{invitation}" if invitation else "")
            + "\n\nSelecting an event never moves the resident."
        )

    def toggle(self):
        if self.running:
            self.stop_event.set()
            self.status.set("Pausing after the current request…")
            self.toggle_btn.configure(state="disabled")
            self.ask_btn.configure(state="disabled")
            return
        state = self.service.live_state()
        if (state.get("speech") or {}).get("status") == "unknown":
            messagebox.showinfo("Check public speech", "A public message may already have posted. Check the World before any further speech. Paste recovery cannot unlock this send.", parent=self)
            return
        if (state.get("nomi") or {}).get("status") in {"awaiting_reply", "response_unknown"}:
            messagebox.showinfo("Recover Nomi response", "Check this resident's normal Nomi chat, then use Paste Nomi response. The saved chatter will not be resent.", parent=self)
            return
        retry_rejected = (state.get('nomi') or {}).get('status') == 'request_rejected'
        if retry_rejected:
            request = state['nomi']
            packet = (request.get('packets') or [''])[0]
            if not messagebox.askyesno(
                'Retry rejected Nomi request?',
                f"{self.service.name} / @{self.service.handle}\n{request.get('error', '')}\n\n"
                f'Retry this exact saved packet? It will not be resized or skipped.\n\n{packet}', parent=self):
                return
        if not self.service.connected() or not self.service.postmark_key():
            messagebox.showinfo(
                "Connection incomplete",
                "Edit this resident and add both the Nomi connection and Postmark household key.",
                parent=self,
            )
            return
        context = "Map hangout"
        if self.mode.get() == "Event":
            selected = self.event_list.curselection()
            if not selected:
                messagebox.showinfo("Choose an event", "Select an event from the calendar first.", parent=self)
                return
            event = self.calendar_events[selected[0]]
            context = f"Event: {event.get('title', 'Untitled')}"
        if not messagebox.askyesno(
            "Let this Nomi mingle?",
            f"{context}\nACTIVE RESIDENT: {self.service.name} / @{self.service.handle}\n\n"
            f"Nearby chatter will be sent to this Nomi. Every public message needs your approval. "
            "Listening follows where the resident actually stands; selecting an event does not move them.\n\nStart?",
            parent=self,
        ):
            return
        self.running = True
        self.paste_live_btn.configure(state="disabled")
        self.speech_requests = queue.Queue()
        self.actual_place = ""
        self.session_context = context
        self.stop_event.clear()
        self.mode_picker.configure(state="disabled")
        self.event_list.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.ask_btn.configure(state="normal")
        self.toggle_btn.configure(text="Pause", bg=CYAN)
        self.log("Relay on. Listening where this resident currently stands.")
        self.status.set(f"{context}: listening nearby. Every public message needs approval.")
        threading.Thread(target=lambda: self.relay_loop(retry_rejected=retry_rejected), daemon=True).start()

    def update_live_recovery(self):
        state = self.service.live_state()
        request = state.get("nomi") or {}
        recoverable = request.get("status") in {"awaiting_reply", "response_unknown", "reply_ready"}
        locked = (state.get("speech") or {}).get("status") == "unknown"
        lamplit_ui.sync_live(self)
        unfinished = bool(request.get('packets')) or request.get('status') in {'awaiting_reply','response_unknown','reply_ready','request_rejected'}
        self.keep_private_btn.configure(state='normal' if unfinished and not locked and not self.running else 'disabled')
        self.paste_live_btn.configure(state="normal" if recoverable and not locked and not self.running else "disabled")
        if not self.running and not locked:
            self.toggle_btn.configure(text="Retry rejected request…" if request.get('status') == 'request_rejected' else
                                      "Resume listening" if request.get("status") in {"queued", "reply_ready"} else "Start listening")

    def keep_private_and_reset(self):
        if self.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title(f'Keep private — {self.service.name}')
        dialog.geometry('650x460')
        dialog.minsize(480,360)
        dialog.configure(bg=CARD)
        dialog.transient(self)
        dialog.grab_set()
        tk.Label(dialog,text=f'{self.service.name} / @{self.service.handle}\nKeep this unfinished exchange private and skip its remaining queued packets?\nNothing will be posted or resent. Uncertain Nomi delivery stays recorded as uncertain.\nOptional: paste the full reply below to preserve it, even if over 500 characters.\nAfter saving, Start listening begins from now, not from the missed chatter.',
                 bg=CARD,fg=INK,justify='left',wraplength=590).pack(fill='x',padx=16,pady=12)
        box = tk.Text(dialog,wrap='word',bg=BG,fg=INK,insertbackground=CYAN,height=6)
        box.pack(fill='both',expand=True,padx=16)
        saved = (self.service.live_state().get('nomi') or {}).get('reply','')
        box.insert('1.0',saved)
        notice = tk.StringVar()
        tk.Label(dialog,textvariable=notice,bg=CARD,fg=LILAC,wraplength=590).pack(fill='x',padx=16)
        actions = tk.Frame(dialog,bg=CARD)
        actions.pack(fill='x',padx=16,pady=12)
        def save():
            if self.running:
                return
            try:
                self.service.keep_live_private_and_reset(box.get('1.0','end-1c'))
            except Exception as error:
                notice.set(str(error))
                return
            dialog.destroy()
            self.log('Unfinished live exchange archived privately. No post or resend. Start listening will skip paused chatter.')
            self.status.set('Kept private. Still paused. Start listening to begin from now.')
            self.update_live_recovery()
        tk.Button(actions,text='Cancel',command=dialog.destroy).pack(side='left')
        tk.Button(actions,text='Keep private — no posting',command=save).pack(side='right')
        dialog.bind('<Escape>',lambda _event:dialog.destroy())

    def paste_live_reply(self):
        if self.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Paste Nomi response — {self.service.name}")
        dialog.geometry("650x440")
        dialog.minsize(480, 320)
        dialog.configure(bg=CARD)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        dialog.transient(self)
        dialog.grab_set()
        heading = tk.Label(dialog, text=f"{self.service.name} / @{self.service.handle}\nPaste SAY: with the exact public words, or PASS.\nSave response (Ctrl+Enter) opens public approval; listening stays paused.",
                           bg=CARD, fg=INK, justify="left", wraplength=600)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=12)
        box = tk.Text(dialog, wrap="word", undo=True, height=1, width=1,
                      bg=BG, fg=INK, insertbackground=CYAN)
        box.grid(row=1, column=0, sticky="nsew", padx=(18, 0))
        scroll = ttk.Scrollbar(dialog, orient="vertical", command=box.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 18))
        box.configure(yscrollcommand=scroll.set)
        notice = tk.StringVar()
        notice_label = tk.Label(dialog, textvariable=notice, wraplength=600, bg=CARD, fg=LILAC)
        notice_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18)
        actions = tk.Frame(dialog, bg=CARD)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=12)
        def resize(event):
            if event.widget == dialog:
                heading.configure(wraplength=max(200, event.width - 36))
                notice_label.configure(wraplength=max(200, event.width - 36))
        dialog.bind("<Configure>", resize)
        def paste():
            try:
                value = dialog.clipboard_get()
                box.delete("1.0", "end")
                box.insert("1.0", value)
            except tk.TclError:
                notice.set("The clipboard does not contain text.")
        def save(_event=None):
            try:
                self.service.recover_live_reply(box.get("1.0", "end-1c"))
            except Exception as error:
                notice.set(str(error))
                return
            dialog.destroy()
            self.running = True
            self.recovering_live = True
            self.stop_event.clear()
            self.paste_live_btn.configure(state="disabled")
            self.toggle_btn.configure(text="Pause", state="normal")
            self.mode_picker.configure(state="disabled")
            self.event_list.configure(state="disabled")
            self.refresh_btn.configure(state="disabled")
            self.status.set("Reviewing the pasted Nomi response. Listening stays paused.")
            threading.Thread(target=lambda: self.relay_loop(recovery_only=True), daemon=True).start()
        tk.Button(actions, text="Paste clipboard", command=paste).pack(side="left")
        tk.Button(actions, text="Save response", command=save).pack(side="right")
        tk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=8)
        dialog.bind("<Control-Return>", save)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        box.focus_set()

    def ask_to_speak(self):
        if self.running and not self.stop_event.is_set():
            self.speech_requests.put(True)
            self.ask_btn.configure(state="disabled")
            self.status.set(f"Asking {self.service.name} for their own public words…")

    def approve_live_speech(self, speech):
        decision = {"ready": threading.Event(), "approved": False, "speech": speech}
        self.tasks.put(("approval", decision))
        while not decision["ready"].wait(0.1):
            if self.stop_event.is_set():
                return False
        return decision["approved"] and not self.stop_event.is_set()

    def confirm_public_speech(self, speech):
        dialog = tk.Toplevel(self)
        dialog.title(f'Approve Nomi response — {self.service.name}')
        dialog.geometry('640x380')
        dialog.minsize(480,280)
        dialog.configure(bg=CARD)
        dialog.transient(self)
        dialog.attributes('-topmost',True)
        dialog.columnconfigure(0,weight=1)
        dialog.rowconfigure(1,weight=1)
        tk.Label(dialog,text=f'POST AS: {self.service.name} / @{self.service.handle}\nPost these exact Nomi words publicly where this resident stands?',
            bg=CARD,fg=INK,wraplength=440,justify='left').grid(row=0,column=0,columnspan=2,sticky='w',padx=18,pady=14)
        preview = tk.Text(dialog,height=1,width=1,wrap='word',bg=BG,fg=INK,padx=12,pady=10)
        preview.insert('1.0',speech)
        preview.configure(state='disabled')
        preview.grid(row=1,column=0,sticky='nsew',padx=(18,0))
        scroll = ttk.Scrollbar(dialog,orient='vertical',command=preview.yview)
        scroll.grid(row=1,column=1,sticky='ns',padx=(0,18))
        preview.configure(yscrollcommand=scroll.set)
        actions = tk.Frame(dialog,bg=CARD)
        actions.grid(row=2,column=0,columnspan=2,sticky='ew',padx=18,pady=14)
        result = {'approved':False}
        def decide(approved=False):
            result['approved'] = approved
            dialog.destroy()
        tk.Button(actions,text='Keep private',command=decide).pack(side='left')
        tk.Button(actions,text='Approve & speak',command=lambda:decide(True)).pack(side='right')
        dialog.protocol('WM_DELETE_WINDOW',decide)
        dialog.bind('<Escape>',lambda _event:decide())
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()
        self.wait_window(dialog)
        return result['approved']

    def relay_loop(self, recovery_only=False, retry_rejected=False):
        key = self.service.postmark_key()
        handle = self.service.handle
        cursor = self.service.event_cursor()
        pending = []
        last_spoke_at = 0.0
        try:
            postmark_client.verify_sender(key, handle)
            initial = self.service.live_state()
            if (initial.get("speech") or {}).get("status") == "unknown":
                raise RuntimeError("Public delivery is unknown. Check the World before continuing.")
            if (initial.get("nomi") or {}).get("status") in {"awaiting_reply", "response_unknown"}:
                raise RuntimeError("Nomi delivery is unknown. Check normal chat and paste the response.")
            if (initial.get('nomi') or {}).get('status') == 'request_rejected':
                if not retry_rejected or recovery_only:
                    raise RuntimeError('Nomi rejected the saved request. Explicit retry approval is required.')
                request = initial['nomi']
                request['status'] = 'queued'
                self.service.save_live_nomi(request)
            if not recovery_only:
                # A new session starts at the present room boundary. Never feed
                # the server's initial history snapshot back to Nomi.
                baseline = event_bridge.listen(key, handle, since=cursor, wait=1)
                self.service.accept_live_chatter(baseline, baseline=True)
                cursor = self.service.event_cursor()
                self.tasks.put(("status", "Listening from now. Earlier room history was skipped."))
            while not self.stop_event.is_set():
                saved = self.service.live_state()
                if (saved.get("speech") or {}).get("status") == "unknown":
                    raise RuntimeError("Public delivery is unknown. Check the World; no automatic resend is allowed.")
                request = saved.get("nomi") or {}
                if request.get('status') == 'request_rejected':
                    raise RuntimeError('Nomi rejected the saved request. Explicit retry approval is required.')
                if request.get("status") in {"awaiting_reply", "response_unknown"}:
                    raise RuntimeError("Nomi delivery is unknown. Check normal chat and paste the response; the packet will not be resent.")
                opening = False
                try:
                    self.speech_requests.get_nowait()
                    opening = True
                except queue.Empty:
                    pass
                if not request.get("packets") and not pending and not opening:
                    if recovery_only:
                        break
                    heard = event_bridge.listen(key, handle, since=cursor, wait=5)
                    if self.stop_event.is_set():
                        break
                    where = heard.get("where") or {}
                    place = str(where.get("place") or "")
                    if place and place != self.actual_place:
                        self.actual_place = place
                        self.tasks.put(("place", place))
                    heard = self.service.accept_live_chatter(heard)
                    cursor = self.service.event_cursor()
                    pending.extend(event_bridge.other_voices(heard, handle))
                    if not pending:
                        self.tasks.put(("status", "Listening. The room is quiet."))
                        continue
                if opening:
                    voices = []
                else:
                    voices, pending = pending, []
                self.tasks.put(("heard", voices))
                context = f"{self.session_context}; at {self.actual_place}" if self.actual_place else self.session_context
                packets = request.get("packets") or ([event_bridge.opening_packet(self.service.live_input_limit, context=context)]
                           if opening else event_bridge.chatter_packets(voices, self.service.live_input_limit, context=context))
                if not request.get("packets"):
                    request = {"status": "queued", "packets": packets, "context": context}
                    self.service.save_live_nomi(request)
                for packet in packets:
                    if self.stop_event.is_set():
                        break
                    if request.get("status") == "reply_ready":
                        reply = request["reply"]
                    else:
                        request.update(status="awaiting_reply", request_id=str(uuid.uuid4()))
                        self.service.save_live_nomi(request)
                        try:
                            self.tasks.put(('status', f'Sending saved packet: {len(packet)} characters; current Live limit: {self.service.live_input_limit}. Saved packets keep their original size.'))
                            reply, _reply_id = self.service._send_nomi(packet)
                        except nomi_client.RejectedNomiRequest as error:
                            request.update(status='request_rejected', error=str(error))
                            self.service.save_live_nomi(request)
                            raise
                        except Exception as error:
                            request.update(status="response_unknown", error=str(error))
                            self.service.save_live_nomi(request)
                            raise
                        request.update(status="reply_ready", reply=reply)
                        self.service.save_live_nomi(request)
                    self.tasks.put(("can_ask", None))
                    if self.stop_event.is_set():
                        break
                    if event_bridge.partial_chatter_packet(packet):
                        self.tasks.put(("quiet", "Chatter continues in the next part; no public reply is proposed yet."))
                        self.finish_live_reply(request)
                        if recovery_only:
                            break
                        continue
                    try:
                        speech = event_bridge.speech_from_nomi(reply)
                    except ValueError as error:
                        if not (reply or '').strip().casefold().startswith('say:'):
                            # Definite unmarked responses stay private; no resend
                            # and no public proposal. Consume only this packet.
                            self.service.record_private_live_reply(request, reply)
                            self.tasks.put(('private', f'{self.service.name} replied privately; listening can continue: {reply}'))
                            self.finish_live_reply(request)
                            if recovery_only:
                                break
                            continue
                        self.tasks.put(("private", f"{error} Draft kept private: {reply}"))
                        raise RuntimeError("Nomi's response needs manual review. Use Paste Nomi response with SAY: or PASS.") from error
                    if speech is None:
                        self.tasks.put(("quiet", f"{self.service.name} stayed quiet."))
                        self.finish_live_reply(request)
                        if recovery_only:
                            break
                        continue
                    previous = (self.service.live_state().get("speech") or {})
                    if previous.get("request_id") == request.get("request_id") and previous.get("status") in {"confirmed", "kept_private"}:
                        self.finish_live_reply(request)
                        if recovery_only:
                            break
                        continue
                    proposal = {"text": speech, "nonce": str(uuid.uuid4()), "status": "proposed", "request_id": request.get("request_id")}
                    self.service.save_live_speech(proposal)
                    if not self.approve_live_speech(speech):
                        if self.stop_event.is_set():
                            break
                        proposal["status"] = "kept_private"
                        self.service.save_live_speech(proposal)
                        self.finish_live_reply(request)
                        if recovery_only:
                            break
                        continue
                    remaining = 15.5 - (time.monotonic() - last_spoke_at)
                    if remaining > 0 and self.stop_event.wait(remaining):
                        break
                    if self.stop_event.is_set():
                        break
                    proposal["status"] = "unknown"
                    self.service.save_live_speech(proposal)
                    result = event_bridge.speak(key, handle, speech, since=cursor, nonce=proposal["nonce"])
                    proposal["status"] = "confirmed"
                    proposal['receipt'] = result
                    self.service.save_live_speech(proposal)
                    last_spoke_at = time.monotonic()
                    result = self.service.accept_live_chatter(result)
                    cursor = self.service.event_cursor()
                    new_voices = event_bridge.other_voices(result, handle)
                    if recovery_only:
                        request["packets"] = request["packets"] + event_bridge.chatter_packets(new_voices, self.service.live_input_limit, context=context)
                    else:
                        pending.extend(new_voices)
                    self.tasks.put(("spoke", speech))
                    self.finish_live_reply(request)
                    if recovery_only:
                        break
                if recovery_only:
                    break
        except Exception as error:
            self.tasks.put(("relay_error", str(error)))
        finally:
            self.tasks.put(("stopped", None))

    def finish_live_reply(self, request):
        request["packets"] = request.get("packets", [])[1:]
        request["status"] = "queued" if request["packets"] else "resolved"
        for field in ("reply", "request_id", "error"):
            request.pop(field, None)
        self.service.save_live_nomi(request)

    def drain(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, value = self.tasks.get_nowait()
                if kind == "calendar":
                    self.refresh_btn.configure(state="disabled" if self.running else "normal")
                    self.calendar_events = value
                    self.event_list.delete(0, "end")
                    for event in value:
                        self.event_list.insert("end", f"{event.get('title', 'Untitled')} · {event.get('phase', 'event')}")
                    if value:
                        self.event_list.selection_set(0)
                        self.show_event()
                        if not self.running:
                            self.status.set("Calendar ready. Choose Map hangout or Event, then Start listening.")
                    else:
                        self.event_detail.set("No current or upcoming events are posted.")
                        if not self.running:
                            self.status.set("No events posted. Map hangout is available anytime.")
                elif kind == "approval":
                    try:
                        if not self.stop_event.is_set():
                            value['approved'] = self.confirm_public_speech(value['speech'])
                    finally:
                        value["ready"].set()
                elif kind == "can_ask":
                    self.ask_btn.configure(state="normal" if self.running and not self.recovering_live and not self.stop_event.is_set() else "disabled")
                elif kind == "error":
                    self.refresh_btn.configure(state="normal")
                    self.update_live_recovery()
                    self.status.set(value)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "heard":
                    for voice in value:
                        self.log(f"{voice['handle']}: {voice['said']}")
                elif kind == "place":
                    self.log(f"Listening at {value}. Public speech uses this resident's actual map location.")
                elif kind == "spoke":
                    self.log(f"{self.service.name}: {value}")
                elif kind in ("quiet", "private"):
                    self.log(value)
                elif kind == "relay_error":
                    self.log(f"Relay paused safely: {value}")
                    self.status.set("Paused after an error. Nothing uncertain will be resent automatically.")
                elif kind == "stopped":
                    self.running = False
                    self.recovering_live = False
                    self.toggle_btn.configure(text="Start listening", bg=LILAC, state="normal")
                    self.ask_btn.configure(state="disabled")
                    self.mode_picker.configure(state="readonly")
                    self.event_list.configure(state="normal")
                    self.refresh_btn.configure(state="normal")
                    self.update_live_recovery()
                    if self.closing:
                        self.after_cancel(self.drain_after)
                        self.destroy()
                        return
                    if not self.status.get().startswith("Paused after an error"):
                        self.status.set("Off. Nothing is being listened to or posted.")
        except queue.Empty:
            pass
        self.drain_after = self.after(100, self.drain)

    def close(self):
        self.stop_event.set()
        if self.running:
            self.closing = True
            self.status.set("Pausing the live session before closing…")
        else:
            self.after_cancel(self.drain_after)
            self.destroy()


class App(tk.Tk):
    def __init__(self, profiles):
        super().__init__()
        self.profiles = profiles
        self.service = ResidentService(profiles, profiles.active_id()) if profiles.active_id() else None
        self.title(f"Nomi Postmark Mail v{APP_VERSION}")
        icon = Path(__file__).resolve().parent / 'assets' / 'lamplit-mail-v1.ico'
        if icon.exists():
            self.iconbitmap(default=str(icon))
        self.geometry("1080x780")
        self.minsize(900, 650)
        self.configure(bg=BG)
        self.busy = False
        self.current = None
        self.ids = []
        self.tasks = queue.Queue()
        self.controls = []
        self.events_window = None
        self.profile_labels = {}
        self.protocol("WM_DELETE_WINDOW", self.close)

        lamplit_ui.build_mail(self)

        self.rebuild_profile_selector()
        self.refresh()
        self.after(100, self.drain)
        if not self.service:
            self.after(350, self.add_resident)
        elif not self.service.handle:
            self.status.set('Connect Fizz locally. Her Postmark handle can stay blank until registration.')
            self.after(450, self.edit_resident)
        elif self.service.profile.get("migrated_from_legacy"):
            self.status.set(f"Migrated {self.service.name} into an isolated resident profile. Existing mail state was preserved.")

    def button(self, parent, text, command, color):
        button = tk.Button(
            parent, text=text, command=command, bg=color,
            fg=BG if color in (CYAN, LILAC) else INK, relief="flat", bd=0,
            padx=12, pady=8, cursor="hand2", font=("Segoe UI", 10, "bold"),
        )
        self.controls.append(button)
        return button

    def textbox(self, parent, height):
        return tk.Text(
            parent, height=height, bg=CARD, fg=INK, insertbackground=CYAN,
            selectbackground="#575072", relief="flat", padx=15, pady=12,
            wrap="word", font=("Segoe UI", 11), undo=True,
        )

    def connected(self):
        return bool(self.service and self.service.connected())

    def rebuild_profile_selector(self):
        self.profile_labels = {}
        default = self.profiles.index.get("default_profile_id")
        selected = ""
        for profile in self.profiles.profiles():
            label = f"{'★ ' if profile['id'] == default else ''}{profile.get('nomi_name') or 'Resident'} — @{profile.get('handle') or 'needs setup'}"
            self.profile_labels[label] = profile["id"]
            if profile["id"] == self.profiles.active_id():
                selected = label
        self.resident_combo.configure(values=list(self.profile_labels))
        self.resident_choice.set(selected)
        if self.service:
            marker = "DEFAULT · " if self.service.profile_id == default else ""
            self.active_identity.set(f"{marker}{self.service.name} / @{self.service.handle or 'needs setup'}")
        else:
            self.active_identity.set("NO RESIDENT SELECTED")

    def switch_resident(self, _event=None):
        profile_id = self.profile_labels.get(self.resident_choice.get())
        if not profile_id or (self.service and profile_id == self.service.profile_id):
            return
        if self.busy:
            messagebox.showinfo("One moment", "Let the current operation finish before switching residents.", parent=self)
            self.rebuild_profile_selector()
            return
        if self.events_window and self.events_window.winfo_exists():
            messagebox.showinfo("Close Live Events", "Close the current resident's Live Events window before switching.", parent=self)
            self.rebuild_profile_selector()
            return
        self.autosave()
        self.profiles.set_active(profile_id)
        self.service = ResidentService(self.profiles, profile_id)
        self.current = None
        self.rebuild_profile_selector()
        self.refresh()
        self.status.set(f"Active resident changed to {self.service.name} / @{self.service.handle}. All mailbox and event state now comes from that profile only.")

    def work(self, label, function, done):
        if self.busy:
            return
        if self.events_window and self.events_window.winfo_exists():
            messagebox.showinfo('Close Live Events', 'Close Live Events before mailbox operations.', parent=self)
            return
        self.autosave()
        self.busy = True
        resident_id = self.service.profile_id if self.service else None
        self.status.set(label)
        for control in self.controls:
            control.configure(state="disabled")
        self.resident_combo.configure(state="disabled")
        self.listbox.configure(state='disabled')
        self.draft.configure(state='disabled')
        def run():
            try:
                value = function()
                self.tasks.put((done, value, None, resident_id))
            except Exception as error:
                self.tasks.put((done, None, str(error), resident_id))
        threading.Thread(target=run, daemon=True).start()

    def drain(self):
        try:
            while True:
                done, value, error, resident_id = self.tasks.get_nowait()
                self.busy = False
                for control in self.controls:
                    control.configure(state="normal")
                self.resident_combo.configure(state="readonly")
                self.listbox.configure(state='normal')
                if not self.service or self.service.profile_id != resident_id:
                    self.status.set("A completed operation belongs to another resident profile; its state stayed isolated there.")
                    continue
                self.refresh()
                if error:
                    entry = self.service.entries().get(self.current, {}) if self.current else {}
                    if entry.get("status") == "response_unknown":
                        self.status.set(entry.get("error", "Nomi response is uncertain. Check the normal chat."))
                    elif entry.get("status") == "needs_send_check":
                        self.status.set("Submission is uncertain. Inspect Postmark before any retry.")
                    else:
                        self.status.set(error)
                    messagebox.showerror("Needs attention", error, parent=self)
                else:
                    done(value)
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def refresh(self):
        self.listbox.delete(0, "end")
        self.ids = []
        if not self.service:
            self.detail.configure(text="Add a resident to begin")
            self._clear_text()
            return
        entries = self.service.entries()
        labels = {
            'awaiting_nomi_confirmation': 'approve Nomi',
            "pending": "held draft", "needs_attention": "continue", "response_unknown": "check Nomi",
            "rejected": "kept unsent", "submitting": "submitting", "needs_send_check": "check Postmark",
            "queued": "queued for ferry", "delivered": "delivery confirmed",
            "pr_open": "submitted", "contacting_nomi": "with Nomi", "reading": "reading",
        }
        for key, entry in sorted(entries.items(), reverse=True):
            if bool(entry.get('archived_at')) != self.show_archived.get():
                continue
            if (entry.get('status') == 'rejected' and not entry.get('archived_at')) or entry.get('cleared_from_mailbox_at'):
                continue
            self.ids.append(key)
            sender = (entry.get("fields") or {}).get("from", "unknown")
            self.listbox.insert("end", f"{sender} · {labels.get(entry.get('status'), entry.get('status'))}")
        if self.current in self.ids:
            index = self.ids.index(self.current)
            self.listbox.selection_set(index)
            self.show_current()
        elif self.ids:
            self.current = self.ids[0]
            self.listbox.selection_set(0)
            self.show_current()
        else:
            self.current = None
            self.detail.configure(text=f"{self.service.name}'s mailbox")
            self._clear_text()
        self.update_actions()

    def _clear_text(self):
        for widget in (self.incoming, self.draft):
            widget.configure(state="normal")
            widget.delete("1.0", "end")

    def select(self, _event=None):
        if self.busy:
            return
        selection = self.listbox.curselection()
        if selection:
            self.autosave()
            self.current = self.ids[selection[0]]
            self.show_current()

    def show_current(self):
        if not self.current or not self.service:
            return
        entry = self.service.entries().get(self.current, {})
        self.detail.configure(text=f"From {(entry.get('fields') or {}).get('from', 'unknown')} · {entry.get('status', 'unknown').replace('_', ' ')}")
        self.incoming.configure(state="normal")
        self.incoming.delete("1.0", "end")
        diagnostics = f"\n\nParts confirmed: {entry.get('nomi_parts_completed', 0)}/{entry.get('nomi_parts', 0)}"
        diagnostics += '\n' + entry.get('error', '')
        if entry.get('receipt'):
            diagnostics += '\nReceipt: ' + json.dumps(entry['receipt'], ensure_ascii=False, indent=2)
        self.incoming.insert("1.0", entry.get("body", ""))
        self.incoming.configure(state="disabled")
        self.draft.configure(state="normal")
        self.draft.delete("1.0", "end")
        self.draft.insert("1.0", entry.get("draft", ""))
        self.update_actions()

    def update_actions(self):
        entry = self.service.entries().get(self.current, {}) if self.service and self.current else {}
        archived = bool(entry.get('archived_at'))
        if archived:
            entry = {}
        pending = entry.get("status") == "pending"
        uncertain = entry.get("status") == "response_unknown"
        self.save_btn.configure(state="normal" if pending else "disabled")
        submitted = entry.get('status') in {'queued', 'delivered', 'pr_open'}
        self.reject_btn.configure(text='Clear submitted' if submitted else 'Keep unsent', state="normal" if pending or submitted else "disabled")
        self.send_btn.configure(state="normal" if pending else "disabled")
        awaiting = entry.get('status') == 'awaiting_nomi_confirmation'
        self.retry_btn.configure(state="normal" if awaiting else "disabled")
        self.continue_btn.configure(state="normal" if entry.get('status') == 'needs_attention' else "disabled")
        paste_safe = entry.get('status') in {'pending','awaiting_nomi_confirmation','needs_attention','response_unknown'} and not entry.get('submission')
        self.recover_btn.configure(state="normal" if paste_safe else "disabled")
        self.resend_btn.configure(state="normal" if uncertain else "disabled")
        self.pr_btn.configure(state="normal" if entry.get('submission') or entry.get("pr") else "disabled")
        self.mail_retry_btn.configure(state='normal' if entry.get('status') == 'needs_send_check' and entry.get('submission') else 'disabled')
        self.draft.configure(state='normal' if pending else 'disabled')
        can_archive = entry.get('status') in {'pending','awaiting_nomi_confirmation','needs_attention','response_unknown','rejected'} and not entry.get('submission')
        self.archive_btn.configure(text='Restore mail' if archived else 'Archive mail…', state='normal' if archived or can_archive else 'disabled')
        lamplit_ui.sync_mail(self)

    def autosave(self):
        if not self.service or not self.current:
            return
        entry = self.service.entries().get(self.current, {})
        text = self.draft.get("1.0", "end").strip()
        if not entry.get('archived_at') and entry.get("status") == "pending" and text and text != entry.get("draft", ""):
            try:
                self.service.save_draft(self.current, text)
            except Exception:
                pass

    def check_mail(self):
        if self.events_window and self.events_window.winfo_exists():
            messagebox.showinfo('Live Events open', 'Close Live Events before checking mail so the Nomi conversations do not overlap.', parent=self)
            return
        if not self.service:
            self.add_resident()
            return
        service = self.service
        if not service.connected():
            self.edit_resident()
            return
        if not service.initialized():
            choice = messagebox.askyesnocancel('First mailbox check', f'Import existing mail for {service.name} / @{service.handle}?\n\nYes = collect existing letters for individual review.\nNo = start fresh, marking existing letters seen.\nCancel = leave mailbox unchanged.\n\nNeither choice sends anything to Nomi.', parent=self)
            if choice is None:
                return
            self.work(
                f"Creating {service.name}'s independent inbox baseline…",
                lambda: service.initialize(import_existing=choice),
                lambda _value: self.status.set('Mailbox initialized. Existing mail is waiting for review; nothing sent to Nomi.' if choice else 'Mailbox initialized. Existing letters marked seen for this resident only.'),
            )
            return
        self.work(
            f"Checking mail for {service.name} / @{service.handle}…",
            service.poll,
            lambda _value: self.status.set("Mail checked. Select an incoming letter and use Send to Nomi… to review it. Nothing was sent to Nomi."),
        )

    def test_nomi(self):
        service = self.service
        if not service or not service.config.get('nomi_id') or not service.nomi_key():
            self.edit_resident()
            return
        if self.busy or (self.events_window and self.events_window.winfo_exists()):
            return
        store = mail_pipeline.AtomicState(service.directory / 'connection-test.json')
        if store.exists() and store.read().get('status') == 'response_unknown':
            messagebox.showinfo('Check the normal Nomi chat', 'The previous connection test may have arrived. Inspect the normal chat. This test will not be resent automatically.', parent=self)
            return
        message = ('Local Postmark mail-app connection test. Read as correspondence; your human reviews before mailing.\n\n'
                   'Hello Fizz! This is a local Postmark mail-app connection test. Please write a short reply in your own words.')
        if not confirm_words(self, 'Approve one live Nomi connection test',
            f'NOMI: {service.name}\nUUID: {service.config["nomi_id"]}', message,
            'This sends one visible message to this Nomi’s normal chat and saves her reply locally. It does not submit public mail.'):
            return
        def run():
            # Unknown is persisted BEFORE sending, also covering forced shutdown.
            store.write({'status':'response_unknown', 'message':message, 'nomi_id':service.config['nomi_id']})
            reply, reply_id = service._send_nomi(message)
            store.write({'status':'held', 'message':message, 'reply':reply, 'reply_id':reply_id, 'nomi_id':service.config['nomi_id']})
            return reply
        def done(reply):
            confirm_words(self, 'Fizz’s test reply — saved locally', f'NOMI: {service.name}', reply,
                  'This is a private test result. Neither button submits mail.', approval_label='Close result')
            self.status.set('Connection test complete. Reply saved locally; no public mail submitted.')
        self.work('Sending your approved Nomi connection test…', run, done)

    def save(self):
        if not self.current:
            return
        try:
            self.service.save_draft(self.current, self.draft.get("1.0", "end"))
            self.status.set("Edits saved in this resident's isolated mailbox. Nothing was mailed.")
            self.refresh()
        except Exception as error:
            messagebox.showerror("Could not save", str(error), parent=self)

    def retry_letter(self):
        if not self.current:
            return
        key, service = self.current, self.service
        entry = service.entries()[key]
        if entry.get('status') == 'needs_attention' and not entry.get('nomi_messages'):
            self.work('Reading letter for review…', lambda: service.pipeline().prepare_delivery(key),
                lambda _value: self.status.set('Letter prepared for review. Select Send to Nomi…; nothing was sent.'))
            return
        if entry.get('status') not in {'awaiting_nomi_confirmation', 'needs_attention'}:
            return
        next_part = None
        if entry['status'] == 'needs_attention':
            next_part = self.choose_resume_part(key)
            if next_part is None:
                return
        if not self.nomi_delivery_confirmed(key, next_part=next_part):
            self.status.set('Left waiting for approval. Nothing new was sent to Nomi.')
            return
        operation = service.pipeline().confirm_delivery if entry['status'] == 'awaiting_nomi_confirmation' else lambda selected: service.pipeline().continue_after_definite_failure(selected, next_part=next_part, human_checked_chat=True)
        self.work('Sending approved letter to Nomi…', lambda: operation(key), lambda _value: self.status.set('Letter completed. Draft held for review.'))

    def nomi_delivery_confirmed(self, key, *, next_part=None):
        service = self.service
        entry = service.entries()[key]
        fields = entry.get('fields') or {}
        completed = int(entry.get('nomi_parts_completed', 0))
        total = int(entry.get('nomi_parts', 0))
        first = next_part or completed + 1
        action = f'NEXT: part {first} through {total}' if first <= total else 'NEXT: no send; paste final reply'
        return confirm_words(self, 'Approve incoming letter to Nomi',
            f'RESIDENT: {service.name} / @{service.handle}\nNOMI UUID: {service.config.get("nomi_id", "")}\n'
            f'FROM: {fields.get("from", "unknown")}\nTITLE: {fields.get("title", "Untitled")}\n'
            f'PARTS: {completed} confirmed / {total} total\n{action}', entry.get('body', ''),
            'This sends the remaining letter parts to this Nomi’s normal chat. Her reply will be held locally; no public mail is submitted.')

    def recover_reply(self):
        if not self.current:
            return
        dialog = tk.Toplevel(self)
        dialog.title('Paste reply from the normal Nomi chat')
        dialog.geometry('650x460')
        dialog.transient(self)
        dialog.grab_set()
        box = tk.Text(dialog, height=1, wrap='word', padx=12, pady=12)
        box.pack(fill='both', expand=True, padx=15, pady=15)
        key, service = self.current, self.service
        def save():
            try:
                service.recover_reply(key, box.get('1.0', 'end'))
                dialog.destroy()
                self.refresh()
                self.status.set('Recovered reply held for review. Nothing was resent.')
            except Exception as error:
                messagebox.showerror('Could not recover', str(error), parent=dialog)
        def paste_clipboard():
            try:
                box.insert('insert', dialog.clipboard_get())
            except tk.TclError:
                messagebox.showinfo('Clipboard empty', 'Copy the Nomi reply first.', parent=dialog)
        tk.Button(dialog, text='Paste from clipboard', command=paste_clipboard).pack(side='left', padx=15, pady=(0,15))
        tk.Button(dialog, text='Hold this reply for review', command=save).pack(side='right', padx=15, pady=(0, 15))

    def resend_after_timeout(self):
        if not self.current:
            return
        key, service = self.current, self.service
        next_part = self.choose_resume_part(key, uncertain=True)
        if next_part is None:
            return
        if not self.nomi_delivery_confirmed(key, next_part=next_part):
            return
        self.work("Recovering from the checked checkpoint…", lambda: service.pipeline().resend_after_human_check(key, human_checked_chat=True, next_part=next_part), lambda _value: self.status.set('Recovery saved. Paste final reply if all parts were already present. Nothing is mailed.'))

    def choose_resume_part(self, key, *, uncertain=False):
        entry = self.service.entries()[key]
        saved, total = int(entry.get('nomi_parts_completed', 0)), int(entry.get('nomi_parts', 0))
        prompt = f'Inspect {self.service.name}’s normal chat first. Saved confirmed parts: {saved}/{total}.\n\nEnter the next part to send ({saved + 1}–{total + 1}).\nIf a possibly-sent part is missing, choose it. If it visibly arrived, choose the following part.\n{total + 1} means all parts arrived: send nothing and paste the final reply. Checkpoints cannot move backward.'
        selected = simpledialog.askinteger('Checked-chat recovery', prompt, parent=self, initialvalue=saved+1, minvalue=saved+1, maxvalue=total+1)
        if selected is None:
            return None
        if (uncertain or selected > saved+1) and not messagebox.askyesno('Confirm normal chat inspected', f'You checked the normal chat and verified parts through {selected-1} arrived, and part {selected} is missing (or all parts arrived if beyond the final part)?\n\nOnly confirm after checking.', parent=self):
            return None
        return selected

    def archive_mail(self):
        if self.busy or not self.current:
            return
        key, service = self.current, self.service
        entry = service.entries()[key]
        if entry.get('archived_at'):
            service.pipeline().restore(key)
            self.show_archived.set(False)
        else:
            if not messagebox.askyesno('Archive without replying?', f'Archive this letter from {(entry.get("fields") or {}).get("from", "unknown")} for {service.name}?\n\nIt stays saved with its original status and duplicate protection. No Nomi message or public reply is sent. A previous uncertain send may already have reached Nomi; archiving does not undo it.', parent=self):
                return
            self.autosave()
            service.pipeline().archive(key)
        self.refresh()
        self.status.set('Archive updated locally. Nothing was sent or deleted.')

    def reject(self):
        if self.current and self.service.entries()[self.current].get('status') in {'queued', 'delivered', 'pr_open'}:
            if messagebox.askyesno('Clear submitted letter?', 'Hide this letter from the active mailbox? Its exact submission, receipt, history and duplicate protection will remain saved. Queued mail is not cancelled or confirmed delivered by clearing it.', parent=self):
                self.service.pipeline().clear_submitted(self.current)
                self.status.set('Cleared from active mailbox. Submission history preserved; nothing was sent.')
                self.refresh()
            return
        if self.current and messagebox.askyesno("Keep unsent?", "Keep this reply unsent?", parent=self):
            self.autosave()
            self.service.reject(self.current)
            self.status.set("Kept unsent and hidden from active mailbox. History preserved; nothing was mailed.")
            self.refresh()

    def send(self):
        if not self.current:
            return
        text = self.draft.get("1.0", "end").strip()
        entry = self.service.entries()[self.current]
        recipient = (entry.get("fields") or {}).get("from", "unknown")
        if not text:
            messagebox.showinfo("Empty reply", "Write a reply before approving it.", parent=self)
            return
        if not confirm_words(self, 'Approve exact public reply',
            f'FROM: {self.service.name} / @{self.service.handle}\nTO: @{recipient}', text,
            'This queues public mail through Postmark. Delivery is confirmed separately after the ferry crossing.'):
            return
        key = self.current
        self.work("Submitting the approved reply…", lambda: self.service.send_approved(key, text), lambda _receipt: self.status.set("Accepted and queued for the ferry. Delivery is not yet confirmed."))

    def open_pr(self):
        if self.current:
            key, service = self.current, self.service
            if service.entries()[key].get('pr'):
                webbrowser.open(service.entries()[key]['pr'])
                return
            self.work('Reading delivery status…', lambda: postmark_client.check_submission(service.pipeline(), key, service.postmark_key()),
                lambda entry: self.status.set('Delivery confirmed.' if entry['status'] == 'delivered' else 'No delivery confirmation yet. Nothing was resent.'))

    def retry_submission(self):
        key, service = self.current, self.service
        payload = service.entries()[key]['submission']
        if messagebox.askyesno('Recover uncertain public mail?',
            f"Check your Postmark outbox first. This repeats the saved request with its original nonce. Nonce protection may expire after a crossing; if unsure, choose No.\n\nFROM: {payload['from']}\nTO: {payload['to']}\n\n{payload['body']}\n\nYou checked Postmark and approve this exact recovery request?", parent=self):
            self.work('Recovering saved submission…', lambda: postmark_client.retry_uncertain(service.pipeline(), key, service.postmark_key(), human_checked=True),
                lambda receipt: self.status.set('Acceptance receipt recovered. Queued; delivery not yet confirmed.'))

    def open_home(self):
        if self.service and self.service.handle:
            webbrowser.open(f"https://postmark.town/residents/{self.service.handle}/")

    def open_live_events(self):
        if self.busy:
            return
        if not self.service:
            self.add_resident()
            return
        if self.events_window and self.events_window.winfo_exists():
            self.events_window.lift()
            return
        self.events_window = LiveEventsWindow(self, self.service)

    def add_resident(self):
        self.profile_dialog(None)

    def edit_resident(self):
        if not self.service:
            self.add_resident()
            return
        self.profile_dialog(self.service.profile_id)

    def profile_dialog(self, profile_id):
        if self.busy or (self.events_window and self.events_window.winfo_exists()):
            messagebox.showinfo('Resident in use', 'Finish mail work and close Live Events before editing profiles.', parent=self)
            return
        editing = profile_id is not None
        current_settings = self.profiles.read_settings(profile_id) if editing else {}
        current_profile = self.profiles.profile(profile_id) if editing else {}
        state_exists = editing and self.profiles.mail_state_path(profile_id).exists()
        dialog = tk.Toplevel(self)
        dialog.title("Edit Resident" if editing else "Add Resident")
        dialog.geometry("650x740")
        dialog.minsize(620,720)
        dialog.configure(bg=CARD)
        dialog.transient(self)
        dialog.grab_set()
        body = tk.Frame(dialog, bg=CARD)
        body.pack(fill="both", expand=True, padx=25, pady=22)
        tk.Label(body, text="Resident profile", font=("Segoe UI", 17, "bold"), fg=CYAN, bg=CARD).pack(anchor="w")
        tk.Label(body, text="Each profile has its own mailbox, recovery checkpoints, drafts, outgoing locks, and Live Events cursor.", wraplength=530, justify="left", font=("Segoe UI", 9), fg=MUTED, bg=CARD).pack(anchor="w", pady=(7, 12))

        def field(label, variable, secret=False):
            tk.Label(body, text=label, font=("Segoe UI", 9, "bold"), fg=MUTED, bg=CARD).pack(anchor="w", pady=(7, 3))
            widget = tk.Entry(body, textvariable=variable, show="•" if secret else "", bg=BG, fg=INK, insertbackground=CYAN, relief="flat", font=("Segoe UI", 10))
            widget.pack(fill="x", ipady=7)
            return widget

        handle = tk.StringVar(value=current_profile.get("handle", ""))
        handle_widget = field("Postmark resident handle (leave blank until registered)", handle)
        if state_exists and handle.get():
            handle_widget.configure(state="disabled")
            tk.Label(body, text="Handle is locked after this resident's inbox baseline exists.", font=("Segoe UI", 8), fg=LILAC, bg=CARD).pack(anchor="w")
        nomi_key = tk.StringVar(value=read_secret(current_settings, "encrypted_key"))
        field("Nomi API key", nomi_key, True)
        selected_nomi = tk.StringVar()
        choices = {}
        existing_nomi_id = current_settings.get("nomi_id")
        existing_name = current_profile.get("nomi_name") or current_settings.get("nomi_name")
        if existing_nomi_id:
            label = f"{existing_name or 'Nomi'} · {str(existing_nomi_id)[:8]}"
            choices[label] = existing_nomi_id
            selected_nomi.set(label)
        tk.Label(body, text="Nomi", font=("Segoe UI", 9, "bold"), fg=MUTED, bg=CARD).pack(anchor="w", pady=(7, 3))
        nomi_combo = ttk.Combobox(body, textvariable=selected_nomi, values=list(choices), state="readonly")
        nomi_combo.pack(fill="x", ipady=5)
        postmark_key = tk.StringVar(value=read_secret(current_settings, "encrypted_postmark_key"))
        field("Postmark household key (for approved mail and optional Live Events)", postmark_key, True)
        if current_settings.get('encrypted_postmark_key'):
            tk.Label(body, text='Household key saved. Leaving the field blank keeps it.', font=('Segoe UI',8), fg=LILAC, bg=CARD).pack(anchor='w')
        options = tk.Frame(body, bg=CARD)
        options.pack(fill="x", pady=10)
        tk.Label(options, text="Mail · Nomi input limit", font=("Segoe UI", 10, "bold"), fg=MUTED, bg=CARD).pack(side="left")
        limit = tk.StringVar(value=str(current_settings.get("input_limit", 400)))
        ttk.Combobox(options, textvariable=limit, values=["400", "800"], state="readonly", width=6).pack(side="left", padx=10)
        tk.Label(options, text="800 requires a qualifying Nomi plan", font=("Segoe UI", 8), fg=MUTED, bg=CARD).pack(side="left")
        live_options = tk.Frame(body, bg=CARD)
        live_options.pack(fill='x', pady=4)
        tk.Label(live_options, text='Live · Nomi input limit', font=('Segoe UI',10,'bold'), fg=MUTED, bg=CARD).pack(side='left')
        live_limit = tk.StringVar(value=str(current_settings.get('live_input_limit', current_settings.get('input_limit',400))))
        ttk.Combobox(live_options, textvariable=live_limit, values=['400','800'], state='readonly', width=6).pack(side='left', padx=10)
        make_default = tk.BooleanVar(value=editing and self.profiles.index.get("default_profile_id") == profile_id)
        ttk.Checkbutton(body, text="Use this resident by default on startup", variable=make_default).pack(anchor="w", pady=4)
        notice = tk.StringVar(value="Load Nomis after entering the Nomi key, then select the correct one.")
        tk.Label(body, textvariable=notice, wraplength=530, justify="left", font=("Segoe UI", 9), fg=LILAC, bg=CARD).pack(anchor="w", pady=9)
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=(4, 0))

        def load_nomis():
            api_key = nomi_key.get().strip()
            if not api_key:
                notice.set("Enter this resident's Nomi API key first.")
                return
            load_btn.configure(state="disabled")
            local = queue.Queue()
            def request():
                try:
                    result = mail_pipeline.request_json(
                        f"{nomi_client.NOMI_API}/nomis",
                        headers={"Authorization": api_key}, timeout=30,
                    )
                    local.put((result.get("nomis", []), None))
                except Exception as error:
                    local.put((None, str(error)))
            def check():
                try:
                    nomis, error = local.get_nowait()
                except queue.Empty:
                    dialog.after(100, check)
                    return
                load_btn.configure(state="normal")
                if error:
                    notice.set(error)
                    return
                choices.clear()
                for nomi in nomis:
                    label = f"{nomi.get('name', 'Nomi')} · {nomi['uuid'][:8]}"
                    choices[label] = nomi["uuid"]
                nomi_combo.configure(values=list(choices))
                if len(choices) == 1:
                    selected_nomi.set(next(iter(choices)))
                else:
                    selected_nomi.set('')
                notice.set("Select the Nomi that belongs to this Postmark resident.")
            threading.Thread(target=request, daemon=True).start()
            dialog.after(100, check)

        def save_profile():
            chosen_handle = handle.get().strip()
            chosen_label = selected_nomi.get()
            if chosen_handle and not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', chosen_handle):
                notice.set('Postmark handles use lowercase letters, numbers, and hyphens.')
                return
            if chosen_label not in choices:
                notice.set("Load and select this resident's Nomi.")
                return
            if not nomi_key.get().strip() and not current_settings.get('encrypted_key'):
                notice.set("Enter the Nomi API key.")
                return
            settings = {
                "resident_handle": chosen_handle,
                "nomi_id": choices[chosen_label],
                "nomi_name": chosen_label.split(" · ", 1)[0],
                "input_limit": int(limit.get()),
                "live_input_limit": int(live_limit.get()),
                "nomi_reply_timeout": int(current_settings.get("nomi_reply_timeout", 180)),
                "part_pause": float(current_settings.get("part_pause", 4)),
                "encrypted_key": crypt_key(nomi_key.get().strip()) if nomi_key.get().strip() else current_settings.get('encrypted_key'),
            }
            if postmark_key.get().strip():
                settings["encrypted_postmark_key"] = crypt_key(postmark_key.get().strip())
            try:
                if editing:
                    self.profiles.update(
                        profile_id, handle=chosen_handle,
                        nomi_name=settings["nomi_name"], settings=settings,
                        make_default=make_default.get(),
                    )
                    chosen_id = profile_id
                else:
                    chosen_id = self.profiles.add(
                        handle=chosen_handle, nomi_name=settings["nomi_name"],
                        settings=settings, make_default=make_default.get(),
                    )
                self.profiles.set_active(chosen_id)
                self.service = ResidentService(self.profiles, chosen_id)
                if editing and self.service.initialized() and int(settings['input_limit']) != int(current_settings.get('input_limit',400)):
                    self.service.pipeline().repack_unstarted()
                self.current = None
                self.rebuild_profile_selector()
                self.refresh()
                dialog.destroy()
                self.status.set(f"Resident profile saved: {self.service.name} / @{self.service.handle}. Its state is isolated from every other resident.")
            except Exception as error:
                notice.set(str(error))

        load_btn = tk.Button(row, text="Load my Nomis", command=load_nomis, bg=CYAN, fg=BG, relief="flat", padx=12, pady=9)
        load_btn.pack(side="left")
        tk.Button(row, text="Save Resident", command=save_profile, bg=LILAC, fg=BG, relief="flat", padx=12, pady=9).pack(side="right")

    def remove_resident(self):
        if not self.service:
            return
        if self.busy or (self.events_window and self.events_window.winfo_exists()):
            messagebox.showinfo("Resident in use", "Finish active work and close Live Events before removing a resident.", parent=self)
            return
        answer = messagebox.askyesnocancel(
            "Remove resident",
            f"Remove {self.service.name} / @{self.service.handle} from the app?\n\n"
            "Yes = remove profile AND permanently delete only this resident's local state.\n"
            "No = remove profile but archive this resident's local state for recovery.\n"
            "Cancel = do nothing.",
            parent=self,
        )
        if answer is None:
            return
        name = self.service.name
        archived = self.profiles.remove(self.service.profile_id, delete_state=bool(answer))
        active = self.profiles.active_id()
        self.service = ResidentService(self.profiles, active) if active else None
        self.current = None
        self.rebuild_profile_selector()
        self.refresh()
        self.status.set(
            f"Removed {name}. " + ("Its resident directory was deleted." if answer else f"Its local state was archived at {archived}.")
        )
        if not self.service:
            self.after(200, self.add_resident)

    def set_default(self):
        if not self.service:
            return
        self.profiles.set_default(self.service.profile_id)
        self.rebuild_profile_selector()
        self.status.set(f"{self.service.name} is now the default resident on startup.")

    def close(self):
        if self.busy:
            messagebox.showinfo("One moment", "Let the current mail operation finish before closing.", parent=self)
            return
        self.autosave()
        if self.events_window and self.events_window.winfo_exists():
            self.events_window.close()
            if self.events_window.winfo_exists():
                return
        self.destroy()


def prepare_data_root():
    """Fizz's installation never searches for or imports Rowan's records."""
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA


def lock_app(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / "app.lock", "a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    return handle


if __name__ == "__main__":
    try:
        data_root = prepare_data_root()
        lock = lock_app(data_root)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Postmark ↔ Nomi Companion", "The app is already open. Look for its window on the taskbar.")
        sys.exit(0)
    try:
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w")
        profiles = ProfileStore(data_root)
        if not profiles.profiles():
            profiles.add(handle='', nomi_name='Fizz', settings={'nomi_name': 'Fizz', 'input_limit': 400}, make_default=True)
        profiles.activate_default_on_startup()
        App(profiles).mainloop()
    except Exception as error:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Postmark ↔ Nomi Companion", str(error))
