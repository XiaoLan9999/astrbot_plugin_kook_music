"""Real-FFmpeg seek and cooldown probe using synthetic loopback RTP only.

Run with --ffmpeg PATH [--plugins-dir PATH]. Never contacts KOOK or accounts.
Not part of unittest discovery; verifies decoded audio, not real KOOK playback.
"""

import argparse
import array
import asyncio
import json
import logging
import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import patch

from relay_stop_resume_probe import ProbeFailure, decode, require, until


def make_sections(path):
    samples = array.array("h")
    frequencies = (220, 440, 660, 1320)
    for index in range(48000 * 8):
        frequency = frequencies[index // (48000 * 2)]
        sample = int(8000 * math.sin(2 * math.pi * frequency * index / 48000))
        samples.extend((sample, sample))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(samples.tobytes())


async def run(args):
    result = {"probe": "voice_manager_relay_seek", "status": "failed"}
    temporary = tempfile.TemporaryDirectory(prefix="kook-seek-loopback-")
    directory = Path(temporary.name)
    processes, callbacks = [], []
    manager = transport = rtcp = None
    spawn = asyncio.create_subprocess_exec

    async def tracked_spawn(*command, **options):
        require(command[0] == args.ffmpeg, "only_requested_ffmpeg_spawned")
        require(
            all(
                not isinstance(value, str) or "://" not in value
                or value.startswith("rtp://127.0.0.1:") for value in command
            ),
            "subprocess_network_loopback_only",
        )
        process = await spawn(*command, **options)
        processes.append(process)
        return process

    try:
        plugins = Path(args.plugins_dir).resolve() if args.plugins_dir else Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(plugins))
        from astrbot_plugin_kook_music.kook_voice import voice_manager as module
        from astrbot_plugin_kook_music.music.model import Song
        require(
            Path(module.__file__).resolve() == (plugins / "astrbot_plugin_kook_music/kook_voice/voice_manager.py").resolve(),
            "selected_manager_module_matches",
        )
        from relay_loopback_probe import Capture, make_tone, tone_metrics

        capture = Capture()
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: capture, local_addr=("127.0.0.1", 0))
        rtcp, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
        destination = (
            f"rtp://127.0.0.1:{transport.get_extra_info('sockname')[1]}"
            f"?rtcpport={rtcp.get_extra_info('sockname')[1]}"
        )

        class FakeVoice:
            def __init__(self, token):
                self.token = token
                self.rtp_url = destination
                self.ssrc = 42
                self.is_alive = False
                self.remote_removed = False

            async def connect(self, _channel):
                self.is_alive = True
                return True

            async def disconnect(self):
                self.is_alive = False

            async def reconnect(self, *_args):
                raise ProbeFailure("seek_must_not_reconnect_healthy_voice")

            async def refresh_rtp(self, *_args):
                raise ProbeFailure("seek_must_not_refresh_healthy_rtp")

        first_path, next_path = directory / "sections.wav", directory / "next.wav"
        make_sections(first_path)
        make_tone(next_path, 1320, 440)
        first = Song(id="sections", duration=8000, file_path=str(first_path), requester_id="owner")
        following = Song(id="following", duration=3000, file_path=str(next_path), requester_id="owner")
        manager = module.VoiceManager(
            ffmpeg_path=args.ffmpeg, streaming_mode="relay", volume=1.0,
            auto_leave_timeout=0, prefetch_next=False, seek_cooldown_seconds=1,
        )

        def notified(_guild, song, *_args):
            session = manager.sessions["guild"]
            callbacks.append((song.id, session.playback_offset_seconds, time.monotonic()))

        manager.on_song_started = notified
        with patch.object(module, "VoiceClient", FakeVoice), patch.object(asyncio, "create_subprocess_exec", tracked_spawn):
            ok, _ = await manager.join_and_play_many("synthetic-token", "guild", "voice", "text", [first, following])
            require(ok, "join_accepted")
            await until(lambda: len(callbacks) == 1, "first_play_started")
            session = manager.sessions["guild"]
            player = session.ffmpeg_player
            encoder = player._relay.pid
            await until(lambda: player.played_seconds >= 0.7, "first_segment_progress")
            forward_requested = time.monotonic()
            ok, _ = await manager.control("guild", "seek", actor_id="owner", target_seconds=4)
            require(ok, "forward_seek_accepted")
            await until(lambda: len(callbacks) == 2, "forward_segment_started")
            require(abs(session.playback_offset_seconds - 4) < 0.01, "forward_offset_correct")
            require(session.playlist == [first, following], "seek_preserves_queue")
            burst = await asyncio.gather(*(
                manager.control("guild", "seek", actor_id="owner", target_seconds=6)
                for _ in range(6)
            ))
            require(not any(ok for ok, _ in burst), "cooldown_rejects_burst")
            await until(lambda: player.played_seconds >= 1.1, "forward_segment_progress")
            backward_requested = time.monotonic()
            ok, _ = await manager.control("guild", "seek", actor_id="owner", target_seconds=0.2)
            require(ok, "backward_seek_accepted_after_cooldown")
            await until(lambda: len(callbacks) == 3, "backward_segment_started")
            require(abs(session.playback_offset_seconds - 0.2) < 0.01, "backward_offset_correct")
            await until(lambda: player.played_seconds >= 1.1, "backward_segment_progress")
            relative_requested = time.monotonic()
            expected_relative = session.playback_offset_seconds + player.played_seconds + 4
            ok, _ = await manager.control("guild", "seek", actor_id="owner", relative_seconds=4)
            require(ok, "relative_seek_accepted")
            await until(lambda: len(callbacks) == 4, "relative_segment_started")
            require(abs(session.playback_offset_seconds - expected_relative) < 0.05, "relative_offset_uses_media_progress")
            await until(lambda: player.played_seconds >= 1.1, "relative_segment_progress")
            skip_requested = time.monotonic()
            ok, _ = await manager.control("guild", "next", actor_id="owner")
            require(ok, "skip_after_seek_accepted")
            await until(lambda: len(callbacks) == 5, "following_song_started")
            require(callbacks[-1][0] == "following", "skip_advances_exactly_one_song")
            await until(lambda: not session.playlist and not session.is_playing, "following_song_eof", timeout=8)
            await asyncio.sleep(0.15)
            require(player._relay.pid == encoder, "seek_keeps_same_encoder")
            await manager.leave_all()
            await asyncio.sleep(0.05)

            packets = list(capture.packets)
            require(len(packets) >= 200, "sufficient_loopback_rtp")
            sequences = [struct.unpack_from("!H", packet, 2)[0] for _, packet in packets]
            timestamps = [struct.unpack_from("!I", packet, 4)[0] for _, packet in packets]
            sequence_breaks = sum((b - a) % 65536 != 1 for a, b in zip(sequences, sequences[1:]))
            timestamp_breaks = sum((b - a) % 2**32 != 960 for a, b in zip(timestamps, timestamps[1:]))
            require(sequence_breaks == timestamp_breaks == 0, "rtp_continuity")
            require({struct.unpack_from("!I", packet, 8)[0] for _, packet in packets} == {42}, "same_ssrc")

            phases = {
                "initial": (callbacks[0][2], forward_requested),
                "forward": (callbacks[1][2] + 0.1, backward_requested),
                "backward": (callbacks[2][2] + 0.1, relative_requested),
                "relative": (callbacks[3][2] + 0.1, skip_requested),
                "following": (callbacks[4][2] + 0.1, time.monotonic()),
            }
            metrics = {}
            for label, (start, end) in phases.items():
                raw = await decode(args.ffmpeg, directory, label, [item for item in packets if start <= item[0] <= end])
                metrics[label] = tone_metrics(raw)
            require(metrics["initial"]["first_song_audio_seconds"] >= 0.4, "initial_220hz_present")
            require(metrics["forward"]["first_opening_marker_seconds"] >= 0.7, "forward_reaches_660hz_target")
            require(metrics["forward"]["first_song_audio_seconds"] - metrics["forward"]["first_opening_marker_seconds"] < 0.1, "forward_drops_old_220hz_buffer")
            require(metrics["backward"]["first_song_audio_seconds"] >= 0.7, "backward_reaches_220hz_target")
            require(metrics["backward"]["first_opening_marker_seconds"] < 0.1, "backward_drops_old_660hz_buffer")
            require(metrics["relative"]["first_opening_marker_seconds"] >= 0.3, "relative_seek_reaches_later_660hz_section")
            require(metrics["relative"]["second_opening_marker_seconds"] >= 0.2, "relative_seek_advances_into_1320hz_section")
            require(metrics["following"]["second_song_audio_seconds"] >= 2.5, "following_song_decodes")
            result.update(
                status="passed", packets=len(packets), encoder_pid_count=1, ssrc_count=1,
                rtp_sequence_discontinuities=sequence_breaks,
                rtp_timestamp_discontinuities=timestamp_breaks,
                cooldown_rejections=len(burst), callback_offsets=[offset for _, offset, _ in callbacks],
                decoded_phase_metrics=metrics,
            )
    except Exception as error:
        result["error"] = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
    finally:
        clean = True
        if manager is not None:
            try:
                await manager.leave_all()
            except Exception:
                clean = False
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.communicate(), 5)
                except Exception:
                    clean = False
        if transport is not None:
            transport.close()
        if rtcp is not None:
            rtcp.close()
        await asyncio.sleep(0)
        temporary.cleanup()
        result["resources_clean"] = clean and not directory.exists() and all(process.returncode is not None for process in processes)
        if not result["resources_clean"]:
            result["status"] = "failed"
            result.setdefault("error", "resource_cleanup_failed")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--plugins-dir")
    logging.basicConfig(level=logging.ERROR)
    report = asyncio.run(run(parser.parse_args()))
    print(json.dumps(report, separators=(",", ":")))
    raise SystemExit(0 if report["status"] == "passed" else 1)
