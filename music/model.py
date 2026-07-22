from dataclasses import dataclass, field


@dataclass
class Song:
    """歌曲数据模型"""

    id: str
    """歌曲 ID"""

    name: str = ""
    """歌曲名称"""

    artists: str = ""
    """歌手/艺人"""

    duration: int = 0
    """时长（毫秒）"""

    audio_url: str = ""
    """音频播放 URL"""

    cover_url: str = ""
    """封面图 URL"""

    file_path: str = ""
    """本地缓存文件路径"""

    stream_url: str = ""
    """仅供当前播放使用的临时 HTTPS 音频流地址"""

    platform: str = ""
    """来源平台"""

    requester_id: str = ""
    """点歌用户 ID"""

    requester_name: str = ""
    """点歌用户昵称"""

    extra_headers: dict = field(default_factory=dict)
    """下载或流式读取时的额外请求头（如 B站防盗链 Referer）"""

    unplayable_reason: str = ""
    """平台明确返回不可播放时的原因（如会员或版权限制）"""

    provider_data: dict = field(default_factory=dict)
    """平台解析器刷新播放地址所需的稳定元数据"""

    @property
    def display_name(self) -> str:
        """用于显示的歌曲名"""
        if self.artists:
            return f"{self.name} - {self.artists}"
        return self.name

    @property
    def playback_source(self) -> str:
        """当前可交给 FFmpeg 的本地文件或临时流地址。"""
        return self.file_path or self.stream_url

    @property
    def duration_str(self) -> str:
        """格式化时长"""
        if self.duration <= 0:
            return "未知"
        total_seconds = self.duration // 1000
        hours = total_seconds // 3600
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours}:{minutes % 60:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
