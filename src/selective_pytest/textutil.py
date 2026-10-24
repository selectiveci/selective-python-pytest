import hashlib


def identity_bytes(native):
    text = str(native)
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")


def clean_text(value):
    return value.encode("utf-8", "replace").decode("utf-8")


def cut_utf8(raw, limit):
    clipped = raw[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def needs_short_id(native):
    raw = identity_bytes(native)
    if b"\x00" in raw or len(raw) > 255:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def short_id(native):
    raw = identity_bytes(native)
    digest = hashlib.sha256(raw).hexdigest()
    prefix = cut_utf8(raw.replace(b"\x00", b""), 190)
    return prefix + "~" + digest


def wire_id(native):
    text = str(native)
    if needs_short_id(text):
        return short_id(text)
    return text


def clean_json(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, dict):
        return {clean_text(key): clean_json(item) for key, item in value.items()}
    return value
