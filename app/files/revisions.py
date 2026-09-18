import re


def parse_revision_name(name: str) -> int | None:
    """Accept a positive revision number with an optional space-separated label."""
    match = re.fullmatch(r"(\d+)(?:\s+.+)?", name.strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None
