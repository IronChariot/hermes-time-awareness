"""Deterministic temporal context via Hermes' public lifecycle hooks."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import hashlib
from html import escape
import json
import logging
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')


@dataclass(frozen=True)
class Settings:
    timezone: str = 'Europe/London'
    threshold_minutes: float = 30
    user_name: str = 'The user'
    platforms: tuple[str, ...] = ('discord', 'telegram', 'slack', 'cli')
    scope: str = 'sender'

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if (isinstance(self.threshold_minutes, bool)
                or not isinstance(self.threshold_minutes, (int, float))
                or not math.isfinite(self.threshold_minutes) or self.threshold_minutes < 0):
            raise ValueError('threshold_minutes must be a finite nonnegative number')
        if not isinstance(self.user_name, str) or not self.user_name.strip() or len(self.user_name) > 80:
            raise ValueError('user_name must be 1–80 characters')
        if any(ord(c) < 32 for c in self.user_name):
            raise ValueError('user_name cannot contain control characters')
        if (not isinstance(self.platforms, (list, tuple)) or not self.platforms
                or any(not isinstance(p, str) or not p for p in self.platforms)):
            raise ValueError('platforms must be a nonempty list of platform names')
        if self.scope not in ('sender', 'session'):
            raise ValueError('scope must be sender or session')


def synthetic_message(value):
    """Conservative exclusion for known Hermes-generated wake-turn envelopes.

    The current public hook does not expose gateway MessageEvent.internal.
    Matching user quotations are safely omitted rather than rewriting history.
    """
    if not isinstance(value, str):
        return False
    text = re.sub(r'^\[[A-Z][a-z]{2} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]+\]\s*', '', value.lstrip())
    return text.startswith(('[ASYNC DELEGATION ', '[Background process ', '[IMPORTANT:',
                            '[CONTEXT COMPACTION', '[CONTEXT SUMMARY]'))


def valid_time(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 253402214400)


def elapsed_text(seconds):
    """Deliberately approximate; elapsed duration uses UTC, never local arithmetic."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return 'less than a minute ago'
    if minutes < 60:
        return f'{minutes} minute' + (' ago' if minutes == 1 else 's ago')
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        result = f'{hours} hour' + ('' if hours == 1 else 's')
        if minutes:
            result += f' {minutes} minute' + ('' if minutes == 1 else 's')
        return result + ' ago'
    days, hours = divmod(hours, 24)
    result = f'{days} day' + ('' if days == 1 else 's')
    if hours:
        result += f' {hours} hour' + ('' if hours == 1 else 's')
    return result + ' ago'


def render_context(now, last_reply, last_user, settings):
    if (not valid_time(now) or not valid_time(last_reply) or now < last_reply
            or now - last_reply <= settings.threshold_minutes * 60):
        return None
    zone = ZoneInfo(settings.timezone)
    local = datetime.fromtimestamp(now, zone)
    stamp = (f'{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} '
             f'{local.year}, {local:%H:%M} {local.tzname()}')
    # Hermes recognizes this outer fence when sanitizing replay/output and
    # locating other plugins' injected sidecars (including Noctuary). The inner
    # tag and wording identify this as timing metadata, not a recalled memory.
    lines = ['<memory-context>', '<temporal-context source="system">',
             'System-generated timing metadata, not part of the user’s message.',
             f'Temporal context: {stamp}.',
             f'Last assistant reply: {elapsed_text(now - last_reply)}.']
    if valid_time(last_user) and last_user <= now:
        previous = datetime.fromtimestamp(last_user, zone)
        relation = ('same local day' if local.date() == previous.date()
                    else 'local date changed since the previous message')
        lines.append(f'{escape(settings.user_name)}’s previous message: '
                     f'{elapsed_text(now - last_user)}; {relation}.')
    lines.extend(['</temporal-context>', '</memory-context>'])
    return '\n'.join(lines)


class TemporalHooks:
    """One general-plugin instance, not one callback per recreated AIAgent.

    Persist only timestamps, opaque scope keys and the latest decision. Observe
    successful model-turn completion, not platform delivery acknowledgement.
    Hermes serializes normal turns; concurrent writers to the same identity in
    separate processes are outside this plugin's tracking contract.
    """
    def __init__(self, state, settings=Settings(), clock=time.time):
        self.state, self.settings, self.clock = state, settings, clock
        self._pending = OrderedDict()
        self._lock = threading.RLock()

    def _allowed(self, session_id, platform, turn_id, parent_session_id=''):
        return (isinstance(session_id, str) and bool(session_id)
                and isinstance(turn_id, str) and bool(turn_id)
                and platform in self.settings.platforms and not parent_session_id)

    def _scope_key(self, session_id, platform, sender_id):
        use_sender = (self.settings.scope == 'sender' and isinstance(sender_id, str) and bool(sender_id))
        parts = [platform, 'sender' if use_sender else 'session', sender_id if use_sender else session_id]
        return 'clock:' + hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def _turn_key(self, session_id, turn_id):
        # Public state facade is task-local/profile-scoped, even in multiplex mode.
        return (str(self.state.data_dir.resolve()), session_id, turn_id)

    def pre_llm_call(self, session_id=None, platform=None, turn_id=None,
                     sender_id='', parent_session_id='', user_message=None, **kwargs):
        if (not self._allowed(session_id, platform, turn_id, parent_session_id)
                or synthetic_message(user_message)):
            return None
        try:
            now = self.clock()
            if not valid_time(now):
                return None
            key = self._scope_key(session_id, platform, sender_id)
            turn = self._turn_key(session_id, turn_id)
            with self._lock:
                record = self.state.get(key, {})
                if not isinstance(record, dict) or record.get('version', 1) != 1:
                    raise ValueError('unrecognized timing state')
                if record.get('turn_id') == turn_id and record.get('session_id') == session_id:
                    # Same turn/retry: retain the original timestamp and exact bytes.
                    context = record.get('context')
                    observed_at = record.get('last_user_at')
                else:
                    context = render_context(now, record.get('last_reply_at'), record.get('last_user_at'), self.settings)
                    observed_at = now
                    record.update(version=1, session_id=session_id, turn_id=turn_id,
                                  last_user_at=now, context=context)
                    self.state.set(key, record)
                pending = self._pending.get(turn)
                if pending is None:
                    self._pending[turn] = {'key': key, 'observed_at': observed_at, 'reply_at': None}
                while len(self._pending) > 256:
                    self._pending.popitem(last=False)  # omission beats false completion
                return {'context': context} if isinstance(context, str) and context else None
        except Exception as exc:
            logger.warning('Temporal context omitted (%s)', type(exc).__name__)
            return None

    def post_llm_call(self, session_id=None, platform=None, turn_id=None,
                      assistant_response=None, conversation_history=None, **kwargs):
        if not self._allowed(session_id, platform, turn_id, kwargs.get('parent_session_id', '')):
            return
        try:
            with self._lock:
                pending = self._pending.get(self._turn_key(session_id, turn_id))
                if pending is None:
                    return
                pending['reply_at'] = None
                if (not isinstance(assistant_response, str) or not assistant_response.strip()
                        or not isinstance(conversation_history, list) or not conversation_history):
                    return
                tail = conversation_history[-1]
                if (not isinstance(tail, dict) or tail.get('role') != 'assistant'
                        or tail.get('tool_calls') or tail.get('content') != assistant_response):
                    return
                now = self.clock()
                if valid_time(now) and now >= pending['observed_at']:
                    pending['reply_at'] = now
        except Exception as exc:
            logger.warning('Temporal reply observation omitted (%s)', type(exc).__name__)

    def on_session_end(self, session_id=None, platform=None, turn_id=None,
                       completed=None, failed=None, interrupted=None, **kwargs):
        if not self._allowed(session_id, platform, turn_id, kwargs.get('parent_session_id', '')):
            return
        try:
            with self._lock:
                pending = self._pending.pop(self._turn_key(session_id, turn_id), None)
                if (pending is None or not valid_time(pending['reply_at'])
                        or completed is not True or failed is not False or interrupted is not False):
                    return
                record = self.state.get(pending['key'], {})
                # A late completion must not regress a newer observed exchange.
                if (record.get('session_id') != session_id or record.get('turn_id') != turn_id
                        or record.get('last_user_at') != pending['observed_at']):
                    return
                record['last_reply_at'] = pending['reply_at']
                self.state.set(pending['key'], record)
        except Exception as exc:
            logger.warning('Temporal reply timestamp not saved (%s)', type(exc).__name__)


def register(ctx):
    """Configuration is frozen per plugin load; state remains profile-scoped."""
    settings = Settings(
        timezone=ctx.get_config('timezone', 'Europe/London'),
        threshold_minutes=ctx.get_config('threshold_minutes', 30),
        user_name=ctx.get_config('user_name', 'The user'),
        platforms=ctx.get_config('platforms', ['discord', 'telegram', 'slack', 'cli']),
        scope=ctx.get_config('scope', 'sender'),
    )
    hooks = TemporalHooks(ctx.state, settings)
    ctx.register_hook('pre_llm_call', hooks.pre_llm_call)
    ctx.register_hook('post_llm_call', hooks.post_llm_call)
    ctx.register_hook('on_session_end', hooks.on_session_end)
    logger.info('Time awareness enabled: timezone=%s threshold_minutes=%s scope=%s platforms=%s',
                settings.timezone, settings.threshold_minutes, settings.scope, ','.join(settings.platforms))
