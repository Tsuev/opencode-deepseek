"""Offline validation of advertised tool schemas; never fetch remote resources."""

from jsonschema import validators
from referencing import Registry
from referencing.exceptions import NoSuchResource


def _no_remote_schema(uri):
    raise NoSuchResource(ref=uri)


def validate_arguments(name, arguments, tools):
    matches = []
    for tool in tools:
        if isinstance(tool, dict):
            fn = tool.get('function', tool)
            if isinstance(fn, dict) and fn.get('name') == name:
                matches.append(fn)
    if len(matches) != 1:
        raise ValueError('Tool schema is missing or ambiguous; no calls were returned')
    # OpenAI allows omitting parameters; the prompt advertises the same empty
    # object schema (openai_format._tool_specs), so validate against it.
    schema = matches[0]['parameters'] if 'parameters' in matches[0] else {'type': 'object', 'properties': {}}
    try:
        cls = validators.Draft202012Validator
        if isinstance(schema, dict) and '$schema' in schema:
            cls = validators.validator_for(schema, default=None)
        if cls is None:
            raise ValueError('Unknown schema dialect')
        cls.check_schema(schema)
        cls(schema, registry=Registry(retrieve=_no_remote_schema)).validate(arguments)
    except Exception:
        # Validation details can contain commands, file contents and secrets.
        # Invalid schemas, unresolved refs and invalid values all fail closed.
        raise ValueError('Tool arguments do not satisfy the advertised schema; no calls were returned') from None
