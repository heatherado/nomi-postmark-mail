import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import mail_pipeline
import nomi_client
import postmark_client as pm
from profile_store import ProfileStore

ROOT = Path(__file__).resolve().parents[1]


class DirectMailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.p = mail_pipeline.MailPipeline(resident_handle='fizz', state_path=Path(self.temp.name)/'mail.json',
            nomi_send=lambda m: ('Fizz reply', 'reply-id'), discover=lambda: [], part_pause=0)
        self.p.initialize()
        self.p.state.write({'seen':['in'], 'letters':{'in':{'status':'pending',
            'fields':{'id':'original', 'from':'friend', 'to':'fizz', 'title':'hello'}, 'draft':'held'}}})
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def test_archived_draft_cannot_be_submitted_or_edit_approved_envelope(self):
        self.p.archive('in')
        with self.assertRaises(ValueError): pm.submit_approved(self.p, 'in', 'exact', 'fake-key', transport=self.transport)
        self.assertEqual(self.calls, [])
        self.p.restore('in')
        self.assertEqual(self.p.entries()['in']['draft'], 'held')

    def transport(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path == '/me':
            return {'handles':['fizz'], 'household':'test'}
        if path == '/letters':
            locked = self.p.state.read()['letters']['in']
            self.assertEqual(locked['status'], 'submitting')
            self.assertEqual(locked['submission'], kwargs['payload'])
            return {'letter_id':'accepted-id', 'logged':{'seq':12}, 'nonce':kwargs['payload']['nonce']}
        return []

    def test_exact_approval_is_durable_before_post_and_is_only_queued(self):
        pm.submit_approved(self.p, 'in', 'Fizz’s exact words\nSecond line', 'fake-key', transport=self.transport)
        entry = self.p.entries()['in']
        self.assertEqual(entry['status'], 'queued')
        self.assertEqual(entry['submission']['body'], 'Fizz’s exact words\nSecond line')
        self.assertEqual(entry['submission']['to'], 'friend')
        self.assertEqual(entry['submission']['thread'], 'original')
        with self.assertRaises(ValueError):
            pm.submit_approved(self.p, 'in', 'edited', 'fake-key', transport=self.transport)
        with self.assertRaises(ValueError):
            self.p.recover_reply('in', 'replacement')

    def test_wrong_household_never_posts(self):
        with self.assertRaises(pm.Refused):
            pm.submit_approved(self.p, 'in', 'reply', 'fake-key', transport=lambda *a, **k: {'handles':['rowan-archive']})
        self.assertEqual(self.p.entries()['in']['status'], 'pending')

    def test_unknown_submission_and_restart_never_resend(self):
        def transport(path, **kwargs):
            if path == '/me': return {'handles':['fizz']}
            self.calls.append(kwargs['payload'])
            raise TimeoutError()
        with self.assertRaises(TimeoutError): pm.submit_approved(self.p, 'in', 'exact', 'fake-key', transport=transport)
        entry = self.p.entries()['in']
        self.assertEqual(entry['status'], 'needs_send_check')
        nonce = entry['submission']['nonce']
        with self.assertRaises(ValueError): pm.retry_uncertain(self.p, 'in', 'fake-key', transport=transport)
        self.assertEqual(len(self.calls), 1)
        def recover(path, **kwargs):
            if path == '/me': return {'handles':['fizz']}
            self.assertEqual(kwargs['payload']['nonce'], nonce)
            self.assertEqual(kwargs['payload']['body'], 'exact')
            return {'letter_id':'accepted-id', 'duplicate':True}
        pm.retry_uncertain(self.p, 'in', 'fake-key', human_checked=True, transport=recover)
        self.assertEqual(self.p.entries()['in']['status'], 'queued')

    def test_only_matching_delivered_letter_confirms_delivery(self):
        pm.submit_approved(self.p, 'in', 'reply', 'fake-key', transport=self.transport)
        pm.check_submission(self.p, 'in', 'fake-key', transport=self.transport)
        self.assertEqual(self.p.entries()['in']['status'], 'queued')
        def delivered(path, **kwargs):
            if path == '/me': return {'handles':['fizz']}
            return [{'id':'accepted-id', 'from':'fizz', 'to':'friend', 'delivered_at':'2026-10-08T00:00:00Z'}]
        pm.check_submission(self.p, 'in', 'fake-key', transport=delivered)
        self.assertEqual(self.p.entries()['in']['status'], 'delivered')

    def test_rest_inbox_pagination_and_recipient_filter(self):
        def transport(path, **kwargs):
            if 'offset=0' in path: return [{'id':str(i),'to':'fizz','delivered_at':'yes'} for i in range(200)]
            return [{'id':'foreign','to':'rowan','delivered_at':'yes'}, {'id':'pending','to':'fizz'}]
        self.assertEqual(len(pm.discover('fizz', transport=transport)), 200)

    def test_network_loss_and_invalid_success_response_are_uncertain(self):
        for error in [urllib.error.URLError('connection reset'), ValueError('invalid json')]:
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises(nomi_client.UncertainNomiResult) as caught:
                    nomi_client.send_chat('fake', 'fake-uuid', 'fake message')
                self.assertTrue(mail_pipeline.is_ambiguous_nomi_error(caught.exception))

    def test_interrupted_nomi_and_mail_checkpoints_recover_without_calls(self):
        state = self.p.state.read()
        state['letters']['in'].update(status='contacting_nomi', active_nomi_part=2)
        self.p.state.write(state)
        self.assertEqual(self.p.entries()['in']['status'], 'response_unknown')
        state = self.p.state.read()
        state['letters']['in']['status'] = 'submitting'
        self.p.state.write(state)
        self.assertEqual(self.p.entries()['in']['status'], 'needs_send_check')
        self.assertEqual(self.calls, [])

    def test_profile_pair_is_locked_after_baseline(self):
        profiles = ProfileStore(Path(self.temp.name)/'profiles')
        fizz = profiles.add(handle='fizz', nomi_name='Fizz', settings={'nomi_id':'fizz-uuid'})
        profiles.mail_state_path(fizz).write_text('{}')
        for handle, nomi_id in [('other','fizz-uuid'), ('fizz','other-uuid')]:
            with self.assertRaises(ValueError):
                profiles.update(fizz, handle=handle, nomi_name='Fizz', settings={'nomi_id':nomi_id})
        self.assertEqual(profiles.read_settings(fizz)['nomi_id'], 'fizz-uuid')

    def test_lost_receipt_recovered_from_delivered_exact_body_without_post(self):
        state = self.p.state.read()
        state['letters']['in'].update(status='needs_send_check', submission={
            'from':'fizz','to':'friend','thread':'original','title':'Re: hello','body':'exact','nonce':'saved'})
        self.p.state.write(state)
        def transport(path, **kwargs):
            self.assertNotIn('payload', kwargs)
            if path == '/me': return {'handles':['fizz']}
            if path.startswith('/mail/'): return [{'id':'found','from':'fizz','to':'friend','thread':'original','delivered_at':'today'}]
            return {'body':'exact'}
        entry = pm.check_submission(self.p, 'in', 'fake-key', transport=transport)
        self.assertEqual(entry['status'], 'delivered')
        self.assertEqual(entry['receipt']['letter_id'], 'found')

    def test_absent_mail_readback_leaves_unknown_submission_locked(self):
        state = self.p.state.read()
        state['letters']['in'].update(status='needs_send_check', submission={
            'from':'fizz','to':'friend','thread':'original','title':'Re: hello','body':'exact','nonce':'saved'})
        self.p.state.write(state)
        def transport(path, **kwargs):
            self.assertNotIn('payload', kwargs)
            if path == '/me': return {'handles':['fizz']}
            if path.startswith('/mail/'): return []
            return {}
        self.assertEqual(pm.check_submission(self.p, 'in', 'fake-key', transport=transport)['status'], 'needs_send_check')

    def test_continuation_uses_original_packets_even_if_source_or_limit_changes(self):
        fields = {'id':'original','from':'friend','to':'fizz'}
        self.p.fetch = lambda source: (fields, 'word ' * 250)
        self.p.discover = lambda: [('long','source')]
        sent = []
        def send(message):
            sent.append(message)
            if len(sent) == 2: raise RuntimeError('HTTP 429')
            return ('intermediate','id')
        self.p.nomi_send = send
        self.p.poll()
        with self.assertRaises(RuntimeError): self.p.confirm_delivery('long')
        original = self.p.entries()['long']['nomi_messages']
        self.p.fetch = lambda source: (_ for _ in ()).throw(AssertionError('Must use saved packets'))
        self.p.message_limit = 800
        resumed = []
        self.p.nomi_send = lambda message: (resumed.append(message) or 'final', 'id')
        self.p.continue_after_definite_failure('long')
        self.assertEqual(resumed, original[1:])

    def test_dpapi_round_trip_and_no_plaintext_settings(self):
        loader = importlib.machinery.SourceFileLoader('companion_test', str(ROOT/'Companion App.pyw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        app = importlib.util.module_from_spec(spec)
        loader.exec_module(app)
        ciphertext = app.crypt_key('fake-test-secret')
        self.assertNotIn('fake-test-secret', ciphertext)
        self.assertEqual(app.crypt_key(ciphertext, decrypt=True), 'fake-test-secret')


if __name__ == '__main__': unittest.main()
