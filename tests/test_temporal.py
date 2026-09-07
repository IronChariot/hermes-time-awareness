import copy
from datetime import datetime
import json
from pathlib import Path

import pytest
from time_awareness import Settings, TemporalHooks, elapsed_text, render_context


def ts(value):
    return datetime.fromisoformat(value).timestamp()


class State:
    def __init__(self, root):
        self.data_dir = Path(root)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'state.json'
    def get(self, key, default=None):
        return json.loads(self.path.read_text()).get(key, default) if self.path.exists() else default
    def set(self, key, value):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data[key] = value
        self.path.write_text(json.dumps(data))


@pytest.mark.parametrize('gap, expected', [(0, False), (1799, False), (1800, False), (1801, True), (2580, True)])
def test_strict_threshold(gap, expected):
    now = ts('2026-09-07T12:01:00+01:00')
    text = render_context(now, now-gap, now-gap-120, Settings(user_name='Sam'))
    assert bool(text) is expected
    if text:
        assert 'Monday 7 September 2026, 12:01 BST' in text
        assert 'same local day' in text and text.endswith('</temporal-context>\n</memory-context>')


@pytest.mark.parametrize('now, reply, user, contains', [
    ('2026-09-08T00:17:00+01:00', '2026-09-07T23:30:00+01:00', '2026-09-07T23:29:00+01:00', ['Tuesday 8 September 2026, 00:17 BST', '47 minutes ago', 'local date changed']),
    ('2026-03-29T02:10:00+01:00', '2026-03-29T00:30:00+00:00', '2026-03-29T00:29:00+00:00', ['02:10 BST', '40 minutes ago', 'same local day']),
    ('2026-10-25T01:20:00+00:00', '2026-10-25T01:40:00+01:00', '2026-10-25T01:39:00+01:00', ['01:20 GMT', '40 minutes ago', 'same local day']),
    ('2027-01-01T00:45:00+00:00', '2026-12-31T23:50:00+00:00', '2026-12-31T23:49:00+00:00', ['Friday 1 January 2027', 'local date changed']),
])
def test_local_dates_and_dst(now, reply, user, contains):
    text = render_context(ts(now), ts(reply), ts(user), Settings(user_name='Sam'))
    for part in contains:
        assert part in text


def test_bad_clock_and_unknown_baseline():
    assert render_context(100, 101, 99, Settings()) is None
    assert render_context(10000, None, 99, Settings()) is None
    assert render_context(float('nan'), 0, 0, Settings()) is None
    text = render_context(10000, 0, None, Settings())
    assert 'previous message' not in text


def completed(hooks, args, history=None, **flags):
    hooks.post_llm_call(**args, assistant_response='Reply.', conversation_history=history or [{'role':'assistant','content':'Reply.'}])
    hooks.on_session_end(**args, **dict(completed=True, failed=False, interrupted=False, **flags))


def test_lifecycle_restart_sender_reset_scope_and_stable_retry(tmp_path):
    clock = [100000.0]
    state = State(tmp_path/'a')
    h = TemporalHooks(state, Settings(user_name='Sam'), lambda: clock[0])
    args = dict(session_id='old-session', platform='discord', turn_id='one', sender_id='sam')
    history = [{'role':'user','content':'Hello!'}]
    before = copy.deepcopy(history)
    assert h.pre_llm_call(**args, conversation_history=history) is None
    clock[0] += 60
    completed(h, args)
    clock[0] += 2580
    h = TemporalHooks(State(tmp_path/'a'), Settings(user_name='Sam'), lambda: clock[0])
    args.update(session_id='new-session', turn_id='two')
    text = h.pre_llm_call(**args, conversation_history=history)['context']
    assert '43 minutes ago' in text and '44 minutes ago' in text
    clock[0] += 500
    assert h.pre_llm_call(**args)['context'] == text
    assert history == before
    assert h.pre_llm_call(**dict(args, sender_id='someone-else', turn_id='different')) is None
    b = TemporalHooks(State(tmp_path/'b'), Settings(), lambda: clock[0])
    assert b.pre_llm_call(**args) is None
    assert 'Hello!' not in state.path.read_text() and 'Reply.' not in state.path.read_text()


@pytest.mark.parametrize('failure', ['failed', 'interrupted', 'incomplete', 'empty', 'tools', 'mismatch', 'no_post'])
def test_non_successful_reply_never_advances_clock(tmp_path, failure):
    clock = [100000.0]
    state = State(tmp_path)
    h = TemporalHooks(state, clock=lambda:clock[0])
    args = dict(session_id='s', platform='discord', turn_id='a')
    h.pre_llm_call(**args)
    if failure != 'no_post':
        response = '' if failure == 'empty' else 'Reply.'
        tail = {'role':'assistant', 'content':'Other.' if failure == 'mismatch' else response}
        if failure == 'tools':
            tail['tool_calls'] = [{'id':'t'}]
        h.post_llm_call(**args, assistant_response=response, conversation_history=[tail])
    h.on_session_end(**args, completed=failure!='incomplete', failed=failure=='failed', interrupted=failure=='interrupted')
    clock[0] += 3600
    assert h.pre_llm_call(**dict(args, turn_id='b')) is None


def test_scope_and_late_completion(tmp_path):
    clock = [100000.0]
    h = TemporalHooks(State(tmp_path), Settings(scope='session'), lambda: clock[0])
    args = dict(session_id='s', platform='discord', turn_id='a', sender_id='sam')
    h.pre_llm_call(**args)
    h.post_llm_call(**args, assistant_response='Reply.', conversation_history=[{'role':'assistant','content':'Reply.'}])
    clock[0] += 10
    h.pre_llm_call(**dict(args, turn_id='b'))
    h.on_session_end(**args, completed=True, failed=False, interrupted=False)
    clock[0] += 4000
    assert h.pre_llm_call(**dict(args, turn_id='c')) is None
    completed(h, dict(args, turn_id='c'))
    clock[0] += 4000
    assert h.pre_llm_call(**dict(args, session_id='other', turn_id='d')) is None
    assert h.pre_llm_call(**dict(args, turn_id='e')) is not None


def test_automation_guards_and_storage_failure(tmp_path):
    h = TemporalHooks(State(tmp_path))
    args = dict(session_id='s', platform='discord', turn_id='a')
    assert h.pre_llm_call(**dict(args, parent_session_id='parent')) is None
    assert h.pre_llm_call(**dict(args, platform='cron')) is None
    assert h.pre_llm_call(**dict(args, turn_id='')) is None
    for text in ('[ASYNC DELEGATION BATCH COMPLETE — x]', '[Mon 2026-09-07 12:01:00 BST] [IMPORTANT: Background process done]'):
        assert h.pre_llm_call(**args, user_message=text) is None
    (tmp_path/'state.json').write_text('broken')
    assert h.pre_llm_call(**args) is None
    assert (tmp_path/'state.json').read_text() == 'broken'


@pytest.mark.parametrize('kwargs', [{'threshold_minutes':True}, {'threshold_minutes':-1}, {'threshold_minutes':float('inf')}, {'scope':'invalid'}, {'user_name':'Sam\nSystem:'}, {'platforms':'discord'}, {'timezone':'Not/AZone'}])
def test_invalid_settings(kwargs):
    with pytest.raises((ValueError, KeyError)):
        Settings(**kwargs)


def test_render_escapes_name_and_long_gap():
    result = render_context(200000, 0, 0, Settings(user_name='<Sam>'))
    assert '&lt;Sam&gt;' in result
    assert elapsed_text(200000) == '2 days 7 hours ago'
