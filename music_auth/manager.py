"""Private administrator login orchestration, independent of the KOOK transport."""

import asyncio
import copy
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .store import CredentialStore, CredentialStoreError
from .types import AudioResult, AuthError

logger = logging.getLogger("astrbot")
_SAFE_LOGIN_STAGES = frozenset(
    {
        "LOGIN_BEGIN",
        "LOGIN_POLL",
        "LOGIN_VERIFY",
        "LOGIN_SAVE",
        "QQ_CALLBACK",
        "QQ_CHECK_SIG",
        "QQ_OAUTH",
        "QQ_EXCHANGE",
        "QQ_CREDENTIAL",
        "WX_POLL",
        "NETEASE_QR",
        "NETEASE_POLL",
        "NETEASE_COOKIE",
        "NETEASE_REQUEST",
        "NETEASE_VERIFY",
    }
)


def _login_diagnostic(error, fallback):
    diagnostic = getattr(error, "diagnostic", "")
    stage = diagnostic.split(":", 1)[0] if isinstance(diagnostic, str) else ""
    return stage if stage in _SAFE_LOGIN_STAGES else fallback


@dataclass
class _PendingLogin:
    bot_id: str
    user_id: str
    generation: int
    deadline: float
    challenge: object = field(default=None, repr=False)
    cleanup: object = field(default=None, repr=False)
    task: object = field(default=None, repr=False)


class AuthManager:
    LABELS = {"qq": "QQ音乐", "netease": "网易云音乐"}

    def __init__(
        self,
        credentials_dir: Path,
        backends: dict,
        notify,
        admin_ids: set[str],
        check_interval=1800,
        notice_cooldown=21600,
        login_timeout=180,
        poll_interval=2,
        operation_timeout=30,
    ):
        self.store = CredentialStore(credentials_dir)
        self._state = self.store.load()
        self.backends = dict(backends)
        self.notify = notify
        self.admin_ids = {
            str(value).strip() for value in admin_ids if str(value).strip()
        }
        self.check_interval = max(float(check_interval), 0.01)
        self.notice_cooldown = max(float(notice_cooldown), 0)
        self.login_timeout = max(float(login_timeout), 0.01)
        self.poll_interval = max(float(poll_interval), 0.001)
        self.operation_timeout = max(float(operation_timeout), 0.01)
        self._lock = asyncio.Lock()
        self._notice_lock = asyncio.Lock()
        self._check_locks = {provider: asyncio.Lock() for provider in backends}
        self._pending = {}
        self._login_tasks = set()
        self._cleanup_tasks = set()
        self._generation = {provider: 0 for provider in backends}
        self._bots = set()
        self._loop_task = None
        self._closed = False
        self._storage_failed = False

    def _authorized(self, bot_id, user_id):
        return (
            isinstance(bot_id, str)
            and bool(bot_id.strip())
            and isinstance(user_id, str)
            and user_id in self.admin_ids
        )

    def _label(self, provider):
        return self.LABELS.get(provider, "音乐平台")

    def _current(self, provider, pending):
        return (
            not self._closed
            and not self._storage_failed
            and self._pending.get(provider) is pending
            and self._generation.get(provider) == pending.generation
        )

    def _save(self, state):
        try:
            self.store.save(state)
        except CredentialStoreError:
            self._storage_failed = True
            raise
        self._state = state

    async def _send(self, bot_id, user_id, text, qr_bytes=None):
        try:
            result = await asyncio.wait_for(
                self.notify(bot_id, user_id, text, qr_bytes), self.operation_timeout
            )
            return True, result
        except Exception:
            return False, None

    async def start_login(self, provider, method, bot_id, user_id):
        if not self._authorized(bot_id, user_id):
            return False, "只有已配置的管理员可以在私聊中登录音乐账号。"
        async with self._lock:
            if self._closed or self._storage_failed:
                return False, "账号模块当前不可用，请检查凭据存储后重载插件。"
            if provider not in self.backends:
                return False, "该平台的账号登录线路未启用。"
            pending = self._pending.get(provider)
            if pending is not None:
                return False, "该平台已有登录流程，请由发起者完成或取消后再试。"
            self._generation[provider] += 1
            pending = _PendingLogin(
                bot_id,
                user_id,
                self._generation[provider],
                time.monotonic() + self.login_timeout,
            )
            self._pending[provider] = pending
            self._bots.add(bot_id)
            pending.task = asyncio.create_task(
                self._login_flow(provider, method, pending)
            )
            self._login_tasks.add(pending.task)
            pending.task.add_done_callback(self._login_tasks.discard)
        return (
            True,
            "已启动登录流程，二维码仅发送到本次管理员私聊；请在有效期内扫码确认。",
        )

    async def _login_call(self, pending, awaitable):
        remaining = pending.deadline - time.monotonic()
        if remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise TimeoutError
        return await asyncio.wait_for(awaitable, min(remaining, self.operation_timeout))

    async def _login_flow(self, provider, method, pending):
        backend = self.backends[provider]
        label = self._label(provider)
        terminal = None
        stage = "LOGIN_BEGIN"
        try:
            challenge = await self._login_call(pending, backend.begin_login(method))
            pending.challenge = challenge
            pending.deadline = min(
                pending.deadline, time.monotonic() + max(float(challenge.expires_in), 0)
            )
            if not self._current(provider, pending):
                return
            if time.monotonic() >= pending.deadline:
                raise TimeoutError
            if not isinstance(challenge.qr_bytes, bytes) or not challenge.qr_bytes:
                terminal = f"{label}登录二维码生成失败，原账号未更改。"
                return
            sent, cleanup = await self._send(
                pending.bot_id,
                pending.user_id,
                f"{label}登录二维码：请使用所选客户端扫码，并在手机端确认。不要转发二维码。",
                challenge.qr_bytes,
            )
            if callable(cleanup):
                pending.cleanup = cleanup
            if not sent or not self._current(provider, pending):
                return
            failures = 0
            while self._current(provider, pending):
                stage = "LOGIN_POLL"
                try:
                    result = await self._login_call(
                        pending, backend.poll_login(challenge)
                    )
                except (AuthError, TimeoutError) as error:
                    failures += 1
                    retryable = (
                        isinstance(error, TimeoutError) or error.kind == "transient"
                    )
                    if (
                        not retryable
                        or failures >= 3
                        or time.monotonic() >= pending.deadline
                    ):
                        raise
                    logger.warning(
                        "[MusicAuth] login retry provider=%s stage=%s attempt=%s",
                        provider,
                        _login_diagnostic(error, stage),
                        failures,
                    )
                    await asyncio.sleep(
                        min(
                            self.poll_interval,
                            max(0, pending.deadline - time.monotonic()),
                        )
                    )
                    continue
                failures = 0
                if not self._current(provider, pending):
                    return
                if time.monotonic() >= pending.deadline:
                    raise TimeoutError
                if result.status in {"pending", "scanned"}:
                    remaining = pending.deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    await asyncio.sleep(min(self.poll_interval, remaining))
                    continue
                if result.status in {"expired", "denied"}:
                    terminal = (
                        f"{label}登录已过期或被取消，原账号未更改。"
                        "需要登录时请重新发送私聊指令。"
                    )
                    return
                if (
                    result.status != "authorized"
                    or not isinstance(result.credential, dict)
                    or not result.credential
                ):
                    terminal = f"{label}登录结果无效，原账号未更改。"
                    return
                credential = copy.deepcopy(result.credential)
                stage = "LOGIN_VERIFY"
                validity = "unknown"
                for attempt in range(3):
                    try:
                        validity = await self._login_call(
                            pending, backend.check_credentials(credential)
                        )
                    except (AuthError, TimeoutError):
                        validity = "unknown"
                    if validity in {"valid", "expired"} or attempt == 2:
                        break
                    if not self._current(provider, pending):
                        return
                    if time.monotonic() >= pending.deadline:
                        raise TimeoutError
                    await asyncio.sleep(
                        min(
                            self.poll_interval,
                            max(0, pending.deadline - time.monotonic()),
                        )
                    )
                if time.monotonic() >= pending.deadline:
                    raise TimeoutError
                if validity != "valid":
                    terminal = (
                        f"{label}登录凭据尚未通过有效性校验，"
                        "原账号未更改，请稍后重新登录。"
                    )
                    return
                async with self._lock:
                    if not self._current(provider, pending):
                        return
                    state = copy.deepcopy(self._state)
                    state["accounts"][provider] = {
                        "credential": credential,
                        "state": "valid",
                        "refresh_attempted": False,
                    }
                    stage = "LOGIN_SAVE"
                    self._save(state)
                terminal = (
                    f"{label}登录成功，凭据已加密保存在服务器。"
                    "账号线路可用；未发送或展示 Cookie。"
                )
                return
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            terminal = f"{label}登录等待超时，原账号未更改，请重新发送私聊登录指令。"
        except Exception as error:
            diagnostic = _login_diagnostic(error, stage)
            logger.warning(
                "[MusicAuth] login failed provider=%s stage=%s error=%s",
                provider,
                diagnostic,
                type(error).__name__,
            )
            if (
                provider == "netease"
                and isinstance(error, AuthError)
                and error.kind == "verification_required"
            ):
                terminal = (
                    "网易云返回 8821：需要人工完成行为验证码验证，尚未签发登录凭据。"
                    "请在网易云官方页面按提示完成安全验证，不要反复扫码。"
                    "原账号未更改。失败阶段：NETEASE_VERIFY。"
                )
            else:
                terminal = f"{label}登录未完成，原账号未更改。失败阶段：{diagnostic}。请稍后重新登录。"
        finally:
            cleanup = asyncio.create_task(
                self._finish_login(provider, backend, pending)
            )
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            current = cleanup.result()
            if interrupted:
                raise asyncio.CancelledError
            if terminal and current:
                await self._send(pending.bot_id, pending.user_id, terminal)

    async def _finish_login(self, provider, backend, pending):
        await self._cleanup_login(backend, pending)
        async with self._lock:
            current = (
                not self._closed
                and self._pending.get(provider) is pending
                and self._generation.get(provider) == pending.generation
            )
            if self._pending.get(provider) is pending:
                self._pending.pop(provider, None)
            return current

    async def _cleanup_login(self, backend, pending):
        if pending.challenge is not None:
            try:
                await asyncio.wait_for(
                    backend.cancel_login(pending.challenge), self.operation_timeout
                )
            except (Exception, asyncio.CancelledError):
                pass
            pending.challenge = None
        if pending.cleanup is not None:
            cleanup, pending.cleanup = pending.cleanup, None
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, self.operation_timeout)
            except (Exception, asyncio.CancelledError):
                pass

    async def cancel_login(self, provider, bot_id, user_id):
        if not self._authorized(bot_id, user_id):
            return False, "只有已配置的管理员可以取消登录。"
        async with self._lock:
            pending = self._pending.get(provider)
            if pending is None:
                return False, "当前没有该平台的待完成登录。"
            if (pending.bot_id, pending.user_id) != (bot_id, user_id):
                return False, "只能由同一机器人私聊中的登录发起者取消。"
            self._generation[provider] += 1
            self._pending.pop(provider, None)
            pending.task.cancel()
        await asyncio.gather(pending.task, return_exceptions=True)
        return True, "已取消该平台登录，原账号未更改。"

    async def logout(self, provider, bot_id, user_id):
        if not self._authorized(bot_id, user_id):
            return False, "只有已配置的管理员可以注销音乐账号。"
        async with self._lock:
            if self._closed or self._storage_failed:
                return False, "账号模块当前不可用，请检查凭据存储后重载插件。"
            if provider not in self.backends:
                return False, "该平台的账号登录线路未启用。"
            pending = self._pending.get(provider)
            if pending and (pending.bot_id, pending.user_id) != (bot_id, user_id):
                return False, "该平台有其他私聊发起的登录，请由发起者先取消。"
            self._generation[provider] += 1
            if pending:
                self._pending.pop(provider, None)
                pending.task.cancel()
            state = copy.deepcopy(self._state)
            state["accounts"].pop(provider, None)
            try:
                self._save(state)
            except CredentialStoreError:
                reply = (False, "凭据存储异常，未能确认注销，请修复存储后重试。")
            else:
                reply = (
                    True,
                    "已删除服务器保存的该平台登录凭据，后续仅使用其他可用线路。",
                )
        if pending:
            await asyncio.gather(pending.task, return_exceptions=True)
        return reply

    def status(self):
        if self._storage_failed:
            return "音乐账号模块：凭据存储异常，账号线路已停用。"
        lines = []
        for provider in self.backends:
            record = self._state["accounts"].get(provider)
            label = {
                "valid": "已登录",
                "expired": "登录已失效",
                "unknown": "待验证",
            }.get(record.get("state") if record else None, "未登录")
            if provider in self._pending:
                label += "，扫码登录进行中"
            lines.append(f"{self._label(provider)}：{label}")
        return "\n".join(lines) or "未启用任何账号登录线路。"

    async def start(self, bot_id):
        if (
            self._closed
            or self._storage_failed
            or not isinstance(bot_id, str)
            or not bot_id
        ):
            return
        self._bots.add(bot_id)
        await self.check_accounts()
        if not self._closed and self._loop_task is None:
            self._loop_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self):
        try:
            while not self._closed and not self._storage_failed:
                await asyncio.sleep(self.check_interval)
                await self.check_accounts()
        except asyncio.CancelledError:
            raise

    async def check_accounts(self):
        for provider in self.backends:
            if self._closed or self._storage_failed:
                return
            try:
                await self._check_provider(provider)
            except Exception:
                continue

    async def _backend_check(self, backend, credential):
        try:
            value = await asyncio.wait_for(
                backend.check_credentials(copy.deepcopy(credential)),
                self.operation_timeout,
            )
            return value if value in {"valid", "expired", "unknown"} else "unknown"
        except Exception:
            return "unknown"

    async def _check_provider(
        self,
        provider,
        *,
        force_expired=False,
        expected_generation=None,
        expected_credential=None,
    ):
        async with self._check_locks[provider]:
            async with self._lock:
                if self._closed or self._storage_failed or provider in self._pending:
                    return False
                generation = self._generation[provider]
                if (
                    expected_generation is not None
                    and generation != expected_generation
                ):
                    return False
                record = copy.deepcopy(self._state["accounts"].get(provider))
                if (
                    record
                    and expected_credential is not None
                    and record["credential"] != expected_credential
                ):
                    return record["state"] == "valid"
            if not record:
                await self._notice(provider, "missing")
                return False
            backend = self.backends[provider]
            validity = (
                "expired"
                if force_expired
                else await self._backend_check(backend, record["credential"])
            )
            if validity == "unknown":
                return False
            updated = copy.deepcopy(record)
            updated["state"] = validity
            if validity == "valid":
                updated["refresh_attempted"] = False
            elif not record.get("refresh_attempted", False):
                updated["refresh_attempted"] = True
                try:
                    refreshed = await asyncio.wait_for(
                        backend.refresh_credentials(
                            copy.deepcopy(record["credential"])
                        ),
                        self.operation_timeout,
                    )
                except Exception:
                    refreshed = None
                if (
                    isinstance(refreshed, dict)
                    and refreshed
                    and await self._backend_check(backend, refreshed) == "valid"
                ):
                    updated = {
                        "credential": copy.deepcopy(refreshed),
                        "state": "valid",
                        "refresh_attempted": False,
                    }
            async with self._lock:
                if (
                    self._closed
                    or self._storage_failed
                    or self._generation[provider] != generation
                ):
                    return False
                if updated != record:
                    state = copy.deepcopy(self._state)
                    state["accounts"][provider] = updated
                    self._save(state)
            if updated["state"] == "expired":
                await self._notice(provider, "expired")
            return updated["state"] == "valid"

    async def _notice(self, provider, reason):
        async with self._notice_lock:
            for bot_id in sorted(self._bots):
                for user_id in sorted(self.admin_ids):
                    async with self._lock:
                        if (
                            self._closed
                            or self._storage_failed
                            or provider in self._pending
                        ):
                            return
                        record = self._state["accounts"].get(provider)
                        if (
                            reason == "missing"
                            and record
                            or reason == "expired"
                            and (not record or record["state"] != "expired")
                        ):
                            return
                        key = json.dumps(
                            [provider, bot_id, user_id, reason], separators=(",", ":")
                        )
                        last = self._state["notices"].get(key, 0)
                        if time.time() - last < self.notice_cooldown:
                            continue
                    command = "#音乐登录 QQ" if provider == "qq" else "#音乐登录 网易云"
                    status = "尚未登录" if reason == "missing" else "登录已失效"
                    message = (
                        f"{self._label(provider)}账号线路{status}。"
                        f"如需启用备用取歌线路，请在本私聊发送 {command}；"
                        "不会自动发送二维码或展示 Cookie。"
                    )
                    sent, _ = await self._send(bot_id, user_id, message)
                    if sent:
                        async with self._lock:
                            if self._closed or self._storage_failed:
                                return
                            state = copy.deepcopy(self._state)
                            state["notices"][key] = time.time()
                            self._save(state)

    async def resolve_audio(self, song):
        provider = getattr(song, "platform", "")
        if provider not in self.backends or self._closed or self._storage_failed:
            return AudioResult("unavailable", reason="该平台账号线路未启用。")
        async with self._lock:
            record = copy.deepcopy(self._state["accounts"].get(provider))
            generation = self._generation[provider]
        if not record:
            return AudioResult("unavailable", reason="该平台账号尚未登录。")
        if record["state"] == "expired":
            return AudioResult("expired", reason="该平台账号登录已失效。")
        result = await self._resolve_checked(
            provider, song, record["credential"], generation
        )
        if result.status != "expired":
            return result
        try:
            refreshed = await self._check_provider(
                provider,
                force_expired=True,
                expected_generation=generation,
                expected_credential=record["credential"],
            )
        except Exception:
            refreshed = False
        if refreshed:
            async with self._lock:
                current = copy.deepcopy(self._state["accounts"].get(provider))
                if (
                    self._closed
                    or self._storage_failed
                    or self._generation[provider] != generation
                    or not current
                ):
                    return AudioResult("transient", reason="账号状态已变化，请重试。")
            result = await self._resolve_checked(
                provider, song, current["credential"], generation
            )
            if result.status == "expired":
                try:
                    await self._mark_expired(
                        provider, generation, current["credential"]
                    )
                except CredentialStoreError:
                    return AudioResult(
                        "transient", reason="账号凭据存储异常，账号线路已停用。"
                    )
            return result
        return AudioResult(
            "expired", reason="该平台账号登录已失效，请管理员在私聊重新登录。"
        )

    async def _mark_expired(self, provider, generation, credential):
        async with self._lock:
            record = self._state["accounts"].get(provider)
            if (
                self._closed
                or self._storage_failed
                or self._generation[provider] != generation
                or not record
                or record["credential"] != credential
            ):
                return
            state = copy.deepcopy(self._state)
            state["accounts"][provider]["state"] = "expired"
            state["accounts"][provider]["refresh_attempted"] = True
            self._save(state)
        await self._notice(provider, "expired")

    async def _resolve_checked(self, provider, song, credential, generation):
        result = await self._resolve(provider, song, credential)
        async with self._lock:
            record = self._state["accounts"].get(provider)
            if (
                self._closed
                or self._storage_failed
                or self._generation[provider] != generation
                or not record
                or record["credential"] != credential
            ):
                return AudioResult(
                    "transient", reason="账号状态已变化，已丢弃旧账号的取歌结果。"
                )
        return result

    async def _resolve(self, provider, song, credential):
        try:
            result = await asyncio.wait_for(
                self.backends[provider].resolve_audio(song, copy.deepcopy(credential)),
                self.operation_timeout,
            )
        except Exception:
            return AudioResult("transient", reason="账号取歌线路暂时不可用。")
        status = getattr(result, "status", None)
        url = getattr(result, "url", None)
        if (
            status == "resolved"
            and isinstance(url, str)
            and url.startswith(("https://", "http://"))
        ):
            return AudioResult("resolved", url=result.url)
        if status == "expired":
            return AudioResult("expired", reason="该平台账号登录已失效。")
        if status == "unavailable":
            return AudioResult(
                "unavailable", reason="账号无此歌曲播放权益或歌曲不可用。"
            )
        return AudioResult("transient", reason="账号取歌线路暂时不可用。")

    async def close(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._login_tasks)
            if self._loop_task:
                tasks.append(self._loop_task)
            for provider in self._generation:
                self._generation[provider] += 1
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks), return_exceptions=True)
        self._pending.clear()
        self._loop_task = None
        for backend in self.backends.values():
            try:
                await asyncio.wait_for(backend.close(), self.operation_timeout)
            except Exception:
                pass
