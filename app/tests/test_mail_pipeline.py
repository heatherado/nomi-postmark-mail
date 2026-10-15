import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import github_mailer
import mail_pipeline


def letter(body="Hello from the road."):
    return (
        "---\n"
        "id: sender-2026-01-01-note\n"
        "from: sender-resident\n"
        "to: recipient-resident\n"
        "title: A note\n"
        "---\n\n"
        + body
    )


class MailPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.state_path = pathlib.Path(self.temporary.name) / "state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def pipeline(self, *, discover=None, fetch=None, send=None, limit=400):
        return mail_pipeline.MailPipeline(
            resident_handle="recipient-resident",
            state_path=self.state_path,
            nomi_send=send or (lambda message: ("draft reply", "reply-id")),
            message_limit=limit,
            part_pause=0,
            discover=discover or (lambda: []),
            fetch=fetch or (lambda source: mail_pipeline.parse_letter_text(letter(), "recipient-resident")),
        )

    def test_initialization_baselines_without_contacting_nomi(self):
        calls = []
        pipeline = self.pipeline(
            discover=lambda: [("old-one", "url-one"), ("old-two", "url-two")],
            send=lambda message: calls.append(message),
        )
        pipeline.initialize()
        state = pipeline.state.read()
        self.assertEqual(state["seen"], ["old-one", "old-two"])
        self.assertEqual(calls, [])

    def test_import_existing_queues_for_review_without_any_nomi_calls(self):
        calls = []
        pipeline = self.pipeline(discover=lambda:[('old','source')], send=lambda m:calls.append(m))
        pipeline.initialize(import_existing=True)
        self.assertEqual(calls, [])
        self.assertEqual(pipeline.entries()['old']['status'], 'awaiting_nomi_confirmation')
        with self.assertRaises(RuntimeError): pipeline.initialize(import_existing=True)

    def test_archive_restore_preserves_unknown_checkpoint_and_deduplication(self):
        calls = []
        pipeline = self.pipeline(send=lambda m:calls.append(m))
        pipeline.initialize()
        state = pipeline.state.read()
        state['seen'] = ['uncertain']
        state['letters']['uncertain'] = {'status':'response_unknown', 'nomi_parts_completed':2, 'active_nomi_part':3, 'body':'saved letter'}
        pipeline.state.write(state)
        before = dict(state['letters']['uncertain'])
        pipeline.archive('uncertain')
        pipeline.discover = lambda:[('uncertain','source')]
        pipeline.poll()
        self.assertEqual(calls, [])
        archived = pipeline.entries()['uncertain']
        for key, value in before.items(): self.assertEqual(archived[key], value)
        with self.assertRaises(ValueError): pipeline.recover_reply('uncertain','reply')
        pipeline.restore('uncertain')
        self.assertEqual(pipeline.entries()['uncertain']['status'], 'response_unknown')
        self.assertEqual(pipeline.entries()['uncertain']['nomi_parts_completed'], 2)
        with self.assertRaises(ValueError): pipeline.confirm_delivery('uncertain')

    def test_archive_unanswered_prevents_delivery_until_explicit_restoration(self):
        pipeline = self.pipeline()
        pipeline.initialize()
        pipeline.discover = lambda:[('unanswered','source')]
        pipeline.poll()
        pipeline.archive('unanswered')
        with self.assertRaises(ValueError): pipeline.confirm_delivery('unanswered')
        pipeline.restore('unanswered')
        self.assertEqual(pipeline.entries()['unanswered']['status'], 'awaiting_nomi_confirmation')

    def test_skip_verified_uncertain_final_part_sends_nothing_and_checkpoint_never_rewinds(self):
        calls = []
        pipeline = self.pipeline(send=lambda m:calls.append(m))
        pipeline.initialize()
        state = pipeline.state.read()
        state['letters']['checked'] = {'status':'response_unknown','nomi_parts':2,'nomi_parts_completed':1,'nomi_messages':['one','two']}
        pipeline.state.write(state)
        result = pipeline.resend_after_human_check('checked', human_checked_chat=True, next_part=3)
        self.assertEqual(calls, [])
        self.assertEqual(result['nomi_parts_completed'], 2)
        with self.assertRaises(ValueError): pipeline.resend_after_human_check('checked', human_checked_chat=True, next_part=2)
        pipeline.recover_reply('checked', 'copied final reply')
        self.assertEqual(pipeline.entries()['checked']['status'], 'pending')

    def test_skip_verified_part_continues_only_with_original_remaining_packets(self):
        calls = []
        pipeline = self.pipeline(send=lambda m:(calls.append(m) or 'final reply','id'))
        pipeline.initialize()
        state = pipeline.state.read()
        state['letters']['checked'] = {'status':'needs_attention','fields':{'from':'friend'},'body':'original','nomi_parts':3,'nomi_parts_completed':1,'nomi_messages':['one','two','three']}
        pipeline.state.write(state)
        with self.assertRaises(ValueError): pipeline.continue_after_definite_failure('checked', next_part=3)
        pipeline.continue_after_definite_failure('checked', next_part=3, human_checked_chat=True)
        self.assertEqual(calls, ['three'])

    def test_missing_packets_with_confirmed_parts_are_never_restaged_from_changed_source(self):
        pipeline = self.pipeline(fetch=lambda source:self.fail('Must never refetch confirmed letter'))
        pipeline.initialize()
        state = pipeline.state.read()
        state['letters']['legacy'] = {'status':'needs_attention','nomi_parts_completed':1,'nomi_parts':2,'source':'changed'}
        pipeline.state.write(state)
        with self.assertRaises(ValueError): pipeline.prepare_delivery('legacy')

    def test_poll_and_restart_stage_only_and_approval_uses_saved_packets(self):
        calls = []
        pipeline = self.pipeline(send=lambda message: (calls.append(message) or 'reply', 'id'))
        pipeline.initialize()
        pipeline.discover = lambda: [('incoming', 'source')]
        pipeline.poll()
        original = pipeline.entries()['incoming']['nomi_messages']
        pipeline.poll()
        self.assertEqual(calls, [])
        restarted = self.pipeline(send=pipeline.nomi_send, fetch=lambda source: self.fail('Must not refetch approved content'))
        restarted.message_limit = 800
        restarted.confirm_delivery('incoming')
        self.assertEqual(calls, original)
        with self.assertRaises(ValueError): restarted.confirm_delivery('incoming')

    def test_clear_submission_retains_dedupe_and_rejects_uncertainty(self):
        pipeline = self.pipeline()
        pipeline.initialize()
        state = pipeline.state.read()
        state['seen'] = ['sent']
        state['letters']['sent'] = {'status':'needs_send_check', 'submission':{'nonce':'original'}, 'draft':'exact'}
        pipeline.state.write(state)
        with self.assertRaises(ValueError): pipeline.clear_submitted('sent')
        state['letters']['sent']['status'] = 'delivered'
        pipeline.state.write(state)
        pipeline.clear_submitted('sent')
        pipeline.discover = lambda: [('sent','source')]
        pipeline.poll()
        self.assertEqual(pipeline.entries()['sent']['submission']['nonce'], 'original')
        self.assertEqual(pipeline.entries()['sent']['status'], 'delivered')

    def test_short_and_multipart_wrappers_are_bounded(self):
        fields, short_body = mail_pipeline.parse_letter_text(letter(), "recipient-resident")
        one = mail_pipeline.format_nomi_messages(fields, short_body, 400)
        self.assertEqual(len(one), 1)
        self.assertNotIn("[1/", one[0])
        long = mail_pipeline.format_nomi_messages(fields, "word " * 500, 400)
        self.assertGreater(len(long), 2)
        self.assertTrue(all(len(message) <= 400 for message in long))
        self.assertIn("Wait for FINAL", long[0])
        self.assertIn("FINAL:", long[-1])

    def test_poll_claims_and_holds_successful_draft(self):
        pipeline = self.pipeline()
        pipeline.initialize()
        pipeline.discover = lambda: [("new-letter", "source")]
        pipeline.poll()
        self.assertEqual(pipeline.entries()['new-letter']['status'], 'awaiting_nomi_confirmation')
        pipeline.confirm_delivery('new-letter')
        entry = pipeline.entries()["new-letter"]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["draft"], "draft reply")
        self.assertEqual(entry["nomi_parts_completed"], 1)

    def test_definite_failure_continues_only_after_confirmed_parts(self):
        fields, body = mail_pipeline.parse_letter_text(letter("word " * 500), "recipient-resident")
        expected = mail_pipeline.format_nomi_messages(fields, body, 400)
        sent_first = []

        def fail_on_third(message):
            sent_first.append(message)
            if len(sent_first) == 3:
                raise RuntimeError("connection refused")
            return ("intermediate", f"reply-{len(sent_first)}")

        pipeline = self.pipeline(
            fetch=lambda source: (fields, body),
            send=fail_on_third,
        )
        pipeline.initialize()
        pipeline.discover = lambda: [("long-letter", "source")]
        pipeline.poll()
        with self.assertRaises(RuntimeError):
            pipeline.confirm_delivery('long-letter')
        failed = pipeline.entries()["long-letter"]
        self.assertEqual(failed["status"], "needs_attention")
        self.assertEqual(failed["nomi_parts_completed"], 2)

        resumed = []
        pipeline.nomi_send = lambda message: (resumed.append(message) or "final draft", "final-id")
        pipeline.continue_after_definite_failure("long-letter")
        self.assertEqual(resumed, expected[2:])
        self.assertEqual(pipeline.entries()["long-letter"]["status"], "pending")

    def test_timeout_is_uncertain_and_manual_paste_does_not_resend(self):
        calls = []

        def timeout(message):
            calls.append(message)
            raise TimeoutError("read timed out")

        pipeline = self.pipeline(send=timeout)
        pipeline.initialize()
        pipeline.discover = lambda: [("timed-letter", "source")]
        pipeline.poll()
        with self.assertRaises(TimeoutError):
            pipeline.confirm_delivery('timed-letter')
        uncertain = pipeline.entries()["timed-letter"]
        self.assertEqual(uncertain["status"], "response_unknown")
        self.assertEqual(uncertain["ambiguous_part"], 1)

        recovered = pipeline.recover_reply("timed-letter", "reply copied from normal chat")
        self.assertEqual(recovered["status"], "pending")
        self.assertTrue(recovered["recovered_manually"])
        self.assertEqual(len(calls), 1)

    def test_uncertain_resend_requires_human_check(self):
        def timeout(_message):
            raise RuntimeError("HTTP 504")

        pipeline = self.pipeline(send=timeout)
        pipeline.initialize()
        pipeline.discover = lambda: [("uncertain", "source")]
        pipeline.poll()
        with self.assertRaises(RuntimeError):
            pipeline.confirm_delivery('uncertain')

        pipeline.nomi_send = lambda message: ("recovered draft", "reply-id")
        with self.assertRaises(ValueError):
            pipeline.resend_after_human_check("uncertain")
        result = pipeline.resend_after_human_check("uncertain", human_checked_chat=True)
        self.assertEqual(result["status"], "pending")

    def test_interrupted_github_submission_locks_for_inspection(self):
        pipeline = self.pipeline()
        pipeline.initialize()
        state = pipeline.state.read()
        fields, body = mail_pipeline.parse_letter_text(letter(), "recipient-resident")
        state["seen"].append("ready")
        state["letters"]["ready"] = {
            "status": "pending", "fields": fields, "body": body, "draft": "approved words"
        }
        pipeline.state.write(state)

        def uncertain_submission(*args, **kwargs):
            raise RuntimeError("network disappeared")

        with self.assertRaises(RuntimeError):
            github_mailer.submit_approved(
                pipeline, "ready", "approved words", create_pr=uncertain_submission
            )
        locked = pipeline.entries()["ready"]
        self.assertEqual(locked["status"], "needs_send_check")
        self.assertIn("Inspect GitHub", locked["error"])


if __name__ == "__main__":
    unittest.main()
