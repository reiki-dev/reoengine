"""Extract intentional JSON without forwarding leaked prose."""
from __future__ import annotations

import json

from ._data import Json, json_value, loads


def extract_json(text: str) -> Json:
    begin, end = text.rfind("<<<JSON_BEGIN>>>"), text.rfind("<<<JSON_END>>>")
    if begin >= 0 or end >= 0:
        if begin < 0 or end <= begin:
            raise ValueError("Incomplete JSON markers")
        return loads(text[begin + len("<<<JSON_BEGIN>>>"):end].strip())
    decoder = json.JSONDecoder()
    candidates: list[Json] = []
    index = 0
    while index < len(text):
        if text[index] == "[":
            try:
                _, length = decoder.raw_decode(text[index:])
                index += length
                continue
            except ValueError:
                pass
        if text[index] != "{":
            index += 1
            continue
        # Balance the entire outer object, respecting escaped quotes. Do not
        # accidentally select a nested object from a malformed outer object.
        start, depth, quoted, escaped = index, 0, False, False
        while index < len(text):
            character = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    index += 1
                    try:
                        value: object
                        value, length = decoder.raw_decode(text[start:index])
                        if length == index - start:
                            candidates.append(json_value(value))
                    except ValueError:
                        pass
                    break
            index += 1
        else:
            break
    if not candidates:
        raise ValueError("No valid top-level JSON object")
    return candidates[-1]
