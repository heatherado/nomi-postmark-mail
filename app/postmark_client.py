"""Postmark REST mail. No credentials are persisted or included in errors."""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

API = 'https://postmark.town/api'


class Refused(RuntimeError):
    pass


def request(path, *, key=None, payload=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'NomiCompanion/2'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(API + path, headers=headers,
        data=None if payload is None else json.dumps(payload).encode('utf-8'),
        method='GET' if payload is None else 'POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # A server error may follow a committed act. Never expose response bodies
        # or request headers: these can contain credentials or private content.
        kind = Refused if error.code in (400, 401, 403, 413, 422, 429) else RuntimeError
        raise kind(f'Postmark returned HTTP {error.code}.') from None
    except Exception:
        raise RuntimeError('Postmark returned no certain result. Inspect receipt state before retrying.') from None


def verify_sender(key, handle, *, transport=request):
    if not key or not handle:
        raise Refused('Configure the household key and registered Postmark handle first.')
    identity = transport('/me', key=key)
    if handle not in identity.get('handles', []):
        raise Refused('The household key does not authorize this resident. No mail was submitted.')
    return identity


def discover(handle, *, transport=request):
    # The REST mailbox is a list (not {inbox: [...]}); only delivered incoming
    # letters are passed into Nomi, never sender-visible standing/outbox mail.
    rows = mailbox(handle, transport=transport)
    return [(row['id'], '/letters/' + urllib.parse.quote(row['id'], safe=''))
            for row in rows if row.get('to') == handle and row.get('delivered_at')]


def mailbox(handle, *, transport=request):
    rows, offset = [], 0
    while True:
        page = transport('/mail/' + urllib.parse.quote(handle, safe='') + f'?limit=200&offset={offset}')
        if not isinstance(page, list):
            raise RuntimeError('Unexpected Postmark mailbox response.')
        rows.extend(page)
        if len(page) < 200:
            return rows
        offset += len(page)


def fetch(path, handle, *, transport=request):
    if not path.startswith('/letters/'):
        raise ValueError('Unexpected letter source.')
    letter = transport(path)
    if letter.get('to') != handle or not letter.get('delivered_at'):
        raise ValueError('This is not delivered mail for the active resident.')
    if not letter.get('body') or not letter.get('id') or not letter.get('from'):
        raise ValueError('Incomplete letter response.')
    fields = {k: letter[k] for k in ('id', 'from', 'to')}
    fields['title'] = letter.get('title') or letter.get('subjectSlug') or 'your letter'
    return fields, letter['body']


def _accept(entry, receipt):
    if not isinstance(receipt, dict) or not receipt.get('letter_id') or receipt.get('error'):
        raise RuntimeError('Postmark did not return a complete acceptance receipt.')
    entry['receipt'] = receipt
    entry['status'] = 'queued'
    entry.pop('error', None)


def submit_approved(pipeline, letter_key, exact_text, household_key, *, transport=request):
    state = pipeline.state.read()
    entry = state['letters'][letter_key]
    if entry.get('archived_at') or entry.get('status') != 'pending' or entry.get('submission'):
        raise ValueError('This reply is already locked or not awaiting approval.')
    if entry['fields']['to'] != pipeline.resident_handle:
        raise ValueError('The incoming letter belongs to another resident.')
    if not exact_text.strip():
        raise ValueError('The reply is empty.')
    if len(exact_text.encode('utf-8')) > 100000:
        raise ValueError('The reply exceeds Postmark’s 100 KB body limit.')
    verify_sender(household_key, pipeline.resident_handle, transport=transport)
    payload = {'from': pipeline.resident_handle, 'to': entry['fields']['from'],
        'title': 'Re: ' + entry['fields'].get('title', 'your letter'),
        'body': exact_text, 'thread': entry['fields']['id'], 'nonce': uuid.uuid4().hex}
    entry.update(draft=exact_text, status='submitting', submission=payload,
                 approved_at=datetime.now(timezone.utc).isoformat())
    pipeline.state.write(state)  # durable exact envelope and nonce BEFORE POST
    try:
        _accept(entry, transport('/letters', key=household_key, payload=payload))
    except Refused:
        entry['status'] = 'pending'
        entry.pop('submission', None)
        pipeline.state.write(state)
        raise
    except Exception:
        entry['status'] = 'needs_send_check'
        entry['error'] = 'Submission may have succeeded. Check Postmark; no automatic resend is permitted.'
        pipeline.state.write(state)
        raise
    pipeline.state.write(state)
    return entry['receipt']


def check_submission(pipeline, letter_key, household_key, *, transport=request):
    """Read only. Absence is never evidence that an uncertain send failed."""
    state = pipeline.state.read()
    entry = state['letters'][letter_key]
    if not entry.get('submission'):
        return entry
    verify_sender(household_key, pipeline.resident_handle, transport=transport)
    receipt = entry.get('receipt', {})
    payload = entry['submission']
    rows = mailbox(payload['to'], transport=transport)
    if not receipt.get('letter_id'):
        # Recover a lost receipt only from a full, delivered letter with the
        # exact saved sender, recipient, thread and approved body.
        matches = []
        for row in rows:
            if row.get('from') == payload['from'] and row.get('to') == payload['to'] and row.get('thread') == payload['thread'] and row.get('delivered_at'):
                full = transport('/letters/' + urllib.parse.quote(row['id'], safe=''))
                if full.get('body', '').strip() == payload['body'].strip():
                    matches.append(row)
        if len(matches) == 1:
            receipt = {'letter_id': matches[0]['id'], 'recovered_from_delivered_record': True}
            entry['receipt'] = receipt
        else:
            doorstep = transport('/doorstep/' + urllib.parse.quote(payload['from'], safe=''), key=household_key)
            entry['possible_pending_matches'] = [r for r in (doorstep.get('your_pending_letters') or {}).get('standing', [])
                if r.get('handle') == payload['from'] and r.get('to') == payload['to'] and r.get('thread') == payload['thread'] and r.get('title') == payload['title']]
    if receipt.get('letter_id'):
        delivered = next((r for r in rows if r.get('id') == receipt['letter_id']
            and r.get('from') == pipeline.resident_handle and r.get('to') == payload['to'] and r.get('delivered_at')), None)
        if delivered:
            entry['status'] = 'delivered'
            entry['delivered_at'] = delivered['delivered_at']
    pipeline.state.write(state)
    return entry


def retry_uncertain(pipeline, letter_key, household_key, *, human_checked=False, transport=request):
    if not human_checked:
        raise ValueError('Inspect Postmark and explicitly approve the saved envelope before retrying.')
    state = pipeline.state.read()
    entry = state['letters'][letter_key]
    if entry.get('status') != 'needs_send_check' or not entry.get('submission'):
        raise ValueError('No uncertain direct submission is available.')
    verify_sender(household_key, pipeline.resident_handle, transport=transport)
    # Never mint a new nonce or alter the approved text/recipient during recovery.
    receipt = transport('/letters', key=household_key, payload=entry['submission'])
    _accept(entry, receipt)
    pipeline.state.write(state)
    return receipt
