"""Tk integration tests; every network entry point is forbidden."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from profile_store import ProfileStore
from mail_pipeline import AtomicState

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('windows_companion', str(ROOT/'Companion App.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
companion = importlib.util.module_from_spec(spec)
loader.exec_module(companion)


class WindowsInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.profiles = ProfileStore(self.temp.name)
        self.first = self.profiles.add(handle='fizz-test', nomi_name='Fizz', settings={'nomi_id':'uuid-fizz'})
        self.second = self.profiles.add(handle='second-test', nomi_name='Second', settings={'nomi_id':'uuid-second'})
        for profile_id, handle, draft in [(self.first, 'fizz-test', 'Fizz original'), (self.second, 'second-test', 'Second original')]:
            AtomicState(self.profiles.mail_state_path(profile_id)).write({'seen':['same-key'], 'letters':{'same-key':
                {'status':'pending', 'fields':{'from':'friend','to':handle,'id':'incoming'}, 'body':'Incoming', 'draft':draft}}})
        self.profiles.set_active(self.first)
        self.block = patch('urllib.request.urlopen', side_effect=AssertionError('UI tests must never contact a live API'))
        self.block.start()
        self.app = companion.App(self.profiles)
        self.app.update()

    def tearDown(self):
        for callback in self.app.tk.call('after', 'info'):
            self.app.after_cancel(callback)
        self.app.destroy()
        self.block.stop()
        self.temp.cleanup()

    def test_edit_and_switch_preserves_exact_resident_drafts(self):
        self.app.draft.delete('1.0', 'end')
        self.app.draft.insert('1.0', 'Fizz edited\nTwo lines')
        self.app.autosave()
        label = next(label for label, profile_id in self.app.profile_labels.items() if profile_id == self.second)
        self.app.resident_choice.set(label)
        self.app.switch_resident()
        self.assertEqual(self.app.draft.get('1.0','end').strip(), 'Second original')
        self.assertEqual(AtomicState(self.profiles.mail_state_path(self.first)).read()['letters']['same-key']['draft'], 'Fizz edited\nTwo lines')

    def test_queued_reply_is_locked_and_shows_receipt_controls(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state = store.read()
        state['letters']['same-key'].update(status='queued', receipt={'letter_id':'queued-id'}, submission={'body':'Fizz original'})
        store.write(state)
        self.app.refresh()
        self.assertEqual(str(self.app.send_btn['state']), 'disabled')
        self.assertEqual(str(self.app.draft['state']), 'disabled')
        self.assertEqual(str(self.app.pr_btn['state']), 'normal')

    def test_permanent_send_button_is_visible_and_unknown_cannot_continue(self):
        self.assertEqual(self.app.title(), 'Nomi Postmark Mail v2.0')
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state = store.read()
        state['letters']['same-key']['status'] = 'response_unknown'
        store.write(state)
        self.app.refresh()
        self.app.update()
        self.assertEqual(self.app.retry_btn['text'], 'Send to Nomi…')
        self.assertTrue(self.app.retry_btn.winfo_ismapped())
        self.assertEqual(str(self.app.retry_btn['state']), 'disabled')
        self.assertEqual(str(self.app.continue_btn['state']), 'disabled')
        state['letters']['same-key']['status'] = 'awaiting_nomi_confirmation'
        store.write(state)
        self.app.refresh()
        self.assertEqual(str(self.app.retry_btn['state']), 'normal')
        self.assertEqual(str(self.app.continue_btn['state']), 'disabled')

    def test_live_events_off_and_cursor_checkpoint_is_resident_specific(self):
        service = self.app.service
        service.event_checkpoint({'stage':'speech_unknown', 'nonce':'saved'})
        service.save_event_cursor(123)
        self.assertEqual(service.event_checkpoint()['nonce'], 'saved')
        second = companion.ResidentService(self.profiles, self.second)
        self.assertIsNone(second.event_cursor())
        self.assertIsNone(second.event_checkpoint())
        with patch.object(companion.LiveEventsWindow, 'refresh_calendar'):
            window = companion.LiveEventsWindow(self.app, service)
            self.assertFalse(window.running)
            window.close()

    def test_empty_fizz_profile_needs_no_registered_handle_or_keys(self):
        third = self.profiles.add(handle='', nomi_name='Fizz future', settings={'nomi_name':'Fizz future'})
        service = companion.ResidentService(self.profiles, third)
        self.assertFalse(service.connected())
        self.assertFalse(service.initialized())

    def test_recovery_controls_fit_default_and_minimum_window(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state = store.read()
        state['letters']['same-key'].update(status='needs_send_check',submission={'body':'synthetic pending request'})
        store.write(state)
        self.app.refresh()
        for geometry in ['1080x780', '900x650']:
            self.app.geometry(geometry)
            self.app.update()
            button = self.app.mail_retry_btn
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.app.winfo_rooty() + self.app.winfo_height())
            self.assertLessEqual(self.app.archive_btn.winfo_rootx()+self.app.archive_btn.winfo_width(), self.app.winfo_rootx()+self.app.winfo_width())

    def test_lamplit_letter_is_readable_and_diagnostics_are_separate(self):
        self.assertEqual(self.app.incoming['bg'],'#f3ead9')
        self.assertEqual(self.app.incoming.get('1.0','end-1c'),'Incoming')
        self.assertNotIn('Parts confirmed',self.app.incoming.get('1.0','end'))
        self.assertTrue(self.app.draft_section.winfo_ismapped())
        self.assertEqual(str(self.app.send_btn['state']),'normal')

    def test_contextual_recovery_does_not_hide_a_required_action(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        for status, visible in [('response_unknown','resend_btn'),('needs_attention','continue_btn'),('needs_send_check','mail_retry_btn')]:
            state=store.read()
            state['letters']['same-key'].update(status=status)
            if status=='needs_send_check':
                state['letters']['same-key']['submission']={'body':'synthetic'}
            store.write(state)
            self.app.refresh()
            self.app.update()
            self.assertTrue(getattr(self.app,visible).winfo_ismapped())
            self.assertEqual(str(self.app.send_btn['state']),'disabled')

    def test_empty_draft_hides_editor_but_send_to_nomi_stays_visible(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state=store.read()
        state['letters']['same-key'].update(status='awaiting_nomi_confirmation',draft='')
        store.write(state)
        self.app.refresh()
        self.app.update()
        self.assertFalse(self.app.draft_section.winfo_ismapped())
        self.assertTrue(self.app.retry_btn.winfo_ismapped())
        self.assertEqual(str(self.app.retry_btn['state']),'normal')

    def test_mailbox_card_selection_preserves_controller_contract_and_busy_lock(self):
        store=AtomicState(self.profiles.mail_state_path(self.first))
        state=store.read()
        state['seen'].append('second-letter')
        state['letters']['second-letter']={'status':'awaiting_nomi_confirmation','fields':{'from':'second-sender','to':'fizz-test'},'body':'Second exact letter'}
        store.write(state)
        self.app.refresh()
        index=self.app.ids.index('second-letter')
        self.app.listbox.selection_set(index)
        self.app.listbox.event_generate('<<ListboxSelect>>')
        self.app.update()
        self.assertEqual(self.app.current,'second-letter')
        self.assertEqual(self.app.incoming.get('1.0','end-1c'),'Second exact letter')
        self.app.listbox.configure(state='disabled')
        self.assertTrue(self.app.listbox.instate(['disabled']))
        self.app.listbox.configure(state='normal')
        self.assertFalse(self.app.listbox.instate(['disabled']))

    def test_archive_unanswered_and_restore_do_not_change_other_resident(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state = store.read()
        state['letters']['same-key']['status'] = 'awaiting_nomi_confirmation'
        store.write(state)
        self.app.refresh()
        with patch.object(companion.messagebox, 'askyesno', return_value=True):
            self.app.archive_mail()
        self.assertEqual(self.app.ids, [])
        self.app.show_archived.set(True)
        self.app.refresh()
        self.assertEqual(self.app.ids, ['same-key'])
        self.assertEqual(self.app.archive_btn['text'], 'Restore mail')
        self.assertEqual(str(self.app.retry_btn['state']), 'disabled')
        self.app.archive_mail()
        self.assertEqual(self.app.ids, ['same-key'])
        self.assertEqual(store.read()['letters']['same-key']['status'], 'awaiting_nomi_confirmation')
        self.assertEqual(AtomicState(self.profiles.mail_state_path(self.second)).read()['letters']['same-key']['draft'], 'Second original')

    def test_paste_is_enabled_for_safe_inbound_states_only(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        for status in ['pending','awaiting_nomi_confirmation','needs_attention','response_unknown','queued','needs_send_check']:
            state = store.read()
            state['letters']['same-key']['status'] = status
            store.write(state)
            self.app.refresh()
            expected = 'disabled' if status in {'queued','needs_send_check'} else 'normal'
            self.assertEqual(str(self.app.recover_btn['state']), expected)

    def test_first_run_import_choice_and_cancellation_do_not_send(self):
        third = self.profiles.add(handle='fresh-resident', nomi_name='Fresh', settings={'nomi_id':'uuid-fresh'})
        self.app.service = companion.ResidentService(self.profiles, third)
        service = self.app.service
        with patch.object(service, 'connected', return_value=True), patch.object(companion.messagebox, 'askyesnocancel', return_value=None), patch.object(self.app, 'work') as work:
            self.app.check_mail()
            work.assert_not_called()
        with patch.object(service, 'connected', return_value=True), patch.object(companion.messagebox, 'askyesnocancel', return_value=True), patch.object(self.app, 'work') as work, patch.object(service, 'initialize') as initialize:
            self.app.check_mail()
            work.call_args.args[1]()
            initialize.assert_called_once_with(import_existing=True)

    def test_cancelled_live_test_does_not_send_or_create_checkpoint(self):
        service = self.app.service
        with patch.object(service, 'nomi_key', return_value='fake-key'), patch.object(companion, 'confirm_words', return_value=False), patch.object(service, '_send_nomi') as send:
            self.app.test_nomi()
            send.assert_not_called()
        self.assertFalse((service.directory / 'connection-test.json').exists())

    def test_exact_review_buttons_fit_and_cancelling_sends_nothing(self):
        visibility = []
        def inspect_and_cancel():
            dialog = next(w for w in self.app.winfo_children() if isinstance(w, companion.tk.Toplevel))
            dialog.update_idletasks()
            row = dialog.winfo_children()[-1]
            for button in row.winfo_children():
                visibility.append(button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height())
            dialog.destroy()
        self.app.after(100, inspect_and_cancel)
        self.assertFalse(companion.confirm_words(self.app, 'Review test', 'Fizz / friend', 'Exact text\n' * 100, 'Test only.'))
        self.assertEqual(visibility, [True, True])

    def test_incoming_and_continuation_decline_do_not_start_work(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        for status in ['awaiting_nomi_confirmation', 'needs_attention']:
            state = store.read()
            state['letters']['same-key'].update(status=status, nomi_messages=['original packet'], nomi_parts=1)
            store.write(state)
            self.app.refresh()
            with patch.object(self.app, 'choose_resume_part', return_value=1), patch.object(companion, 'confirm_words', return_value=False) as review, patch.object(self.app, 'work') as work:
                self.app.retry_letter()
                work.assert_not_called()
                self.assertIn('uuid-fizz', review.call_args.args[2])
                self.assertEqual(review.call_args.args[3], 'Incoming')
            self.assertEqual(store.read()['letters']['same-key']['status'], status)

    def test_clear_queued_keeps_exact_history_and_other_resident(self):
        store = AtomicState(self.profiles.mail_state_path(self.first))
        state = store.read()
        state['letters']['same-key'].update(status='queued', receipt={'letter_id':'queued-id'}, submission={'body':'exact words','nonce':'original'})
        store.write(state)
        self.app.refresh()
        self.assertEqual(self.app.reject_btn['text'], 'Clear submitted')
        with patch.object(companion.messagebox, 'askyesno', return_value=True):
            self.app.reject()
        self.assertEqual(self.app.ids, [])
        saved = store.read()
        self.assertEqual(saved['letters']['same-key']['submission'], state['letters']['same-key']['submission'])
        self.assertEqual(saved['letters']['same-key']['status'], 'queued')
        self.assertIn('same-key', saved['seen'])
        self.assertEqual(AtomicState(self.profiles.mail_state_path(self.second)).read()['letters']['same-key']['draft'], 'Second original')

    def test_keep_unsent_saves_edits_and_hides_without_deleting(self):
        self.app.draft.delete('1.0', 'end')
        self.app.draft.insert('1.0', 'Last edit')
        with patch.object(companion.messagebox, 'askyesno', return_value=True):
            self.app.reject()
        self.assertEqual(self.app.ids, [])
        saved = AtomicState(self.profiles.mail_state_path(self.first)).read()
        self.assertEqual(saved['letters']['same-key']['draft'], 'Last edit')
        self.assertEqual(saved['letters']['same-key']['status'], 'rejected')
        self.assertIn('same-key', saved['seen'])


if __name__ == '__main__': unittest.main()
