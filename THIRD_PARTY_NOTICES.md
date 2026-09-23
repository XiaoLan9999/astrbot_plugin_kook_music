# 第三方依赖与来源

本文件记录代码和协议参考来源，不把整个历史工程重新声明为 MIT、Apache 或其他统一许可。本项目当前没有根 LICENSE；各来源已有的权利和许可声明不因此改变。

## QQMusicApi

- 项目：https://github.com/L-1124/QQMusicApi
- 固定源码：`ba95861ee9391f5b5f60f89caa8d4de4af160c8b`
- 上游作者：Luren 及 QQMusicApi contributors。
- 上游声明：GPL-3.0-or-later，见其 README、pyproject.toml 和 LICENSE。
- 使用方式：通过 `requirements.txt` 安装固定版本；`music_auth/qq_backend.py` 对其传输和登录协议作适配，QQ 登录请求参数与接口流程参考上游 `qqmusic_api/modules/login.py`。
- 未将 SDK 二进制、账号数据、二维码或第三方服务器配置放入本仓库。

## 网易云协议参考

- 项目：https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced
- 核对提交：`a8c781fd64faab17fedfd46e0615a2609307f163`
- 参考模块：`login_qr_key`、`login_qr_check`、`login_status`、`song_url_v1`、`util/crypto`。
- 另参考 https://github.com/mos9527/pyncm 的扫码登录接口说明，以及网易云官方网页实际请求。
- `music_auth/netease_backend.py` 使用独立的 aiohttp 会话和本地加密实现，不依赖公共代理，也不将个人 Cookie 发给第三方解析站。

## 历史来源与运行依赖

原工程说明提及 `astrbot_plugin_music` 与 `KO-ON-Bot`；本次发布保留原作者署名和历史，不擅自重新授权来源代码。

AstrBot、aiohttp、aiofiles、psutil、yt-dlp、cryptography、qrcode、FFmpeg 等遵循各自项目许可。安装或分发这些组件时应保留它们自己的许可说明；仓库内仅包含插件源码、文档与合成测试。
