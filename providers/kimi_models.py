"""Explicit text model selections from Kimi's account model catalog."""

MODELS = {
    'k2d6-chat': {
        'public_id': 'kimi-instant', 'label': 'Kimi Instant · web',
        'scenario': 'SCENARIO_CHAT', 'effort': 'REASONING_EFFORT_NONE',
    },
    'k3-agent': {
        'public_id': 'kimi-k3', 'label': 'Kimi K3 · web',
        'scenario': 'SCENARIO_OK_COMPUTER', 'effort': 'REASONING_EFFORT_HIGH',
        'kimiplus_id': 'ok-computer', 'context_length': 'CONTEXT_LENGTH_L',
    },
    'k28-agent-preview': {
        'public_id': 'kimi-k2.8', 'label': 'Kimi K2.8 Preview · web',
        'scenario': 'SCENARIO_OK_COMPUTER', 'effort': 'REASONING_EFFORT_HIGH',
        'kimiplus_id': 'ok-computer', 'context_length': 'CONTEXT_LENGTH_L',
    },
}
PUBLIC_MODELS = {spec['public_id']: wire for wire, spec in MODELS.items()}


def request_payload(prompt, model, chat=None, parent=None):
    if model not in MODELS:
        raise ValueError('Unsupported direct Kimi model')
    spec = MODELS[model]
    message = {'role': 'user', 'blocks': [{'text': {'content': prompt}}],
               'scenario': spec['scenario']}
    if parent:
        message['parent_id'] = parent
    options = {'model': model, 'thinking': True, 'reasoning_effort': spec['effort'],
               'enable_plugin': False}
    if 'context_length' in spec:
        options['context_length'] = spec['context_length']
    payload = {'chat_id': chat or '', 'scenario': spec['scenario'], 'tools': [],
               'message': message, 'options': options}
    if 'kimiplus_id' in spec:
        payload['kimiplus_id'] = spec['kimiplus_id']
    return payload
