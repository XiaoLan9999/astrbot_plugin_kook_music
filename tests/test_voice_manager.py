import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import DirectFFmpegPlayer
from astrbot_plugin_kook_music.kook_voice.voice_manager import (
    GuildSession,
    VoiceManager,
)
from astrbot_plugin_kook_music.music.model import Song


class FakeVoiceClient:
    def __init__(self, token=""):
        self.token = token
        self.is_alive = True
        self.rtp_url = "rtp://fake"
        self.ssrc = 1234
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self, channel_id):
        self.connect_calls += 1
        self.is_alive = True
        return True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_alive = False

    async def reconnect(self, channel_id):
        return await self.connect(channel_id)

    async def refresh_rtp(self):
        return True


class AutoDirectPlayer(DirectFFmpegPlayer):
    def __init__(self, fail_paths=None):
        self.played = []
        self.play_headers = []
        self.play_offsets = []
        self.wait_timeouts = []
        self.fail_paths = set(fail_paths or [])
        self._playing = False

    @property
    def is_playing(self):
        return self._playing

    async def play(
        self,
        file_path,
        rtp_url,
        ssrc,
        extra_headers=None,
        start_seconds=0.0,
    ):
        self.played.append(file_path)
        self.play_headers.append(extra_headers or {})
        self.play_offsets.append(start_seconds)
        self._playing = file_path not in self.fail_paths
        return self._playing

    async def wait_until_done(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self._playing = False
        return True

    async def stop(self):
        self._playing = False


class BlockingDirectPlayer(DirectFFmpegPlayer):
    def __init__(self):
        self.played = []
        self.play_headers = []
        self.play_offsets = []
        self.wait_timeouts = []
        self._playing = False
        self._done = asyncio.Event()

    @property
    def is_playing(self):
        return self._playing

    async def play(
        self,
        file_path,
        rtp_url,
        ssrc,
        extra_headers=None,
        start_seconds=0.0,
    ):
        self.played.append(file_path)
        self.play_headers.append(extra_headers or {})
        self.play_offsets.append(start_seconds)
        self._playing = True
        self._done = asyncio.Event()
        return True

    async def wait_until_done(self, timeout=None):
        self.wait_timeouts.append(timeout)
        await self._done.wait()
        return True

    async def stop(self):
        self._playing = False
        self._done.set()

    def finish(self):
        self._playing = False
        self._done.set()


def make_song(name, downloaded=True, platform="netease"):
    return Song(
        id=name,
        name=name,
        platform=platform,
        file_path=name if downloaded else "",
    )


async def wait_for(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.001)


class VoiceManagerPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, songs, player=None, max_queue_size=20):
        manager = VoiceManager(max_queue_size=max_queue_size, streaming_mode="direct")
        manager.PLAYBACK_START_DELAY = 0
        manager.DIRECT_RECONNECT_DELAY = 0
        player = player or AutoDirectPlayer()
        voice = FakeVoiceClient()
        session = GuildSession(
            guild_id="guild",
            voice_channel_id="voice",
            text_channel_id="text",
            voice_client=voice,
            ffmpeg_player=player,
            playlist=list(songs),
        )
        manager.sessions["guild"] = session
        return manager, session, player, voice

    async def test_point_songs_and_playlist_keep_fifo_with_lazy_download(self):
        songs = [
            make_song("point-a"),
            make_song("list-1", downloaded=False),
            make_song("bad", downloaded=False),
            make_song("point-b"),
        ]
        manager, session, player, voice = self.make_manager(songs)
        downloaded = []

        async def download(song):
            downloaded.append(song.name)
            if song.name != "bad":
                song.file_path = song.name
            return song

        manager.on_download_song = download
        await manager._playback_loop("guild")

        self.assertEqual(player.played, ["point-a", "list-1", "point-b"])
        self.assertEqual(downloaded, ["list-1", "bad"])
        self.assertEqual(session.playlist, [])
        self.assertEqual(voice.connect_calls, 2)

    async def test_mixed_platform_failure_does_not_break_song_flow(self):
        songs = [
            make_song("netease-a"),
            make_song("qq-free", downloaded=False, platform="qq"),
            make_song("kg-paid", downloaded=False, platform="kugou"),
            make_song("kg-free", downloaded=False, platform="kugou"),
            make_song("netease-b"),
        ]
        manager, session, player, _ = self.make_manager(songs)
        attempted = []

        async def download(song):
            attempted.append((song.platform, song.name))
            if song.name == "qq-free":
                song.file_path = "qq-free.m4a"
            elif song.name == "kg-free":
                song.file_path = "kg-free.mp3"
            return song

        manager.on_download_song = download
        await manager._playback_loop("guild")

        self.assertEqual(
            attempted,
            [
                ("qq", "qq-free"),
                ("kugou", "kg-paid"),
                ("kugou", "kg-free"),
            ],
        )
        self.assertEqual(
            player.played,
            ["netease-a", "qq-free.m4a", "kg-free.mp3", "netease-b"],
        )
        self.assertEqual(session.playlist, [])

    async def test_stream_source_is_ready_without_download_and_passes_headers(self):
        stream_url = "https://cdn.example.test/audio.m4s?token=secret"
        headers = {
            "Referer": "https://www.bilibili.com/",
            "User-Agent": "test-agent",
        }
        song = Song(
            id="long-video",
            name="long-video",
            platform="bilibili",
            stream_url=stream_url,
            extra_headers=headers,
        )
        manager, _, player, _ = self.make_manager([song])
        download_calls = []

        async def download(queued_song):
            download_calls.append(queued_song.id)
            return queued_song

        manager.on_download_song = download
        await manager._playback_loop("guild")

        self.assertEqual(download_calls, [])
        self.assertEqual(player.played, [stream_url])
        self.assertEqual(player.play_headers, [headers])

    async def test_download_callback_may_prepare_stream_instead_of_local_file(self):
        song = make_song("lazy-long-video", downloaded=False, platform="bilibili")
        manager, session, player, _ = self.make_manager([song])
        stream_url = "https://cdn.example.test/lazy.m4s?expires=999999"
        headers = {"Referer": "https://www.bilibili.com/"}

        async def prepare(queued_song):
            queued_song.stream_url = stream_url
            queued_song.extra_headers = headers
            return queued_song

        manager.on_download_song = prepare
        await manager._playback_loop("guild")

        self.assertEqual(player.played, [stream_url])
        self.assertEqual(player.play_headers, [headers])
        self.assertEqual(session.playlist, [])

    async def test_long_and_unknown_durations_use_safe_playback_timeouts(self):
        unknown = Song(
            id="unknown",
            name="unknown",
            stream_url="https://cdn.example.test/unknown.m4s",
        )
        three_hours = Song(
            id="three-hours",
            name="three-hours",
            duration=3 * 60 * 60 * 1000,
            stream_url="https://cdn.example.test/three-hours.m4s",
        )
        manager, _, player, _ = self.make_manager([unknown, three_hours])

        await manager._playback_loop("guild")

        self.assertEqual(player.wait_timeouts, [None, 10830.0])

    async def test_stream_url_is_refreshed_before_loop_replay(self):
        song = Song(
            id="looped-stream",
            name="looped-stream",
            duration=60_000,
            platform="bilibili",
            stream_url="https://cdn.example.test/first.m4s?token=first",
            extra_headers={"Referer": "https://www.bilibili.com/"},
        )
        manager, session, player, _ = self.make_manager([song])
        session.loop_mode = 1
        prepare_calls = []

        async def prepare(queued_song):
            prepare_calls.append(queued_song.id)
            queued_song.stream_url = (
                "https://cdn.example.test/second.m4s?token=second"
            )
            queued_song.extra_headers = {
                "Referer": "https://www.bilibili.com/"
            }
            return queued_song

        async def song_started(_guild_id, _song, _queue_size, _loop_name):
            if len(player.played) == 2:
                session.loop_mode = 0

        manager.on_download_song = prepare
        manager.on_song_started = song_started

        await manager._playback_loop("guild")

        self.assertEqual(
            player.played,
            [
                "https://cdn.example.test/first.m4s?token=first",
                "https://cdn.example.test/second.m4s?token=second",
            ],
        )
        self.assertEqual(prepare_calls, ["looped-stream"])
        self.assertEqual(session.playlist, [])

    async def test_interrupted_stream_refreshes_and_resumes_once(self):
        class FailFirstCompletionPlayer(AutoDirectPlayer):
            async def wait_until_done(self, timeout=None):
                self.wait_timeouts.append(timeout)
                self._playing = False
                return len(self.wait_timeouts) > 1

        first_url = "https://cdn.example.test/first.m4s?token=first"
        second_url = "https://cdn.example.test/second.m4s?token=second"
        song = Song(
            id="resumed-stream",
            name="resumed-stream",
            duration=3 * 60 * 60 * 1000,
            platform="bilibili",
            stream_url=first_url,
            extra_headers={"Referer": "https://www.bilibili.com/"},
        )
        player = FailFirstCompletionPlayer()
        manager, session, _, _ = self.make_manager([song], player)
        prepared = []
        started = []

        async def prepare(queued_song):
            prepared.append(queued_song.id)
            queued_song.stream_url = second_url
            queued_song.extra_headers = {
                "Referer": "https://www.bilibili.com/"
            }
            return queued_song

        async def on_started(*_args):
            started.append(song.id)

        manager.on_download_song = prepare
        manager.on_song_started = on_started
        fake_time = Mock()
        fake_time.monotonic.side_effect = [100.0, 700.0, 701.0, 1301.0]
        with patch(
            "astrbot_plugin_kook_music.kook_voice.voice_manager.time",
            fake_time,
        ):
            await manager._playback_loop("guild")

        self.assertEqual(player.played, [first_url, second_url])
        self.assertEqual(player.play_offsets, [0.0, 595.0])
        self.assertEqual(prepared, ["resumed-stream"])
        self.assertEqual(started, ["resumed-stream"])
        self.assertEqual(session.playlist, [])
        self.assertNotIn(manager._STREAM_RESUME_OFFSET_KEY, song.provider_data)

    async def test_two_rapid_skips_advance_two_distinct_songs(self):
        manager, session, player, _ = self.make_manager(
            [make_song("a"), make_song("b"), make_song("c")],
            BlockingDirectPlayer(),
        )
        task = asyncio.create_task(manager._playback_loop("guild"))
        await wait_for(lambda: player.played == ["a"])

        await asyncio.gather(manager.skip("guild"), manager.skip("guild"))
        await wait_for(lambda: player.played == ["a", "c"])
        player.finish()
        await task

        self.assertEqual(player.played, ["a", "c"])
        self.assertEqual(session.pending_skips, 0)
        self.assertEqual(session.playlist, [])

    async def test_skip_overrides_single_song_loop(self):
        manager, session, player, _ = self.make_manager(
            [make_song("a"), make_song("b")], BlockingDirectPlayer()
        )
        session.loop_mode = 1
        task = asyncio.create_task(manager._playback_loop("guild"))
        await wait_for(lambda: player.played == ["a"])

        await manager.skip("guild")
        await wait_for(lambda: player.played == ["a", "b"])
        session.loop_mode = 0
        player.finish()
        await task

        self.assertEqual(player.played, ["a", "b"])
        self.assertEqual(session.playlist, [])

    async def test_skip_during_failed_download_does_not_drop_next_song(self):
        manager, session, player, _ = self.make_manager(
            [make_song("bad", downloaded=False), make_song("good")],
            BlockingDirectPlayer(),
        )
        download_started = asyncio.Event()
        release_download = asyncio.Event()

        async def download(song):
            download_started.set()
            await release_download.wait()
            raise RuntimeError("simulated download error")

        manager.on_download_song = download
        task = asyncio.create_task(manager._playback_loop("guild"))
        await download_started.wait()
        await manager.skip("guild")
        release_download.set()
        await wait_for(lambda: player.played == ["good"])
        player.finish()
        await task

        self.assertEqual(player.played, ["good"])
        self.assertEqual(session.pending_skips, 0)

    async def test_batch_enqueue_is_all_or_nothing_and_preserves_mix_order(self):
        manager, session, _, _ = self.make_manager(
            [make_song("current"), make_song("point-a")],
            BlockingDirectPlayer(),
            max_queue_size=5,
        )
        session.is_playing = True

        ok, _ = await manager.join_and_play_many(
            "token", "guild", "voice", "text", [make_song("list-1"), make_song("list-2")]
        )
        self.assertTrue(ok)
        ok, _ = await manager.join_and_play(
            "token", "guild", "voice", "text", make_song("point-b")
        )
        self.assertTrue(ok)
        before_rejected = [song.name for song in session.playlist]
        ok, _ = await manager.join_and_play_many(
            "token", "guild", "voice", "text", [make_song("overflow")]
        )

        self.assertFalse(ok)
        self.assertEqual(
            before_rejected,
            ["current", "point-a", "list-1", "list-2", "point-b"],
        )
        self.assertEqual([song.name for song in session.playlist], before_rejected)

    async def test_queued_stream_discards_signed_url_until_it_reaches_head(self):
        manager, session, _, _ = self.make_manager(
            [make_song("current")],
            BlockingDirectPlayer(),
        )
        session.is_playing = True
        queued = Song(
            id="queued-long",
            name="queued-long",
            platform="bilibili",
            stream_url="https://cdn.example.test/audio.m4s?token=soon-expired",
            extra_headers={
                "Referer": "https://www.bilibili.com/",
                "User-Agent": "test-agent",
            },
        )

        ok, message = await manager.join_and_play(
            "token",
            "guild",
            "voice",
            "text",
            queued,
        )

        self.assertTrue(ok)
        self.assertTrue(message.startswith("QUEUED:"))
        self.assertIs(session.playlist[-1], queued)
        self.assertEqual(queued.stream_url, "")
        self.assertEqual(queued.extra_headers, {})

    async def test_concurrent_first_join_creates_one_session_and_one_client(self):
        manager = VoiceManager(max_queue_size=10, streaming_mode="direct")
        created_clients = []

        def client_factory(token):
            client = FakeVoiceClient(token)
            created_clients.append(client)
            return client

        with (
            patch(
                "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
                side_effect=client_factory,
            ),
            patch(
                "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
                return_value=BlockingDirectPlayer(),
            ),
            patch.object(manager, "_start_playback_loop"),
            patch.object(manager, "_ensure_idle_check"),
        ):
            results = await asyncio.gather(
                manager.join_and_play("t", "guild", "voice", "text", make_song("a")),
                manager.join_and_play("t", "guild", "voice", "text", make_song("b")),
            )

        self.assertTrue(all(ok for ok, _ in results))
        self.assertEqual(len(created_clients), 1)
        self.assertEqual(
            [song.name for song in manager.sessions["guild"].playlist], ["a", "b"]
        )

    async def test_leave_waits_for_inflight_join_and_wins_final_state(self):
        manager = VoiceManager(max_queue_size=10, streaming_mode="direct")
        connect_started = asyncio.Event()
        release_connect = asyncio.Event()
        created_clients = []

        class GatedVoiceClient(FakeVoiceClient):
            async def connect(self, channel_id):
                self.connect_calls += 1
                connect_started.set()
                await release_connect.wait()
                self.is_alive = True
                return True

        def client_factory(token):
            client = GatedVoiceClient(token)
            created_clients.append(client)
            return client

        with (
            patch(
                "astrbot_plugin_kook_music.kook_voice.voice_manager.VoiceClient",
                side_effect=client_factory,
            ),
            patch(
                "astrbot_plugin_kook_music.kook_voice.voice_manager.create_player",
                return_value=BlockingDirectPlayer(),
            ),
            patch.object(manager, "_start_playback_loop"),
            patch.object(manager, "_ensure_idle_check"),
        ):
            join_task = asyncio.create_task(
                manager.join_and_play("t", "guild", "voice", "text", make_song("a"))
            )
            await connect_started.wait()
            leave_task = asyncio.create_task(manager.leave("guild"))
            await asyncio.sleep(0)
            self.assertFalse(leave_task.done())
            release_connect.set()
            join_result, leave_result = await asyncio.gather(join_task, leave_task)

        self.assertTrue(join_result[0])
        self.assertTrue(leave_result[0])
        self.assertNotIn("guild", manager.sessions)
        self.assertEqual(created_clients[0].disconnect_calls, 1)

    async def test_clear_playlist_deletes_downloaded_queued_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            current_path = Path(temp_dir) / "current.mp3"
            queued_a_path = Path(temp_dir) / "queued-a.mp3"
            queued_b_path = Path(temp_dir) / "queued-b.mp3"
            for path in (current_path, queued_a_path, queued_b_path):
                path.write_bytes(b"test")

            songs = [
                Song(id="current", name="current", file_path=str(current_path)),
                Song(id="a", name="a", file_path=str(queued_a_path)),
                Song(id="b", name="b", file_path=str(queued_b_path)),
            ]
            manager, session, _, _ = self.make_manager(songs)

            ok, _ = await manager.clear_playlist("guild")

            self.assertTrue(ok)
            self.assertEqual([song.name for song in session.playlist], ["current"])
            self.assertTrue(current_path.exists())
            self.assertFalse(queued_a_path.exists())
            self.assertFalse(queued_b_path.exists())

    async def test_repeated_voice_recovery_failure_does_not_leave_stuck_queue(self):
        class FailingVoiceClient(FakeVoiceClient):
            def __init__(self):
                super().__init__()
                self.is_alive = False

            async def reconnect(self, channel_id):
                self.connect_calls += 1
                self.is_alive = False
                return False

        manager, session, _, _ = self.make_manager([make_song("a")])
        session.voice_client = FailingVoiceClient()
        manager.PLAYBACK_RETRY_DELAY = 0
        manager.MAX_PLAYBACK_RETRIES = 2

        manager._start_playback_loop("guild")
        await wait_for(lambda: not session.playlist)
        await wait_for(lambda: not session.is_playing)

        self.assertEqual(session.playlist, [])
        self.assertEqual(session.voice_client.connect_calls, 3)


if __name__ == "__main__":
    unittest.main()
