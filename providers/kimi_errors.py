"""Map documented Kimi Connect errors without exposing server messages."""

import base64

from .access import ProviderRejected


def _reason(encoded):
    """Read only field 1 (Reason) from bounded common.error.v1.ErrorDetail."""
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise ValueError('Invalid error detail')
    body = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)

    def varint(offset):
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(body):
                raise ValueError('Truncated error detail')
            byte = body[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, offset
        raise ValueError('Invalid error varint')

    offset, reason = 0, None
    while offset < len(body):
        tag, offset = varint(offset)
        field, wire = tag >> 3, tag & 7
        if field == 0 or field == 1 and wire != 0:
            raise ValueError('Invalid error field')
        if wire == 0:
            value, offset = varint(offset)
            if field == 1:
                if reason is not None:
                    raise ValueError('Duplicate error reason')
                reason = value
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        elif wire == 2:
            size, offset = varint(offset)
            offset += size
        else:
            raise ValueError('Unsupported error detail')
        if offset > len(body):
            raise ValueError('Truncated error detail')
    return reason


def connect_rejection(error):
    if not isinstance(error, dict):
        return ProviderRejected()
    code = error.get('code')
    if code == 'unauthenticated':
        return ProviderRejected('session_expired')
    if code == 'permission_denied':
        return ProviderRejected('access_denied')
    if code == 'resource_exhausted':
        details = error.get('details', [])
        if not isinstance(details, list) or len(details) > 16:
            return ProviderRejected()
        try:
            reasons = [_reason(d.get('value')) for d in details if isinstance(d, dict)
                       and d.get('type') == 'common.error.v1.ErrorDetail']
        except (ValueError, TypeError):
            return ProviderRejected()
        if len(reasons) == 1:
            if reasons[0] == 117:  # SERVER_OVERLOADED_FOR_FREE_USER
                return ProviderRejected('capacity_unavailable')
            if reasons[0] in (112, 116, 120, 121, 122, 123, 210, 212):
                return ProviderRejected('quota_exceeded')
    return ProviderRejected()
