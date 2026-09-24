"""Manual real-FFmpeg VoiceManager stop/resume probe using loopback RTP only.

Run: python tests/relay_stop_resume_probe.py --ffmpeg /usr/bin/ffmpeg
     [--plugins-dir /path/to/selected/plugins]
Uses generated tones, temporary files and 127.0.0.1 UDP; never KOOK or accounts.
Prints one compact JSON report and exits nonzero if any check fails. This is not
part of unittest discovery and does not assert anything about KOOK's real mixer.
"""

import argparse
import array
import asyncio
import json
import logging
import math
import re
import struct
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

GUILD = "loopback-guild"
VOICE = "loopback-voice"
TEXT = "loopback-text"
ACTOR = "synthetic-owner"
SSRC = 42
SILENCE_SECONDS = 0.4


class ProbeFailure(RuntimeError):
    pass


def require(condition, check):
    if not condition:
        raise ProbeFailure(check)


async def until(predicate, check, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise ProbeFailure(check)
        await asyncio.sleep(0.005)


def rms(raw):
    samples = array.array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return math.sqrt(sum(value * value for value in samples) / max(1, len(samples)))


async def decode(ffmpeg, directory, label, packets):
    from relay_loopback_probe import write_capture

    require(bool(packets), label + "_has_rtp")
    capture_path = directory / (label + ".ogg")
    write_capture(capture_path, packets)
    decoder = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(capture_path),
        "-f",
        "s16le",
        "-ac",
        "2",
        "-ar",
        "48000",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw, _stderr = await asyncio.wait_for(decoder.communicate(), 10)
        require(decoder.returncode == 0, label + "_opus_decodes")
        require(bool(raw), label + "_decoded_pcm_nonempty")
        return raw
    finally:
        if decoder.returncode is None:
            decoder.kill()
            await decoder.communicate()


async def run(args):
    result = {"probe": "voice_manager_relay_stop_resume", "status": "failed"}
    processes = []
    clients = []
    snapshots = {}
    starts = []
    finished = []
    manager = None
    transport = rtcp = None
    temporary = tempfile.TemporaryDirectory(prefix="kook-stop-resume-loopback-")
    directory = Path(temporary.name)
    capture = None
    spawn = asyncio.create_subprocess_exec

    async def tracked_spawn(*command, **options):
        require(command[0] == args.ffmpeg, "only_requested_ffmpeg_spawned")
        require(
            all(
                not isinstance(argument, str)
                or "://" not in argument
                or argument.startswith("rtp://127.0.0.1:")
                for argument in command
            ),
            "subprocess_network_is_loopback_only",
        )
        process = await spawn(*command, **options)
        processes.append(process)
        return process

    async def cleanup():
        nonlocal transport, rtcp
        cleanup_ok = True
        if manager is not None:
            try:
                await manager.leave_all()
            except Exception:
                cleanup_ok = False
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.communicate(), 5)
                except (Exception, asyncio.CancelledError):
                    cleanup_ok = False
        if transport is not None:
            transport.close()
            transport = None
        if rtcp is not None:
            rtcp.close()
            rtcp = None
        await asyncio.sleep(0)
        temporary.cleanup()
        result["resources_clean"] = (
            cleanup_ok
            and all(process.returncode is not None for process in processes)
            and (manager is None or not manager.sessions)
            and not directory.exists()
        )

    try:
        plugins_dir = (
            Path(args.plugins_dir).resolve()
            if args.plugins_dir
            else Path(__file__).resolve().parents[2]
        )
        sys.path.insert(0, str(plugins_dir))
        from astrbot_plugin_kook_music.kook_voice import voice_manager as manager_module
        from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer
        from astrbot_plugin_kook_music.music.model import Song

        expected_manager = (
            plugins_dir
            / "astrbot_plugin_kook_music"
            / "kook_voice"
            / "voice_manager.py"
        ).resolve()
        result["tested_manager_matches_plugins_dir"] = (
            Path(manager_module.__file__).resolve() == expected_manager
        )
        require(
            result["tested_manager_matches_plugins_dir"],
            "selected_manager_module_matches",
        )
        metadata = plugins_dir / "astrbot_plugin_kook_music" / "metadata.yaml"
        version = re.search(
            r"^version:\s*['\"]?([A-Za-z0-9_.-]{1,64})",
            metadata.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        result["tested_version"] = version[1] if version else "unknown"

        # Load the selected package first: this helper prepends its own parent.
        from relay_loopback_probe import Capture, make_tone, tone_metrics

        capture = Capture()
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: capture, local_addr=("127.0.0.1", 0)
        )
        rtcp, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        destination = (
            f"rtp://127.0.0.1:{transport.get_extra_info('sockname')[1]}"
            f"?rtcpport={rtcp.get_extra_info('sockname')[1]}"
        )

        class FakeVoiceClient:
            def __init__(self, token):
                self.token = token
                self.rtp_url = destination
                self.ssrc = SSRC
                self.is_alive = False
                self.remote_removed = False
                self.connect_calls = 0
                self.disconnect_calls = 0
                self.refresh_calls = 0
                self.reconnect_calls = 0
                clients.append(self)

            async def connect(self, channel_id):
                self.connect_calls += 1
                require(channel_id == VOICE, "known_voice_channel_only")
                require(self.connect_calls == 1, "healthy_path_connects_once")
                self.is_alive = True
                return True

            async def disconnect(self):
                self.disconnect_calls += 1
                self.is_alive = False

            async def refresh_rtp(self, *_args, **_kwargs):
                self.refresh_calls += 1
                raise ProbeFailure("healthy_path_must_not_refresh_rtp")

            async def reconnect(self, *_args, **_kwargs):
                self.reconnect_calls += 1
                raise ProbeFailure("healthy_path_must_not_reconnect")

        paths = [directory / f"tone-{index}.wav" for index in range(3)]
        make_tone(paths[0], 660, 220)
        make_tone(paths[1], 1320, 440)
        make_tone(paths[2], 660, 220)
        songs = [
            Song(
                id=f"synthetic-{index}",
                name=f"Synthetic Tone {index}",
                duration=3000,
                file_path=str(path),
                platform="synthetic",
                requester_id=ACTOR,
            )
            for index, path in enumerate(paths)
        ]
        manager = manager_module.VoiceManager(
            ffmpeg_path=args.ffmpeg,
            streaming_mode="relay",
            volume=1.0,
            auto_leave_timeout=0,
            prefetch_next=False,
        )
        manager.on_song_started = lambda _guild, song, *_args: starts.append(song.id)
        manager.on_playback_finished = lambda guild: finished.append(guild)

        def snapshot(label):
            session = manager.sessions.get(GUILD)
            require(session is not None, label + "_session_retained")
            player = session.ffmpeg_player
            require(isinstance(player, RelayFFmpegPlayer), label + "_real_relay_player")
            require(player.is_relay_running, "assert_" + label + "_relay_alive")
            require(player._relay is not None, label + "_encoder_exists")
            snapshots[label] = player._relay.pid
            require(session.voice_client.ssrc == SSRC, label + "_ssrc_retained")
            return session, player

        async def join(song):
            ok, _detail = await manager.join_and_play(
                "synthetic-loopback-token", GUILD, VOICE, TEXT, song
            )
            require(ok, song.id + "_join_accepted")
            await until(lambda: song.id in starts, song.id + "_playback_started")

        async def eof(song):
            def completed():
                session = manager.sessions.get(GUILD)
                task = manager._playback_tasks.get(GUILD)
                return (
                    song.id in starts
                    and session is not None
                    and not session.playlist
                    and not session.is_playing
                    and task is not None
                    and task.done()
                )

            await until(completed, song.id + "_natural_eof")

        with (
            patch.object(manager_module, "VoiceClient", FakeVoiceClient),
            patch.object(asyncio, "create_subprocess_exec", tracked_spawn),
        ):
            first_started = time.monotonic()
            await join(songs[0])
            session, player = snapshot("first_play")
            await until(
                lambda: player.played_seconds >= 0.9, "first_song_reaches_midplay"
            )
            ok, _detail = await manager.control(
                GUILD, "stop", actor_id=ACTOR, expected_session=session
            )
            require(ok, "stop_command_accepted")
            stop_finished = time.monotonic()
            snapshot("after_stop")
            require(
                not session.playlist and not session.is_playing,
                "stop_clears_current_queue",
            )
            require(not session.needs_relay_refresh, "stop_keeps_healthy_relay")
            await asyncio.sleep(SILENCE_SECONDS)

            second_started = time.monotonic()
            await join(songs[1])
            snapshot("second_play")
            await eof(songs[1])
            second_finished = time.monotonic()
            snapshot("after_second_eof")
            require(not session.needs_relay_refresh, "eof_keeps_healthy_relay")
            await asyncio.sleep(SILENCE_SECONDS)

            third_started = time.monotonic()
            await join(songs[2])
            snapshot("third_play")
            await eof(songs[2])
            third_finished = time.monotonic()
            snapshot("after_third_eof")
            await asyncio.sleep(0.12)
            await manager.leave_all()
            await asyncio.sleep(0.05)

            packets = list(capture.packets)
            require(len(packets) >= 200, "sufficient_loopback_rtp_packets")
            sequences = [
                struct.unpack_from("!H", packet, 2)[0] for _, packet in packets
            ]
            timestamps = [
                struct.unpack_from("!I", packet, 4)[0] for _, packet in packets
            ]
            ssrcs = {struct.unpack_from("!I", packet, 8)[0] for _, packet in packets}
            sequence_breaks = sum(
                (b - a) % 65536 != 1 for a, b in zip(sequences, sequences[1:])
            )
            timestamp_breaks = sum(
                (b - a) % 2**32 != 960 for a, b in zip(timestamps, timestamps[1:])
            )
            max_gap = max(
                (b[0] - a[0] for a, b in zip(packets, packets[1:])), default=0
            )
            require(
                len(set(snapshots.values())) == 1,
                "same_encoder_pid_across_stop_and_eof",
            )
            require(ssrcs == {SSRC}, "single_expected_ssrc")
            require(sequence_breaks == 0, "rtp_sequence_continuity")
            require(timestamp_breaks == 0, "rtp_timestamp_continuity")
            require(max_gap < 0.35, "continuous_rtp_while_silent")

            def select(begin, end):
                return [
                    (when, packet) for when, packet in packets if begin <= when <= end
                ]

            phases = {
                "first": (first_started, stop_finished + 0.08),
                "second": (second_started, second_finished + 0.08),
                "third": (third_started, third_finished + 0.08),
            }
            phase_metrics = {}
            for label, (begin, end) in phases.items():
                raw = await decode(args.ffmpeg, directory, label, select(begin, end))
                phase_metrics[label] = tone_metrics(raw)
            require(
                phase_metrics["first"]["first_opening_marker_seconds"] >= 0.25,
                "first_opening_marker_present",
            )
            require(
                phase_metrics["second"]["second_opening_marker_seconds"] >= 0.25,
                "second_opening_marker_present",
            )
            require(
                phase_metrics["third"]["first_opening_marker_seconds"] >= 0.25,
                "third_opening_marker_present",
            )
            require(
                phase_metrics["second"]["second_song_audio_seconds"] >= 2.8,
                "second_song_decodable_audio",
            )
            require(
                phase_metrics["third"]["first_song_audio_seconds"] >= 2.8,
                "third_song_decodable_audio",
            )

            silence_metrics = {}
            for label, begin, end in (
                ("after_stop", stop_finished + 0.12, second_started - 0.02),
                ("after_eof", second_finished + 0.12, third_started - 0.02),
            ):
                segment = select(begin, end)
                require(len(segment) >= 8, label + "_silent_rtp_continues")
                raw = await decode(args.ffmpeg, directory, label, segment)
                amplitude = rms(raw)
                require(amplitude < 200, label + "_decoded_silence")
                silence_metrics[label] = {
                    "packets": len(segment),
                    "pcm_rms": round(amplitude, 2),
                }

            require(len(clients) == 1, "one_voice_client_used")
            require(clients[0].connect_calls == 1, "one_initial_connect")
            require(
                clients[0].refresh_calls == 0 and clients[0].reconnect_calls == 0,
                "no_healthy_transport_replacement",
            )
            require(clients[0].disconnect_calls == 1, "leave_disconnects_once")
            require(
                starts == [song.id for song in songs],
                "all_three_real_manager_playbacks_started",
            )
            require(len(finished) == 3, "stop_and_both_natural_eofs_reported")
            result.update(
                {
                    "status": "passed",
                    "packets": len(packets),
                    "encoder_pid_count": len(set(snapshots.values())),
                    "ssrc_count": len(ssrcs),
                    "rtp_sequence_discontinuities": sequence_breaks,
                    "rtp_timestamp_discontinuities": timestamp_breaks,
                    "max_packet_gap_seconds": round(max_gap, 3),
                    "second_audio_seconds": phase_metrics["second"][
                        "second_song_audio_seconds"
                    ],
                    "third_audio_seconds": phase_metrics["third"][
                        "first_song_audio_seconds"
                    ],
                    "opening_marker_seconds": [
                        phase_metrics["first"]["first_opening_marker_seconds"],
                        phase_metrics["second"]["second_opening_marker_seconds"],
                        phase_metrics["third"]["first_opening_marker_seconds"],
                    ],
                    "silence": silence_metrics,
                }
            )
    except Exception as error:
        result["error"] = (
            str(error) if isinstance(error, ProbeFailure) else type(error).__name__
        )
    finally:
        await cleanup()
        result["connect_calls"] = sum(client.connect_calls for client in clients)
        result["refresh_calls"] = sum(client.refresh_calls for client in clients)
        result["reconnect_calls"] = sum(client.reconnect_calls for client in clients)
        result["disconnect_calls"] = sum(client.disconnect_calls for client in clients)
        result["subprocesses"] = len(processes)
        if not result["resources_clean"]:
            result["status"] = "failed"
            result.setdefault("error", "resource_cleanup_failed")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ffmpeg", required=True, help="Explicit FFmpeg executable path"
    )
    parser.add_argument(
        "--plugins-dir", help="Parent directory of the plugin version under test"
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    report = asyncio.run(run(arguments))
    print(json.dumps(report, separators=(",", ":")))
    raise SystemExit(0 if report["status"] == "passed" else 1)
