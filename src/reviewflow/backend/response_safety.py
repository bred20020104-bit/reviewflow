"""Reject credential echoes before structured model output reaches the workflow."""

import json


def validate_response_text(text, key):
    """Check both wire text and JSON-decoded property names/string values.

    The workflow decodes this JSON once; checking only its wire spelling misses
    Unicode escapes. Do not include rejected values in diagnostics.
    """
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > 131072:
        raise ValueError("Invalid response text")
    if key in text:
        raise ValueError("Credential echo")
    pending = [json.loads(text)]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if key in value:
                raise ValueError("Credential echo")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
