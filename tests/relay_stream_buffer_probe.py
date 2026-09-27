"""Synthetic FFmpeg PCM buffering probe: local TCP source and loopback RTP only.

Run manually with --ffmpeg PATH [--plugins-dir PLUGINS_PARENT]. This is not a
unittest-discovery test and never contacts KOOK, music providers, or accounts.
Only this process maps one temporary file to a local raw-PCM TCP input; production
HTTPS source validation is unchanged. Output is one aggregate JSON report.
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
from pathlib import Path
from unittest.mock import patch

RATE = 48000
FRAME_BYTES = 3840
SSRC = 73


class ProbeFailure(RuntimeError):
    pass


def require(condition, label):
    if not condition:
        raise ProbeFailure(label)


async def until(predicate, label, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise ProbeFailure(label)
        await asyncio.sleep(0.005)


def pcm_tone(seconds, marker, body):
    values = array.array("h")
    for index in range(int(RATE * seconds)):
        frequency = marker if index < int(RATE * 0.35) else body
        amplitude = 0.35 if index < int(RATE * 0.35) else 0.1
        value = int(
            32767 * amplitude * math.sin(2 * math.pi * frequency * index / RATE)
        )
        values.extend((value, value))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def analyze(raw):
    values = array.array("h", raw)
    if sys.byteorder != "little":
        values.byteswap()
    samples = values[::2]
    labels = []
    frequencies = (220, 440, 660, 1320)
    for offset in range(0, len(samples) - 480, 480):
        chunk = samples[offset : offset + 480]
        power = sum(value * value for value in chunk) / 480
        if power < 10000:
            labels.append(0)
            continue
        strengths = []
        for frequency in frequencies:
            coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
            a = b = 0.0
            for value in chunk:
                current = value + coefficient * a - b
                b, a = a, current
            strengths.append(a * a + b * b - coefficient * a * b)
        labels.append(frequencies[strengths.index(max(strengths))])
    audible = [index for index, label in enumerate(labels) if label]
    longest = current = 0
    if audible:
        for label in labels[audible[0] : audible[-1] + 1]:
            current = current + 1 if label == 0 else 0
            longest = max(longest, current)
    return {
        "first_marker_seconds": round(labels.count(660) * 0.01, 3),
        "second_marker_seconds": round(labels.count(1320) * 0.01, 3),
        "first_audio_seconds": round((labels.count(220) + labels.count(660)) * 0.01, 3),
        "second_audio_seconds": round(
            (labels.count(440) + labels.count(1320)) * 0.01, 3
        ),
        "silent_seconds": round(labels.count(0) * 0.01, 3),
        "internal_silence_seconds": round(longest * 0.01, 3),
        "decoded_seconds": round(len(samples) / RATE, 3),
        "pcm_rms": round(
            math.sqrt(sum(value * value for value in samples) / max(1, len(samples))), 2
        ),
    }


async def run(args):
    result = {"probe": "relay_stream_buffer", "status": "failed"}
    temporary = tempfile.TemporaryDirectory(prefix="kook-pcm-buffer-loopback-")
    directory = Path(temporary.name)
    processes = []
    writers = set()
    writer_tasks = set()
    player = server = rtp = rtcp = None
    real_spawn = asyncio.create_subprocess_exec
    allow_fill = asyncio.Event()
    partial_sent = asyncio.Event()
    pause_complete = asyncio.Event()
    source_times = {}
    background_errors = []

    async def tracked_spawn(*command, **options):
        require(command[0] == args.ffmpeg, "only_requested_ffmpeg_is_spawned")
        require(
            all(
                not isinstance(value, str)
                or "://" not in value
                or value.startswith(("rtp://127.0.0.1:", "tcp://127.0.0.1:"))
                for value in command
            ),
            "subprocess_network_is_loopback_only",
        )
        process = await real_spawn(*command, **options)
        processes.append(process)
        return process

    async def decode(label, packets):
        from relay_loopback_probe import write_capture

        require(bool(packets), label + "_rtp_exists")
        capture_file = directory / (label + ".ogg")
        write_capture(capture_file, packets)
        process = await asyncio.create_subprocess_exec(
            args.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(capture_file),
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
        raw, _ = await asyncio.wait_for(process.communicate(), 8)
        require(process.returncode == 0 and bool(raw), label + "_opus_decodes")
        return analyze(raw)

    try:
        plugins_dir = (
            Path(args.plugins_dir).resolve()
            if args.plugins_dir
            else Path(__file__).resolve().parents[2]
        )
        sys.path.insert(0, str(plugins_dir))
        from astrbot_plugin_kook_music.kook_voice import ffmpeg_player as player_module

        expected = (
            plugins_dir
            / "astrbot_plugin_kook_music"
            / "kook_voice"
            / "ffmpeg_player.py"
        )
        require(
            Path(player_module.__file__).resolve() == expected.resolve(),
            "selected_player_module_matches",
        )
        from relay_loopback_probe import Capture, make_tone

        source = pcm_tone(8, 660, 220)

        async def source_client(reader, writer):
            task = asyncio.current_task()
            writer_tasks.add(task)
            writers.add(writer)
            try:
                require(
                    "accepted" not in source_times, "one_synthetic_source_connection"
                )
                source_times["accepted"] = time.monotonic()
                writer.write(source[: RATE * 4 // 2])
                await writer.drain()
                partial_sent.set()
                await allow_fill.wait()
                writer.write(source[RATE * 4 // 2 : RATE * 4 * 4])
                await writer.drain()
                source_times["pause_start"] = time.monotonic()
                await asyncio.sleep(2.0)
                source_times["pause_end"] = time.monotonic()
                pause_complete.set()
                writer.write(source[RATE * 4 * 4 :])
                await writer.drain()
            except (ConnectionError, BrokenPipeError):
                pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                background_errors.append(
                    str(error)
                    if isinstance(error, ProbeFailure)
                    else type(error).__name__
                )
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, BrokenPipeError):
                    pass
                writers.discard(writer)
                writer_tasks.discard(task)

        server = await asyncio.start_server(source_client, "127.0.0.1", 0)
        source_url = f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        source_marker = directory / "synthetic-buffer-source.pcm"
        source_marker.write_bytes(b"synthetic local input marker")
        second = directory / "second.wav"
        make_tone(second, 1320, 440)
        original_input_args = player_module._http_input_args

        def local_input_args(source_path, extra_headers, start_seconds=0.0):
            if source_path == str(source_marker):
                require(
                    not extra_headers and not start_seconds,
                    "source_has_no_credentials_or_seek",
                )
                return [
                    "-f",
                    "s16le",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-i",
                    source_url,
                ]
            return original_input_args(source_path, extra_headers, start_seconds)

        capture = Capture()
        loop = asyncio.get_running_loop()
        rtp, _ = await loop.create_datagram_endpoint(
            lambda: capture, local_addr=("127.0.0.1", 0)
        )
        rtcp, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        destination = f"rtp://127.0.0.1:{rtp.get_extra_info('sockname')[1]}?rtcpport={rtcp.get_extra_info('sockname')[1]}"
        player = player_module.RelayFFmpegPlayer(args.ffmpeg, volume=1.0)
        with (
            patch.object(player_module, "_http_input_args", local_input_args),
            patch.object(asyncio, "create_subprocess_exec", tracked_spawn),
        ):
            require(await player.start_relay(destination, SSRC), "real_relay_started")
            relay_pid = player._relay.pid
            first_begin = time.monotonic()
            start_task = asyncio.create_task(
                player.play(
                    str(source_marker),
                    startup_buffer_seconds=3,
                    max_buffer_seconds=4,
                    rebuffer_seconds=1,
                )
            )
            try:
                await asyncio.wait_for(partial_sent.wait(), 2)
                await asyncio.sleep(0.15)
                require(not start_task.done(), "startup_waits_for_buffer_target")
                require(player.played_seconds == 0, "startup_does_not_consume_opening")
                allow_fill.set()
                require(
                    await asyncio.wait_for(start_task, 3), "buffered_first_song_started"
                )
            finally:
                if not start_task.done():
                    start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            first_ready = time.monotonic()
            initial_buffer = player.buffered_seconds
            require(initial_buffer >= 2.8, "startup_has_three_seconds_buffer")
            await asyncio.wait_for(pause_complete.wait(), 3)
            require(
                source_times["pause_end"] - source_times["pause_start"] >= 1.9,
                "source_pause_lasts_two_seconds",
            )
            await until(
                lambda: player.played_seconds >= 3.2 and player.buffered_seconds >= 1.0,
                "old_audio_still_buffered_before_stop",
            )
            require(not player.is_buffering, "sufficient_buffer_avoids_rebuffering")
            old_track = player._song
            buffered_at_stop = player.buffered_seconds
            require(buffered_at_stop <= 4.0, "pcm_queue_respects_memory_limit")
            await player.stop()
            stopped = time.monotonic()
            old_played_bytes = old_track.played_bytes
            require(
                not player.is_playing and player.buffered_seconds == 0,
                "stop_detaches_old_buffer",
            )
            await asyncio.sleep(0.25)
            second_begin = time.monotonic()
            require(
                await player.play(
                    str(second),
                    startup_buffer_seconds=0.5,
                    max_buffer_seconds=2,
                    rebuffer_seconds=0.5,
                ),
                "second_song_started",
            )
            require(await player.wait_until_done(timeout=5), "second_song_completed")
            second_end = time.monotonic()
            require(
                old_track.played_bytes == old_played_bytes,
                "old_cached_frames_never_emit_after_stop",
            )
            require(player._relay.pid == relay_pid, "same_encoder_for_both_tracks")
            await asyncio.sleep(0.1)
            await player.stop_relay()
            await asyncio.sleep(0.05)

            packets = list(capture.packets)
            require(len(packets) >= 250, "sufficient_real_rtp_packets")
            sequences = [
                struct.unpack_from("!H", packet, 2)[0] for _, packet in packets
            ]
            timestamps = [
                struct.unpack_from("!I", packet, 4)[0] for _, packet in packets
            ]
            sequence_breaks = sum(
                (b - a) % 65536 != 1 for a, b in zip(sequences, sequences[1:])
            )
            timestamp_breaks = sum(
                (b - a) % 2**32 != 960 for a, b in zip(timestamps, timestamps[1:])
            )
            ssrcs = {struct.unpack_from("!I", packet, 8)[0] for _, packet in packets}
            max_gap = max(
                (b[0] - a[0] for a, b in zip(packets, packets[1:])), default=0
            )
            require(sequence_breaks == timestamp_breaks == 0, "rtp_timeline_continuous")
            require(ssrcs == {SSRC}, "single_expected_ssrc")
            require(max_gap < 0.35, "no_rtp_delivery_gap")

            def phase(begin, end):
                return [
                    (when, packet) for when, packet in packets if begin <= when <= end
                ]

            first_metrics = await decode("first", phase(first_begin, stopped + 0.07))
            pause_metrics = await decode(
                "source-pause",
                phase(
                    max(first_ready + 0.1, source_times["pause_start"] + 0.1),
                    source_times["pause_end"] - 0.05,
                ),
            )
            silent_metrics = await decode(
                "after-stop", phase(stopped + 0.10, second_begin - 0.02)
            )
            second_metrics = await decode(
                "second", phase(second_begin, second_end + 0.07)
            )
            require(
                first_metrics["first_marker_seconds"] >= 0.25,
                "first_opening_marker_preserved",
            )
            require(
                first_metrics["internal_silence_seconds"] <= 0.04,
                "no_internal_audio_gap_with_buffer",
            )
            require(
                pause_metrics["decoded_seconds"] >= 1.0
                and pause_metrics["silent_seconds"] <= 0.04,
                "decoded_audio_continues_through_source_pause",
            )
            require(
                silent_metrics["pcm_rms"] < 200, "stop_outputs_silence_not_old_buffer"
            )
            require(
                second_metrics["second_marker_seconds"] >= 0.25,
                "second_opening_marker_preserved",
            )
            require(
                second_metrics["second_audio_seconds"] >= 2.8,
                "second_song_full_audio_decodes",
            )
            require(
                second_metrics["first_audio_seconds"] <= 0.02,
                "no_old_song_leaks_into_second",
            )
            require(not background_errors, "synthetic_source_completed_without_error")
            result.update(
                {
                    "status": "passed",
                    "packets": len(packets),
                    "initial_buffer_seconds": round(initial_buffer, 3),
                    "buffered_at_stop_seconds": round(buffered_at_stop, 3),
                    "source_pause_seconds": round(
                        source_times["pause_end"] - source_times["pause_start"], 3
                    ),
                    "pause_decoded_seconds": pause_metrics["decoded_seconds"],
                    "pause_silence_seconds": pause_metrics["silent_seconds"],
                    "first_opening_marker_seconds": first_metrics[
                        "first_marker_seconds"
                    ],
                    "second_opening_marker_seconds": second_metrics[
                        "second_marker_seconds"
                    ],
                    "first_internal_audio_gap_seconds": first_metrics[
                        "internal_silence_seconds"
                    ],
                    "second_audio_seconds": second_metrics["second_audio_seconds"],
                    "old_audio_in_second_seconds": second_metrics[
                        "first_audio_seconds"
                    ],
                    "after_stop_pcm_rms": silent_metrics["pcm_rms"],
                    "rtp_sequence_discontinuities": sequence_breaks,
                    "rtp_timestamp_discontinuities": timestamp_breaks,
                    "max_packet_gap_seconds": round(max_gap, 3),
                    "encoder_pid_count": 1,
                }
            )
    except Exception as error:
        result["error"] = (
            str(error) if isinstance(error, ProbeFailure) else type(error).__name__
        )
    finally:
        clean = True
        allow_fill.set()
        if player is not None:
            try:
                await player.stop_relay()
            except Exception:
                clean = False
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(writer_tasks):
            task.cancel()
        if writer_tasks:
            await asyncio.gather(*tuple(writer_tasks), return_exceptions=True)
        for process in processes:
            if process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.communicate(), 3)
                except Exception:
                    clean = False
        for transport in (rtp, rtcp):
            if transport is not None:
                transport.close()
        await asyncio.sleep(0)
        temporary.cleanup()
        result["resources_clean"] = (
            clean
            and not directory.exists()
            and all(process.returncode is not None for process in processes)
        )
        if not result["resources_clean"]:
            result["status"] = "failed"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--plugins-dir")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    report = asyncio.run(run(arguments))
    print(json.dumps(report, separators=(",", ":")))
    raise SystemExit(0 if report["status"] == "passed" else 1)
