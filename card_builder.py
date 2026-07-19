"""
KOOK 卡片消息构建器。
使用 AstrBot 内置的 KOOK 卡片消息类型构建精美的播放信息。
"""
import time

from .music.model import Song


WATERMARK_TEXT = "[Powered By XiaoLan9999](https://XiaoLan9999.net)"
_PLATFORM_LABELS = {
    "netease": "网易云音乐",
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "migu": "咪咕音乐",
    "baidu": "百度音乐",
    "bilibili": "哔哩哔哩",
}


def _platform_label(platform: str) -> str:
    return _PLATFORM_LABELS.get(platform, platform or "未知")


def _append_watermark(modules: list[dict]):
    """Append the small footer watermark shared by all cards."""
    modules.append({
        "type": "context",
        "elements": [{
            "type": "kmarkdown",
            "content": WATERMARK_TEXT,
        }]
    })


def _normalize_duration_ms(duration: int) -> int:
    """返回毫秒时长，兼容少量来源返回秒数的情况。"""
    try:
        duration_ms = int(duration)
    except (TypeError, ValueError):
        return 0
    if duration_ms <= 0:
        return 0
    if duration_ms < 1000:
        return duration_ms * 1000
    return duration_ms


def _build_countdown_module(duration: int) -> dict | None:
    """构建 KOOK 原生 countdown 胶囊。

    真正发送前 kook_api 会按 KOOK 服务器时间修正时间戳。
    """
    duration_ms = _normalize_duration_ms(duration)
    if duration_ms <= 0:
        return None
    start_ms = int(time.time() * 1000) + 500
    return {
        "type": "countdown",
        "mode": "second",
        "startTime": start_ms,
        "endTime": start_ms + duration_ms,
    }


def build_now_playing_card(
    song: Song,
    queue_size: int = 0,
    loop_mode: str = "关闭",
) -> dict:
    """构建「正在播放」卡片消息"""
    modules = []

    # 标题
    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": f"🎵 正在播放：{song.name}"}
    })

    # 歌手信息
    artist_text = f"**歌手：** {song.artists}" if song.artists else ""
    duration_text = f"**时长：** {song.duration_str}" if song.duration > 0 else ""
    info_parts = [p for p in [artist_text, duration_text] if p]
    if info_parts:
        modules.append({
            "type": "section",
            "text": {
                "type": "kmarkdown",
                "content": "  |  ".join(info_parts)
            }
        })

    # 封面 + 音频
    if song.cover_url:
        modules.append({
            "type": "container",
            "elements": [{"type": "image", "src": song.cover_url}]
        })

    if song.audio_url:
        modules.append({
            "type": "audio",
            "src": song.audio_url,
            "title": song.display_name,
            "cover": song.cover_url or ""
        })

    # 倒计时
    countdown = _build_countdown_module(song.duration)
    if countdown:
        modules.append(countdown)

    modules.append({"type": "divider"})

    # 底部信息
    context_elements = []
    if song.platform:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"来源：**{_platform_label(song.platform)}**"
        })
    if song.requester_name:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"点歌：**{song.requester_name}**"
        })
    context_elements.append({
        "type": "kmarkdown",
        "content": f"队列：**{queue_size}** 首 | 循环：**{loop_mode}**"
    })
    if context_elements:
        modules.append({"type": "context", "elements": context_elements})

    _append_watermark(modules)

    # 操作按钮
    modules.append({
        "type": "action-group",
        "elements": [
            {
                "type": "button",
                "theme": "primary",
                "text": {"type": "plain-text", "content": "⏭ 下一首"},
                "value": "kook_music_next",
                "click": "return-val"
            },
            {
                "type": "button",
                "theme": "warning",
                "text": {"type": "plain-text", "content": "🔁 循环"},
                "value": "kook_music_loop",
                "click": "return-val"
            },
            {
                "type": "button",
                "theme": "danger",
                "text": {"type": "plain-text", "content": "🗑 清空"},
                "value": "kook_music_clear",
                "click": "return-val"
            },
        ]
    })

    card = {
        "type": "card",
        "theme": "secondary",
        "size": "lg",
        "color": "#7B68EE",
        "modules": modules
    }
    return card


def build_queued_card(
    song: Song,
    queue_size: int = 0,
    loop_mode: str = "关闭",
) -> dict:
    """构建「已加入队列」卡片消息"""
    modules = []

    # 标题
    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": f"📥 已加入队列：{song.name}"}
    })

    # 歌手信息
    info_parts = []
    if song.artists:
        info_parts.append(f"**歌手：** {song.artists}")
    if song.duration > 0:
        info_parts.append(f"**时长：** {song.duration_str}")
    info_parts.append(f"**队列位置：** 第 {queue_size} 首")
    if info_parts:
        modules.append({
            "type": "section",
            "text": {
                "type": "kmarkdown",
                "content": "  |  ".join(info_parts)
            }
        })

    modules.append({"type": "divider"})

    # 底部信息
    context_elements = []
    if song.requester_name:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"点歌：**{song.requester_name}**"
        })
    context_elements.append({
        "type": "kmarkdown",
        "content": f"队列：**{queue_size}** 首 | 循环：**{loop_mode}**"
    })
    if context_elements:
        modules.append({"type": "context", "elements": context_elements})

    _append_watermark(modules)

    card = {
        "type": "card",
        "theme": "info",
        "size": "lg",
        "color": "#4ECDC4",
        "modules": modules
    }
    return card


def build_search_result_card(
    songs: list[Song],
    title: str = "搜索结果",
) -> dict:
    """构建搜索结果列表卡片"""
    modules = []

    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": f"🔍 {title}"}
    })
    modules.append({"type": "divider"})

    lines = []
    for i, song in enumerate(songs, 1):
        lines.append(
            f"**{i}.** [{_platform_label(song.platform)}] "
            f"{song.name} - {song.artists}"
        )
    modules.append({
        "type": "section",
        "text": {"type": "kmarkdown", "content": "\n".join(lines)}
    })

    modules.append({"type": "divider"})
    modules.append({
        "type": "context",
        "elements": [{
            "type": "kmarkdown",
            "content": "请回复 **序号** 选择歌曲，如 `1`"
        }]
    })

    _append_watermark(modules)

    card = {
        "type": "card",
        "theme": "info",
        "size": "lg",
        "modules": modules
    }
    return card


def build_queue_card(
    playlist: list[Song],
    loop_mode: str = "关闭",
) -> dict:
    """构建播放队列卡片"""
    modules = []

    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": "📋 播放队列"}
    })
    modules.append({"type": "divider"})

    if not playlist:
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown", "content": "队列为空"}
        })
    else:
        # KOOK 单条卡片最多 50 个模块。大型队列仅展示前 100 首，
        # 保证卡片稳定可发送；完整数量仍在底部显示。
        visible_playlist = playlist[:100]
        lines = []
        for i, song in enumerate(visible_playlist):
            prefix = "▶" if i == 0 else f"{i + 1}"
            lines.append(f"**{prefix}.** {song.display_name}")
        for start in range(0, len(lines), 10):
            modules.append({
                "type": "section",
                "text": {"type": "kmarkdown", "content": "\n".join(lines[start:start + 10])}
            })
        if len(playlist) > len(visible_playlist):
            modules.append({
                "type": "section",
                "text": {
                    "type": "kmarkdown",
                    "content": (
                        f"_队列较长，仅展示前 {len(visible_playlist)} 首；"
                        f"其余 {len(playlist) - len(visible_playlist)} 首仍会正常播放。_"
                    ),
                },
            })

    modules.append({"type": "divider"})
    modules.append({
        "type": "context",
        "elements": [{
            "type": "kmarkdown",
            "content": f"共 **{len(playlist)}** 首 | 循环模式：**{loop_mode}**"
        }]
    })

    _append_watermark(modules)

    card = {
        "type": "card",
        "theme": "secondary",
        "size": "lg",
        "color": "#7B68EE",
        "modules": modules
    }
    return card


def build_import_result_card(
    total: int,
    playlist_id: str,
    queue_size: int = 0,
    requester_name: str = "",
    platform: str = "",
) -> dict:
    """构建歌单导入结果卡片"""
    modules = []

    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": f"📥 歌单导入完成"}
    })

    info_parts = [
        f"**导入歌曲：** {total} 首",
        f"**队列总数：** {queue_size} 首",
    ]
    if platform:
        source = "网易云音乐" if platform == "djradio" else _platform_label(platform)
        info_parts.insert(0, f"**来源：** {source}")
    modules.append({
        "type": "section",
        "text": {"type": "kmarkdown", "content": "  |  ".join(info_parts)}
    })

    modules.append({"type": "divider"})

    context_elements = []
    context_elements.append({
        "type": "kmarkdown",
        "content": f"歌单 ID：**{playlist_id}**"
    })
    if requester_name:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"操作人：**{requester_name}**"
        })
    modules.append({"type": "context", "elements": context_elements})

    _append_watermark(modules)

    card = {
        "type": "card",
        "theme": "success",
        "size": "lg",
        "color": "#4ECDC4",
        "modules": modules
    }
    return card


def build_bilibili_playing_card(
    song: Song,
    queue_size: int = 0,
    loop_mode: str = "关闭",
) -> dict:
    """构建 B站视频播放卡片"""
    modules = []

    # 标题
    modules.append({
        "type": "header",
        "text": {"type": "plain-text", "content": f"🎬 正在播放：{song.name}"}
    })

    # UP主信息
    info_parts = []
    if song.artists:
        info_parts.append(f"**UP主：** {song.artists}")
    if song.duration > 0:
        info_parts.append(f"**时长：** {song.duration_str}")
    if info_parts:
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown", "content": "  |  ".join(info_parts)}
        })

    # 封面
    if song.cover_url:
        modules.append({
            "type": "container",
            "elements": [{"type": "image", "src": song.cover_url}]
        })

    # 倒计时
    countdown = _build_countdown_module(song.duration)
    if countdown:
        modules.append(countdown)

    modules.append({"type": "divider"})

    # 底部信息
    context_elements = []
    # B站链接
    if song.id:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"来源：**Bilibili** [查看视频](https://www.bilibili.com/video/{song.id}/)"
        })
    if song.requester_name:
        context_elements.append({
            "type": "kmarkdown",
            "content": f"点歌：**{song.requester_name}**"
        })
    context_elements.append({
        "type": "kmarkdown",
        "content": f"队列：**{queue_size}** 首 | 循环：**{loop_mode}**"
    })
    if context_elements:
        modules.append({"type": "context", "elements": context_elements})

    _append_watermark(modules)

    # 操作按钮
    modules.append({
        "type": "action-group",
        "elements": [
            {
                "type": "button",
                "theme": "primary",
                "text": {"type": "plain-text", "content": "⏭ 下一首"},
                "value": "kook_music_next",
                "click": "return-val"
            },
            {
                "type": "button",
                "theme": "warning",
                "text": {"type": "plain-text", "content": "🔁 循环"},
                "value": "kook_music_loop",
                "click": "return-val"
            },
            {
                "type": "button",
                "theme": "danger",
                "text": {"type": "plain-text", "content": "🗑 清空"},
                "value": "kook_music_clear",
                "click": "return-val"
            },
        ]
    })

    card = {
        "type": "card",
        "theme": "secondary",
        "size": "lg",
        "color": "#FB7299",  # B站主题色
        "modules": modules
    }
    return card
