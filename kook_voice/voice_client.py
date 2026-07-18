"""
KOOK 语音频道 WebSocket 客户端。

移植自 KO-ON-Bot 的 voiceAPI.py，重构为更健壮的异步实现。
负责连接 KOOK 语音网关、协商 RTP 传输参数。
"""

import asyncio
import json
import logging
import random
import time

import aiohttp

logger = logging.getLogger("astrbot")


class VoiceClient:
    """KOOK 语音频道 WebSocket 客户端"""

    # KOOK 语音网关 URL（兼容新旧域名）
    GATEWAY_URLS = [
        "https://www.kookapp.cn/api/v3/gateway/voice",
        "https://www.kaiheila.cn/api/v3/gateway/voice",
    ]

    def __init__(self, token: str):
        self.token = token
        self.channel_id: str = ""
        self.rtp_url: str = ""
        self.ssrc: int = 0

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connected = asyncio.Event()
        self._rtp_ready = asyncio.Event()
        self._is_exit = False
        self._tasks: list[asyncio.Task] = []

        # mediasoup Transport/Producer ID（用于关闭旧传输）
        self._transport_id: str = ""
        self._producer_id: str = ""

        # 上一次协商获得的 RTP 地址参数
        self._rtp_ip: str = ""
        self._rtp_port: int = 0
        self._rtcp_port: int = 0

        # RTP 刷新机制：在现有 WS 上重新协商 RTP 传输
        self._refreshing = False
        self._refresh_response: asyncio.Queue = asyncio.Queue()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def is_rtp_ready(self) -> bool:
        return self._rtp_ready.is_set()

    @property
    def is_alive(self) -> bool:
        """检查语音连接是否仍然存活"""
        return (
            self._connected.is_set()
            and self._rtp_ready.is_set()
            and self._ws is not None
            and not self._ws.closed
            and not self._is_exit
        )

    async def reconnect(self, channel_id: str = "", timeout: float = 15.0) -> bool:
        """重新连接语音频道（完全断开并重连，会导致 BOT 离开再进入频道）"""
        target_channel = channel_id or self.channel_id
        if not target_channel:
            return False
        await self.disconnect()
        return await self.connect(target_channel, timeout)

    async def _close_old_transport(self, timeout: float = 5.0):
        """关闭旧的 mediasoup Transport，释放服务端资源。

        必须在创建新 Transport 之前调用，否则 KOOK 的 mediasoup
        可能将旧 Producer 绑定在混音输出上，导致新 Transport 的音频被忽略。
        """
        if not self._transport_id or not self._ws or self._ws.closed:
            return

        # 先关闭 Producer
        if self._producer_id:
            close_producer = {
                "method": "closeProducer",
                "id": random.randint(1000000, 9999999),
                "request": True,
                "data": {"producerId": self._producer_id},
            }
            try:
                logger.debug(f"[KookVoice] 关闭旧 Producer: {self._producer_id}")
                await self._ws.send_json(close_producer)
                # 等待响应（不关心内容）
                await asyncio.wait_for(
                    self._refresh_response.get(), timeout=timeout
                )
            except Exception as e:
                logger.debug(f"[KookVoice] 关闭 Producer 异常（可忽略）: {e}")
            self._producer_id = ""

        # 再关闭 Transport
        close_transport = {
            "method": "closeTransport",
            "id": random.randint(1000000, 9999999),
            "request": True,
            "data": {"transportId": self._transport_id},
        }
        try:
            logger.debug(f"[KookVoice] 关闭旧 Transport: {self._transport_id}")
            await self._ws.send_json(close_transport)
            # 等待响应（不关心内容）
            await asyncio.wait_for(
                self._refresh_response.get(), timeout=timeout
            )
        except Exception as e:
            logger.debug(f"[KookVoice] 关闭 Transport 异常（可忽略）: {e}")
        self._transport_id = ""

    async def refresh_rtp(self, timeout: float = 10.0) -> bool:
        """在现有 WebSocket 上重新协商 RTP 传输参数，BOT 不会离开语音频道。

        先关闭旧的 Transport/Producer，再进行 createPlainTransport + produce
        两步协商，获取新的 RTP 地址和 SSRC。
        必须在 FFmpeg 停止后、下一首歌播放前调用。

        Returns:
            True 表示刷新成功，False 表示失败（此时应 fallback 到完整 reconnect）
        """
        if not self._ws or self._ws.closed or not self._connected.is_set():
            logger.warning("[KookVoice] WebSocket 未连接，无法刷新 RTP")
            return False

        self._rtp_ready.clear()
        old_rtp = self.rtp_url
        self.rtp_url = ""

        # 新的 SSRC
        new_ssrc = random.randint(1000, 9999)

        # 清空响应队列
        while not self._refresh_response.empty():
            try:
                self._refresh_response.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._refreshing = True
        try:
            # ---- 关键步骤：先关闭旧的 Transport/Producer ----
            # 不关闭旧 Transport 会导致 mediasoup 仍将旧 Producer 绑定在
            # 音频混音输出上，新 Transport 的音频被忽略（表现为没有声音）。
            await self._close_old_transport()

            # 短暂等待服务端清理
            await asyncio.sleep(0.5)

            # Step 1: createPlainTransport
            create_transport = {
                "data": {"comedia": True, "rtcpMux": False, "type": "plain"},
                "id": random.randint(1000000, 9999999),
                "method": "createPlainTransport",
                "request": True,
            }

            logger.debug("[KookVoice] 刷新 RTP: 发送 createPlainTransport")
            await self._ws.send_json(create_transport)

            # 等待响应
            transport_data = await asyncio.wait_for(
                self._refresh_response.get(), timeout=timeout
            )
            transport_info = transport_data.get("data", {})
            transport_id = transport_info.get("id", "")
            ip = transport_info.get("ip", "")
            port = transport_info.get("port", 0)
            rtcp_port = transport_info.get("rtcpPort", 0)

            logger.debug(
                f"[KookVoice] 刷新 RTP: Transport {ip}:{port} (rtcp: {rtcp_port})"
            )

            # Step 2: produce
            produce_payload = {
                "data": {
                    "appData": {},
                    "kind": "audio",
                    "peerId": "",
                    "rtpParameters": {
                        "codecs": [
                            {
                                "channels": 2,
                                "clockRate": 48000,
                                "mimeType": "audio/opus",
                                "parameters": {"sprop-stereo": 1},
                                "payloadType": 100,
                            }
                        ],
                        "encodings": [{"ssrc": new_ssrc}],
                    },
                    "transportId": transport_id,
                },
                "id": random.randint(1000000, 9999999),
                "method": "produce",
                "request": True,
            }

            logger.debug("[KookVoice] 刷新 RTP: 发送 produce")
            await self._ws.send_json(produce_payload)

            # 等待响应
            produce_resp = await asyncio.wait_for(
                self._refresh_response.get(), timeout=timeout
            )
            # 保存新的 Producer ID
            new_producer_id = produce_resp.get("data", {}).get("id", "")

            # 更新 RTP 参数
            self._transport_id = transport_id
            self._producer_id = new_producer_id
            self.ssrc = new_ssrc
            self.rtp_url = f"rtp://{ip}:{port}?rtcpport={rtcp_port}"
            self._rtp_ready.set()
            logger.info(
                f"[KookVoice] RTP 刷新成功: {self.rtp_url}, SSRC: {self.ssrc}"
            )
            return True

        except asyncio.TimeoutError:
            logger.error("[KookVoice] RTP 刷新超时")
            # 恢复旧值
            self.rtp_url = old_rtp
            self._rtp_ready.set()
            return False
        except Exception as e:
            logger.error(f"[KookVoice] RTP 刷新异常: {e}")
            self.rtp_url = old_rtp
            self._rtp_ready.set()
            return False
        finally:
            self._refreshing = False

    async def connect(self, channel_id: str, timeout: float = 15.0) -> bool:
        """
        连接到指定语音频道。

        Args:
            channel_id: KOOK 语音频道 ID
            timeout: 连接超时秒数

        Returns:
            是否成功连接并获取 RTP 地址
        """
        self.channel_id = channel_id
        self.rtp_url = ""
        self.ssrc = 0
        self._is_exit = False
        self._connected.clear()
        self._rtp_ready.clear()

        try:
            # 获取语音网关 URL
            gateway = await self._get_gateway(channel_id)
            if not gateway:
                logger.error("[KookVoice] 无法获取语音网关地址")
                return False

            logger.info(f"[KookVoice] 语音网关: {gateway[:60]}...")

            # 建立 WebSocket 连接
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(gateway)
            self._connected.set()

            # 启动后台任务
            ws_task = asyncio.create_task(self._ws_message_handler())
            ping_task = asyncio.create_task(self._ws_ping_loop())
            self._tasks = [ws_task, ping_task]

            # 等待 RTP 准备完成
            try:
                await asyncio.wait_for(self._rtp_ready.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error("[KookVoice] RTP 协商超时")
                await self.disconnect()
                return False

            logger.info(
                f"[KookVoice] 已连接语音频道 {channel_id}, "
                f"RTP: {self.rtp_url}, SSRC: {self.ssrc}"
            )
            return True

        except Exception as e:
            logger.error(f"[KookVoice] 连接失败: {e}")
            await self.disconnect()
            return False

    async def disconnect(self):
        """断开语音连接"""
        self._is_exit = True
        self._connected.clear()
        self._rtp_ready.clear()

        # 取消后台任务
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._tasks.clear()

        # 关闭 WebSocket
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

        # 关闭 HTTP session
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

        self.channel_id = ""
        self.rtp_url = ""
        self.ssrc = 0
        logger.info("[KookVoice] 已断开语音连接")

    async def _get_gateway(self, channel_id: str) -> str | None:
        """获取语音网关 WebSocket URL"""
        headers = {"Authorization": f"Bot {self.token}"}

        async with aiohttp.ClientSession() as session:
            for base_url in self.GATEWAY_URLS:
                try:
                    url = f"{base_url}?channel_id={channel_id}"
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            gateway_url = data.get("data", {}).get("gateway_url", "")
                            if gateway_url:
                                return gateway_url
                except Exception as e:
                    logger.debug(f"[KookVoice] 网关请求失败 ({base_url}): {e}")
                    continue

        return None

    async def _ws_message_handler(self):
        """处理 WebSocket 消息，完成 RTP 协商"""
        if not self._ws:
            return

        # 准备协商载荷（来自 KO-ON-Bot）
        self.ssrc = random.randint(1000, 9999)
        payloads = {
            "1": {
                "request": True,
                "id": random.randint(1000000, 9999999),
                "method": "getRouterRtpCapabilities",
                "data": {},
            },
            "2": {
                "data": {"displayName": ""},
                "id": random.randint(1000000, 9999999),
                "method": "join",
                "request": True,
            },
            "3": {
                "data": {"comedia": True, "rtcpMux": False, "type": "plain"},
                "id": random.randint(1000000, 9999999),
                "method": "createPlainTransport",
                "request": True,
            },
            "4": {
                "data": {
                    "appData": {},
                    "kind": "audio",
                    "peerId": "",
                    "rtpParameters": {
                        "codecs": [
                            {
                                "channels": 2,
                                "clockRate": 48000,
                                "mimeType": "audio/opus",
                                "parameters": {"sprop-stereo": 1},
                                "payloadType": 100,
                            }
                        ],
                        "encodings": [{"ssrc": self.ssrc}],
                    },
                    "transportId": "",
                },
                "id": random.randint(1000000, 9999999),
                "method": "produce",
                "request": True,
            },
        }

        # 发送第一步
        logger.debug("[KookVoice] 发送 getRouterRtpCapabilities")
        await self._ws.send_json(payloads["1"])

        step = 1
        pending_messages: list[str] = []

        try:
            async for msg in self._ws:
                if self._is_exit:
                    return

                if msg.type == aiohttp.WSMsgType.TEXT:
                    pending_messages.append(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("[KookVoice] WebSocket 错误")
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break

                # 处理待处理的消息
                while pending_messages:
                    raw = pending_messages.pop(0)
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if step == 1:
                        # 收到 RtpCapabilities → 发送 join
                        logger.debug("[KookVoice] 发送 join")
                        await self._ws.send_json(payloads["2"])
                        step = 2
                    elif step == 2:
                        # 收到 join 结果 → 发送 createPlainTransport
                        logger.debug("[KookVoice] 发送 createPlainTransport")
                        await self._ws.send_json(payloads["3"])
                        step = 3
                    elif step == 3:
                        # 收到 transport 信息 → 提取 ip/port → 发送 produce
                        transport_data = data.get("data", {})
                        transport_id = transport_data.get("id", "")
                        self._rtp_ip = transport_data.get("ip", "")
                        self._rtp_port = transport_data.get("port", 0)
                        self._rtcp_port = transport_data.get("rtcpPort", 0)

                        # 保存 transport ID
                        self._transport_id = transport_id

                        payloads["4"]["data"]["transportId"] = transport_id
                        logger.debug(
                            f"[KookVoice] Transport: {self._rtp_ip}:{self._rtp_port} "
                            f"(rtcp: {self._rtcp_port})"
                        )
                        logger.debug("[KookVoice] 发送 produce")
                        await self._ws.send_json(payloads["4"])
                        step = 4
                    elif step == 4:
                        # 收到 produce 结果 → RTP 就绪
                        # 保存 producer ID
                        self._producer_id = data.get("data", {}).get("id", "")

                        # 构建 rtp_url
                        self.rtp_url = (
                            f"rtp://{self._rtp_ip}:{self._rtp_port}"
                            f"?rtcpport={self._rtcp_port}"
                        )
                        logger.info(
                            f"[KookVoice] RTP 就绪: {self.rtp_url}"
                        )
                        self._rtp_ready.set()
                        step = 5
                    else:
                        # 已完成初始协商，处理后续消息
                        if self._refreshing:
                            # RTP 刷新正在进行，将响应路由到刷新队列
                            await self._refresh_response.put(data)
                        elif (
                            isinstance(data, dict)
                            and data.get("notification")
                            and data.get("method") == "disconnect"
                        ):
                            logger.warning(
                                "[KookVoice] 收到断开通知"
                            )

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[KookVoice] WebSocket 消息处理异常: {e}")

    async def _ws_ping_loop(self):
        """WebSocket 心跳保活"""
        try:
            # 等待连接就绪
            await self._connected.wait()

            last_ping = 0.0
            while not self._is_exit:
                await asyncio.sleep(1)
                if self._is_exit or not self._ws or self._ws.closed:
                    return

                now = time.time()
                if now - last_ping >= 30:
                    try:
                        await self._ws.ping()
                        last_ping = now
                    except Exception:
                        return
        except asyncio.CancelledError:
            return
