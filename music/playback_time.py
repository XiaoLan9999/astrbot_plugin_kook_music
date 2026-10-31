import re


def parse_playback_time(value: str) -> int:
    """Parse seconds, M:SS or H:MM:SS without accepting ambiguous times."""
    value = str(value).strip().replace("\uff1a", ":")
    if not re.fullmatch(r"[0-9]{1,7}(?::[0-9]{1,2}){0,2}", value):
        raise ValueError("TIME_FORMAT")
    parts = [int(part) for part in value.split(":")]
    if any(part >= 60 for part in parts[1:]):
        raise ValueError("TIME_RANGE")
    result = 0
    for part in parts:
        result = result * 60 + part
    return result
