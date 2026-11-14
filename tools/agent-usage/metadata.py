"""Project JSON metadata without decoding message bodies or tool arguments."""

import json
import re

TOKEN = re.compile(
    r'\s*("(?:[^"\\]|\\.)*"|[{}\[\]:,]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)'
)


def project(text, fields):
    """Decode only whitelisted paths; skip all other values as opaque tokens."""
    tokens = iter(TOKEN.finditer(text))

    def next_token():
        return next(tokens).group(1)

    def skip(token):
        if token not in ("{", "["):
            return
        depth = 1
        while depth:
            token = next_token()
            if token in ("{", "["):
                depth += 1
            elif token in ("}", "]"):
                depth -= 1

    def read(token, selected):
        if selected is True:
            if token in ("{", "["):
                skip(token)
                return None
            return json.loads(token)
        if token != "{":
            skip(token)
            return {}
        result = {}
        token = next_token()
        while token != "}":
            key = json.loads(token)
            if next_token() != ":":
                raise ValueError("invalid metadata JSON")
            value = next_token()
            if key in selected:
                result[key] = read(value, selected[key])
            else:
                skip(value)
            token = next_token()
            if token == ",":
                token = next_token()
            elif token != "}":
                raise ValueError("invalid metadata JSON")
        return result

    try:
        return read(next_token(), fields)
    except (StopIteration, json.JSONDecodeError) as error:
        raise ValueError("incomplete metadata JSON") from error
