import posixpath
import string


def validate_segment(value: str) -> str:
    if not value or value.startswith(".") or value in (".", ".."):
        raise ValueError("Path segment must not be empty or start with a dot.")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError("Path segment must not contain slashes or '..'.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Path segment must not contain control characters.")
    return value


def validate_repo_path(path: str, base_dir: str, allowed_suffixes: tuple[str, ...]) -> str:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("Repository path must be relative and use forward slashes.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Repository path contains an unsafe path segment.")
    normalized = posixpath.normpath(path)
    base = base_dir.strip("/")
    if not base or normalized == base or not normalized.startswith(base + "/"):
        raise ValueError(f"Repository path must stay under {base}/.")
    if not normalized.lower().endswith(tuple(suffix.lower() for suffix in allowed_suffixes)):
        raise ValueError(f"Repository path must end with one of: {', '.join(allowed_suffixes)}.")
    return normalized


def validate_path_pattern(pattern: str, allowed_placeholders: set[str]) -> str:
    if not pattern or pattern.startswith("/") or "\\" in pattern:
        raise ValueError("Path pattern must be a relative repository path.")
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(pattern) if field is not None]
    except ValueError as exc:
        raise ValueError(f"Invalid path pattern: {exc}") from exc
    for field in fields:
        if not field or field.isdigit():
            raise ValueError("Positional placeholders are not allowed in path patterns.")
        if "." in field or "[" in field or "]" in field:
            raise ValueError("Attribute and index access are not allowed in path patterns.")
        if field not in allowed_placeholders:
            raise ValueError(f"Unknown path-pattern placeholder: {field}.")
    return pattern
