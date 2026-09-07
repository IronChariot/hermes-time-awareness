# Hermes Time Awareness

A small, deterministic Hermes plugin that supplies local date/time and conversation-gap context **only when more than 30 minutes have passed since the last observed successful assistant reply**.

No LLM calls, network requests, tools, timers or Hermes-core patches. The code uses the public general-plugin hooks and `ctx.state` persistence, not a callback registered per AIAgent. It therefore survives ordinary agent recreation and normal Hermes upgrades, subject to the documented plugin API remaining compatible.

## What the model receives

The original user message is followed by a fenced suffix, for example:

```text
<temporal-context source="system">
System-generated timing metadata, not part of the user’s message.
Temporal context: Tuesday 8 September 2026, 00:17 BST.
Last assistant reply: 47 minutes ago.
Sam’s previous message: 48 minutes ago; local date changed since the previous message.
</temporal-context>
```

The plugin wraps this inner tag in Hermes' recognized `<memory-context>` fence. That outer fence allows the host to sanitize context correctly and lets Noctuary continue locating its own recalled packets when both plugins add context. The inner wording explicitly identifies timing metadata; it is **not** a recalled autobiographical memory.

The supported `pre_llm_call` API **appends** context. This plugin does not use request middleware to force a prefix: rewriting only outgoing requests would complicate transport compatibility and exact historical replay. No user-visible message, system prompt or old transcript row is edited. Hermes itself stores the exact model-facing suffix in the current message's `api_content`, so later requests replay the old timestamp unchanged. The current timestamp is not recomputed every tool iteration.

## Installation

Requirements: Python 3.10+ with IANA timezone data, and a Hermes version supporting `ctx.state`, `pre_llm_call` with `turn_id`, `post_llm_call`, and per-turn completion flags on `on_session_end`. The live integration test is the compatibility check; no source-level monkey patches are installed.

Clone the repository into a normal directory outside the Hermes source checkout, then link the package into the **chosen profile only**:

```bash
git clone git@github.com:IronChariot/hermes-time-awareness.git ~/main/hermes-time-awareness
ln -s ~/main/hermes-time-awareness/time_awareness ~/.hermes/profiles/wren/plugins/time_awareness
```

Merge the following into that profile's `config.yaml`—preserve its existing enabled/disabled plugins and settings:

```yaml
plugins:
  enabled:
    - time_awareness
  entries:
    time_awareness:
      settings:
        timezone: Europe/London
        threshold_minutes: 30
        user_name: Sam
        platforms: [discord]
        scope: sender
```

Restart only that profile's gateway when it is idle. Settings are loaded once per plugin registration. The plugin does **not** reset the conversation or switch the model.

For CLI use include `cli` in `platforms`; other platforms can be explicitly listed. The default platform list is Discord, Telegram, Slack and CLI, but production installations should narrow it to what they use. No private keys or credentials belong in this repository.

## Exact semantics

- **Trigger:** strictly greater than `threshold_minutes` since the last successful observed assistant turn. Exactly 30 minutes does not trigger the default. Midnight alone does not override the gap threshold.
- **Clock:** UTC epoch seconds from the host clock, formatted in the configured IANA timezone. `Europe/London` changes between BST and GMT automatically. Elapsed duration is computed in UTC, including across DST changes.
- **Current time:** the time the host begins processing the turn, not a forged timestamp parsed from the user's prose. Queueing or long hook delays may make it differ slightly from platform send time.
- **Whose gap:** the threshold uses the assistant's last reply; the separate user line compares the previous incoming conversational turn. Date-change wording refers to that previous user turn's date in the currently configured timezone.
- **First installation:** no fabricated history or implicit scan of private transcripts. Tracking starts with the first observed exchange. The first note becomes eligible after an observed successful reply and a qualifying gap.
- **Restarts:** timing state persists in Hermes' profile-scoped `plugin-data` area through `ctx.state`. It contains timestamps, hashed identity keys and the latest generated note—not user messages or assistant response text.
- **Daily resets:** default `scope: sender` tracks a given sender/platform across sessions **when Hermes supplies a sender ID**. This suits a companion's personal DM. It also spans that sender's different chats with the same profile; choose `scope: session` for per-session isolation. Where no sender ID is supplied (often CLI), tracking falls back to session ID. Profiles never share state.
- **Failures:** interrupted, failed, empty or uncorrelated replies do not advance the reply clock. A first failed exchange cannot create a fictitious successful baseline. Clock rollback, missing timestamps, invalid config/state, or unsupported hooks lead to omission rather than invented elapsed time or a blocked chat.
- **Completion versus delivery:** the reply timestamp marks successful model-turn completion with a matching transcript tail. It is not a Discord/Telegram delivery receipt. A network delivery failure or output-transforming plugin can therefore cause a conservative omission or a slight difference from what was actually displayed.
- **Automation:** child/cron contexts are excluded by normal scope/platform gates. Known Hermes async-delegation, process-notification and summary envelopes are conservatively ignored. The public hook currently does not expose the gateway's `MessageEvent.internal` flag, so this is not a universal classifier for future/custom autonomous wake messages. A literal user quotation starting with a reserved envelope may also be omitted.
- **Concurrency:** pending observations are bounded, thread-locked and correlated by profile/session/turn ID; stale completions cannot overwrite newer exchanges. Normal Hermes session serialization is assumed. Two separate processes intentionally writing the same profile/sender concurrently are not a supported conversation topology.

## Tests

Run the entire standalone suite with a temporary HOME/profile and a scrubbed credential environment:

```bash
python3 scripts/run_tests.py \
  --hermes-root /path/to/hermes-agent \
  --python /path/to/hermes-agent/venv/bin/python
```

Optionally add `--noctuary-root /path/to/hermes-noctuary` to exercise the real Noctuary packet parser with timing notes before and after its recall block.

Coverage includes strict threshold boundaries, midnight/year changes, both London DST transitions, clock rollback, restart and sender continuity across new sessions, failed/empty/interrupted turns, stable repeated-hook output, stale completions, profile isolation, configuration validation and malformed state. The real Hermes test discovers the installed plugin and sends requests to an **offline localhost fixture**, checking the actual suffix, exact DB sidecar replay, unchanged visible messages/system prompt and suppression of a prompt follow-up. No production conversation or paid model is used for testing.

## Disable or roll back

Remove `time_awareness` from the chosen profile's enabled list (or use that profile's plugin disable command), then restart only its gateway. Leave the recorded state if you may re-enable it; delete only this plugin's state if deliberately starting its clock over. Previously injected notes stay in historical `api_content` until normal compaction removes them—do not rewrite history to remove them.

For a code rollback, use the previous tested Git commit with the same current conversation and timing state. A backup of the profile config and plugin installation before activation is sufficient for this additive plugin; broader companion continuity backups are sensible operational protection.
