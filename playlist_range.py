import re


_RANGE_PATTERN = re.compile(r"^(\d+)\s*(?:-|~|～|—|至)\s*(\d+)$")


def validate_playlist_range(
    text: str,
    total_songs: int,
    available_slots: int,
) -> tuple[tuple[int, int] | None, str | None]:
    """Validate a 1-based inclusive playlist range."""
    match = _RANGE_PATTERN.fullmatch(text.strip())
    if not match:
        return None, "区间格式错误，请输入类似 `201-400`"

    start, end = (int(value) for value in match.groups())
    if start < 1 or end < start or end > total_songs:
        return None, f"区间无效，请输入 1-{total_songs} 范围内的起止序号"

    selected_count = end - start + 1
    if selected_count > available_slots:
        return None, (
            f"该区间包含 {selected_count} 首，但当前队列最多还能加入 "
            f"{available_slots} 首，请缩短区间"
        )

    return (start, end), None
