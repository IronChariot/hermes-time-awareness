"""Optional coexistence test against the external Noctuary checkout."""
import copy
import pytest
from time_awareness import Settings, render_context


def test_temporal_note_does_not_hide_existing_noctuary_packet():
    passive = pytest.importorskip('noctuary.passive')
    from agent.memory_manager import build_memory_context_block, sanitize_context
    temporal = render_context(100000, 97000, 96900, Settings(user_name='Sam'))
    recall = build_memory_context_block('## Noctuary passive recall\n- [direct | gist | node fixture-node (concept)] fixture')
    for suffix in (temporal+'\n\n'+recall, recall+'\n\n'+temporal):
        history = [{'role':'user', 'content':'Actual user message.',
                    'api_content':'Actual user message.\n\n'+suffix}]
        before = copy.deepcopy(history)
        assert passive.active_recall_ids(history) == {'fixture-node'}
        assert sanitize_context(history[0]['api_content']).strip() == 'Actual user message.'
        assert history == before
