"""Upgrade regressions, using synthetic residents and forbidden live transport."""
import copy
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
from profile_store import ProfileStore
from mail_pipeline import AtomicState, MailPipeline
from test_windows_ui import companion as app

ROOT = Path(__file__).resolve().parents[1]


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.profiles = ProfileStore(self.temp.name)
        self.first = self.profiles.add(handle='resident-one',nomi_name='One',settings={'nomi_id':'synthetic-id','input_limit':400})
        self.second = self.profiles.add(handle='resident-two',nomi_name='Two',settings={'input_limit':800})
        self.service = app.ResidentService(self.profiles,self.first)
        self.block = patch('urllib.request.urlopen',side_effect=AssertionError('No live transport'))
        self.block.start()

    def tearDown(self):
        self.block.stop()
        self.temp.cleanup()

    def test_existing_limit_is_not_silently_promoted_to_800(self):
        self.assertEqual(self.service.live_input_limit,400)
        self.profiles.write_settings(self.first,{'live_input_limit':800})
        reloaded = app.ResidentService(self.profiles,self.first)
        self.assertEqual(reloaded.input_limit,400)
        self.assertEqual(reloaded.live_input_limit,800)

    def test_blank_and_omitted_keys_preserve_ciphertext_and_options(self):
        self.profiles.write_settings(self.first,{'encrypted_key':'synthetic-cipher-one','encrypted_postmark_key':'synthetic-cipher-two','custom_option':'keep'})
        self.profiles.write_settings(self.first,{'encrypted_key':'','encrypted_postmark_key':None,'live_input_limit':800})
        saved = self.profiles.read_settings(self.first)
        self.assertEqual(saved['encrypted_key'],'synthetic-cipher-one')
        self.assertEqual(saved['encrypted_postmark_key'],'synthetic-cipher-two')
        self.assertEqual(saved['custom_option'],'keep')
        self.assertEqual(saved['input_limit'],400)

    def test_partial_settings_cannot_unlock_resident_pair(self):
        AtomicState(self.service.state_path).write({'seen':[],'letters':{}})
        self.profiles.update(self.first,nomi_name='One',settings={'live_input_limit':800})
        self.assertEqual(self.profiles.read_settings(self.first)['nomi_id'],'synthetic-id')
        with self.assertRaises(ValueError):
            self.profiles.update(self.first,nomi_name='One',settings={'nomi_id':'other-id'})

    def test_legacy_public_unknown_overrides_an_older_confirmed_speech(self):
        saved = {'latest':50,'in_flight':{'stage':'speech_unknown','speech':'unresolved','nonce':'new-unknown'},
            'speech':{'status':'confirmed','text':'older','nonce':'older-confirmed'}}
        store = AtomicState(self.service.event_state_path)
        store.write(saved)
        projected = self.service.live_state()
        self.assertEqual(projected['speech']['status'],'unknown')
        self.assertEqual(projected['speech']['nonce'],'new-unknown')
        self.assertEqual(store.read(),saved)  # reading itself never migrates/reset records
        with self.assertRaises(ValueError): self.service.recover_live_reply('PASS')

    def test_legacy_nomi_checkpoint_is_recovered_without_resend_and_keeps_cursor(self):
        store = AtomicState(self.service.event_state_path)
        store.write({'latest':50,'in_flight':{'stage':'nomi_unknown','packet':'original synthetic packet'}})
        request = self.service.live_state()['nomi']
        self.assertEqual(request['status'],'response_unknown')
        self.assertEqual(request['packets'],['original synthetic packet'])
        self.service.recover_live_reply('PASS')
        self.assertEqual(self.service.live_state()['nomi']['status'],'reply_ready')
        self.service.save_event_cursor(20)
        self.assertEqual(self.service.event_cursor(),50)
        self.assertEqual(store.read()['in_flight']['packet'],'original synthetic packet')
        self.assertEqual(app.ResidentService(self.profiles,self.second).live_state(),{})

    def test_known_public_receipt_never_posts_same_recovered_reply_twice(self):
        self.service.save_live_nomi({'status':'reply_ready','reply':'SAY: Synthetic words.','request_id':'synthetic-request','packets':['original']})
        self.service.save_live_speech({'status':'confirmed','request_id':'synthetic-request','nonce':'original-nonce','receipt':{'spoke':True}})
        session = object.__new__(app.LiveEventsWindow)
        session.service = self.service
        session.stop_event = threading.Event()
        session.tasks = queue.Queue()
        session.speech_requests = queue.Queue()
        session.session_context = 'Map hangout'
        session.actual_place = ''
        with patch.object(app.postmark_client,'verify_sender'), patch.object(self.service,'postmark_key',return_value='synthetic-key'), patch.object(self.service,'_send_nomi') as nomi, patch.object(app.event_bridge,'speak') as speak:
            session.relay_loop(recovery_only=True)
        nomi.assert_not_called()
        speak.assert_not_called()
        self.assertEqual(self.service.live_state()['nomi']['status'],'resolved')

    def test_explicit_repack_only_changes_unstarted_mail_not_partial_checkpoints(self):
        pipeline = MailPipeline(resident_handle='resident-one',state_path=self.service.state_path,nomi_send=lambda m:self.fail('No sends'),discover=lambda:[],
            fetch=lambda source:({'id':'synthetic','from':'neighbor','to':'resident-one'},'word '*250),part_pause=0)
        pipeline.initialize()
        pipeline.poll(discovered=[('unstarted','source'),('partial','source')])
        state = pipeline.state.read()
        state['letters']['partial'].update(status='needs_attention',nomi_parts_completed=1)
        state['letters']['unstarted']['archived_at'] = 123
        before = copy.deepcopy(state['letters']['partial'])
        old_count = state['letters']['unstarted']['nomi_parts']
        pipeline.state.write(state)
        pipeline.message_limit = 800
        pipeline.repack_unstarted()
        after = pipeline.state.read()
        self.assertEqual(after['letters']['partial'],before)
        self.assertLess(after['letters']['unstarted']['nomi_parts'],old_count)
        self.assertEqual(after['letters']['unstarted']['archived_at'],123)
        self.assertEqual(after['seen'],state['seen'])

    def test_settings_save_control_fits_new_live_limit_option(self):
        AtomicState(self.service.state_path).write({'seen':[],'letters':{}})
        self.profiles.set_active(self.first)
        parent = app.App(self.profiles)
        try:
            parent.edit_resident()
            dialog = next(w for w in parent.winfo_children() if isinstance(w,app.tk.Toplevel))
            for geometry in ['650x740','620x720']:
                dialog.geometry(geometry)
                dialog.update()
                def descendants(widget):
                    return [child for item in widget.winfo_children() for child in [item]+descendants(item)]
                button = next(w for w in descendants(dialog) if isinstance(w,app.tk.Button) and w['text']=='Save Resident')
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
        finally:
            for callback in parent.tk.call('after','info'):
                parent.after_cancel(callback)
            parent.destroy()

    def test_public_approval_is_topmost_exact_readonly_and_close_keeps_private(self):
        self.profiles.set_active(self.first)
        parent = app.App(self.profiles)
        try:
            with patch.object(app.LiveEventsWindow,'refresh_calendar'):
                window = app.LiveEventsWindow(parent,self.service)
            exact = 'Synthetic words.\n\nSecond paragraph.'
            checks = []
            positions = []
            def inspect_close():
                dialog = next(w for w in window.winfo_children() if isinstance(w,app.tk.Toplevel))
                dialog.update()
                preview = next(w for w in dialog.winfo_children() if isinstance(w,app.tk.Text))
                checks.append(bool(dialog.attributes('-topmost')))
                checks.append(preview.get('1.0','end-1c') == exact)
                checks.append(str(preview['state']) == 'disabled')
                for geometry in ['640x380','480x280']:
                    dialog.geometry(geometry)
                    dialog.update()
                    actions = next(w for w in dialog.winfo_children() if isinstance(w,app.tk.Frame))
                    for button in actions.winfo_children():
                        positions.append((geometry, button.winfo_rooty()+button.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height()))
                        checks.append(button.winfo_rooty()+button.winfo_height() <= dialog.winfo_rooty()+dialog.winfo_height())
                checks.append(bool(dialog.protocol('WM_DELETE_WINDOW')))
                checks.append(bool(dialog.bind('<Escape>')))
                dialog.destroy()
            window.after(100,inspect_close)
            self.assertFalse(window.confirm_public_speech(exact))
            window.close()
            self.assertTrue(all(checks), (checks,positions))
        finally:
            for callback in parent.tk.call('after','info'):
                parent.after_cancel(callback)
            parent.destroy()

    def test_public_popup_approval_requires_explicit_approve_button(self):
        self.profiles.set_active(self.first)
        parent = app.App(self.profiles)
        try:
            with patch.object(app.LiveEventsWindow,'refresh_calendar'):
                window = app.LiveEventsWindow(parent,self.service)
            def approve():
                dialog = next(w for w in window.winfo_children() if isinstance(w,app.tk.Toplevel))
                actions = next(w for w in dialog.winfo_children() if isinstance(w,app.tk.Frame))
                next(w for w in actions.winfo_children() if w['text']=='Approve & speak').invoke()
            window.after(100,approve)
            self.assertTrue(window.confirm_public_speech('Synthetic words.'))
            window.close()
        finally:
            for callback in parent.tk.call('after','info'):
                parent.after_cancel(callback)
            parent.destroy()


if __name__ == '__main__': unittest.main()
