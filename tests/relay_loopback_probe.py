"""Synthetic loopback only: compare legacy relay with the current PCM relay.

Run manually with --ffmpeg PATH [--mode legacy|current]. No KOOK credentials,
external network, or real music are used. JSON metrics are printed to stdout.
"""
import argparse
import asyncio
import array
import json
import importlib.util
import math
from pathlib import Path
import struct
import sys
import tempfile
import time
import wave


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Capture(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets = []

    def datagram_received(self, data, addr):
        if len(data) >= 12 and data[0] >> 6 == 2 and data[1] & 127 == 100:
            self.packets.append((time.monotonic(), data))


def make_tone(path, marker, body):
    frames = array.array("h")
    for i in range(48000 * 3):
        frequency = marker if i < 48000 * 0.35 else body
        amplitude = 0.35 if i < 48000 * 0.35 else 0.1
        value = int(32767 * amplitude * math.sin(2 * math.pi * frequency * i / 48000))
        frames.extend((value, value))
    if sys.byteorder != "little":
        frames.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(frames.tobytes())


def ogg_page(payload, sequence, granule, flags=0):
    segments = [255] * (len(payload) // 255) + [len(payload) % 255]
    page = bytearray(
        b"OggS\x00" + bytes([flags]) + struct.pack("<QII", granule, 1, sequence)
        + b"\x00" * 4 + bytes([len(segments)]) + bytes(segments) + payload
    )
    checksum = 0
    for byte in page:
        checksum ^= byte << 24
        for _ in range(8):
            checksum = ((checksum << 1) ^ (0x04C11DB7 if checksum & 0x80000000 else 0)) & 0xFFFFFFFF
    page[22:26] = struct.pack("<I", checksum)
    return page


def write_capture(path, packets):
    with path.open("wb") as handle:
        handle.write(ogg_page(b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 0, 48000, 0, 0), 0, 0, 2))
        handle.write(ogg_page(b"OpusTags" + struct.pack("<II", 0, 0), 1, 0))
        for index, (_, packet) in enumerate(packets):
            offset = 12 + (packet[0] & 15) * 4
            if packet[0] & 16:
                offset += 4 + struct.unpack_from("!H", packet, offset + 2)[0] * 4
            payload = packet[offset:]
            if packet[0] & 32:
                payload = payload[:-packet[-1]]
            handle.write(ogg_page(payload, index + 2, (index + 1) * 960, 4 if index + 1 == len(packets) else 0))


def tone_metrics(raw):
    samples = array.array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    samples = samples[::2]
    frequencies = (220, 440, 660, 1320)
    labels = []
    for offset in range(0, len(samples) - 480, 480):
        chunk = samples[offset:offset + 480]
        if sum(value * value for value in chunk) / 480 < 10000:
            labels.append(0)
            continue
        powers = []
        for frequency in frequencies:
            coefficient = 2 * math.cos(2 * math.pi * frequency / 48000)
            a = b = 0.0
            for value in chunk:
                current = value + coefficient * a - b
                b, a = a, current
            powers.append(a * a + b * b - coefficient * a * b)
        labels.append(frequencies[powers.index(max(powers))])
    first_body = [i for i, label in enumerate(labels) if label == 220]
    second_marker = [i for i, label in enumerate(labels) if label == 1320]
    return {
        "first_opening_marker_seconds": round(labels.count(660) * 0.01, 3),
        "second_opening_marker_seconds": round(labels.count(1320) * 0.01, 3),
        "first_song_audio_seconds": round((labels.count(660) + labels.count(220)) * 0.01, 3),
        "second_song_audio_seconds": round((labels.count(1320) + labels.count(440)) * 0.01, 3),
        "decoded_transition_gap_seconds": round((min(second_marker) - max(first_body) - 1) * 0.01, 3) if first_body and second_marker else None,
        "decoded_seconds": round(len(samples) / 48000, 3),
    }


async def kill(process):
    if process and process.returncode is None:
        process.kill()
    if process:
        await process.communicate()


async def legacy(ffmpeg, destination, songs, processes):
    loop = asyncio.get_running_loop()
    temporary, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    port = temporary.get_extra_info("sockname")[1]
    temporary.close()
    relay = await asyncio.create_subprocess_exec(
        ffmpeg, "-f", "mpegts", "-fflags", "nobuffer", "-probesize", "32768", "-analyzeduration", "0",
        "-loglevel", "warning", "-nostats", "-i", f"udp://127.0.0.1:{port}?overrun_nonfatal=1&fifo_size=50&timeout=0",
        "-map", "0:a:0", "-c:a", "copy", "-f", "tee", f"[select=a:f=rtp:ssrc=42:payload_type=100]{destination}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    processes.append(relay)
    await asyncio.sleep(1.5)
    for song in songs:
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-re", "-nostats", "-loglevel", "warning", "-i", str(song),
            "-acodec", "libopus", "-ab", "128k", "-filter:a", "volume=1.0", "-ac", "2", "-ar", "48000",
            "-f", "mpegts", f"udp://127.0.0.1:{port}?pkt_size=1316",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(process)
        await process.communicate()
    await asyncio.sleep(0.2)


async def run(args):
    if args.player_module:
        spec = importlib.util.spec_from_file_location("kook_relay_probe_player", args.player_module)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        RelayFFmpegPlayer = module.RelayFFmpegPlayer
    else:
        from astrbot_plugin_kook_music.kook_voice.ffmpeg_player import RelayFFmpegPlayer
    loop = asyncio.get_running_loop()
    capture = Capture()
    transport, _ = await loop.create_datagram_endpoint(lambda: capture, local_addr=("127.0.0.1", 0))
    rtcp, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    destination = f'rtp://127.0.0.1:{transport.get_extra_info("sockname")[1]}?rtcpport={rtcp.get_extra_info("sockname")[1]}'
    processes = []
    player = None
    try:
        with tempfile.TemporaryDirectory(prefix="kook-relay-loopback-") as temporary:
            directory = Path(temporary)
            songs = [directory / "first.wav", directory / "second.wav"]
            make_tone(songs[0], 660, 220)
            make_tone(songs[1], 1320, 440)
            if args.mode == "legacy":
                await legacy(args.ffmpeg, destination, songs, processes)
            else:
                player = RelayFFmpegPlayer(args.ffmpeg, volume=1.0)
                if not await player.start_relay(destination, 42):
                    raise RuntimeError("relay did not start")
                for song in songs:
                    if not await player.play(str(song)):
                        raise RuntimeError("song did not start")
                    if not await player.wait_until_done(timeout=10):
                        raise RuntimeError("song did not complete")
                await asyncio.sleep(0.2)
                await player.stop_relay()
            for process in processes:
                await kill(process)
            ogg = directory / "captured.ogg"
            write_capture(ogg, capture.packets)
            decoder = await asyncio.create_subprocess_exec(
                args.ffmpeg, "-v", "error", "-i", str(ogg), "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            processes.append(decoder)
            raw, stderr = await decoder.communicate()
            if decoder.returncode:
                raise RuntimeError(stderr.decode(errors="replace"))
            packets = capture.packets
            timestamps = [struct.unpack_from("!I", packet, 4)[0] for _, packet in packets]
            sequences = [struct.unpack_from("!H", packet, 2)[0] for _, packet in packets]
            result = {
                "mode": args.mode,
                "packets": len(packets),
                "rtp_sequence_discontinuities": sum((b - a) % 65536 != 1 for a, b in zip(sequences, sequences[1:])),
                "rtp_timestamp_discontinuities": sum((b - a) % 2**32 != 960 for a, b in zip(timestamps, timestamps[1:])),
                "max_packet_gap_seconds": round(max((b[0] - a[0] for a, b in zip(packets, packets[1:])), default=0), 3),
                **tone_metrics(raw),
            }
            print(json.dumps(result, indent=2))
    finally:
        if player:
            await player.stop_relay()
        for process in processes:
            await kill(process)
        transport.close()
        rtcp.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--mode", choices=("legacy", "current"), default="current")
    parser.add_argument("--player-module", help="Optional standalone player module for isolated server validation")
    asyncio.run(run(parser.parse_args()))
