"""Validate call/result ownership before any provider or durable-state access."""


def validate_tool_history(messages):
    pending, seen = {}, set()
    for message in messages:
        if message.tool_calls and message.role != 'assistant':
            raise ValueError('Only assistant messages may own tool calls')
        if message.tool_call_id is not None and message.role != 'tool':
            raise ValueError('Only tool results may reference a tool call')
        if message.role == 'tool':
            call = pending.pop(message.tool_call_id, None)
            if call is None:
                raise ValueError('Tool result has no pending call or was already delivered')
            if message.name is not None and message.name != call['function']['name']:
                raise ValueError('Tool result name does not match its call')
            continue
        if pending:
            raise ValueError('Tool results must complete before the next message')
        for call in message.tool_calls or []:
            identity = call['id']
            if identity in seen:
                raise ValueError('Tool call identifiers must be unique within the history')
            seen.add(identity)
            pending[identity] = call
    if pending:
        raise ValueError('Tool history contains calls without results; no request sent')
