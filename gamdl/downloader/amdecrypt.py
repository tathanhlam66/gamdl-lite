"""
Modified from https://github.com/sn0wst0rm/st0rmMusicPlayer/blob/main/scripts/amdecrypt.py
"""

import asyncio
import io
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# Default decryption key for songs without per-sample keys (legacy AAC)
DEFAULT_SONG_DECRYPTION_KEY = b"2\xb8\xad\xe1v\x9e&\xb1\xff\xb8\x98cRy?\xc6"

# Pre-fetch key used for first sample description
PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"

# Legacy TCP wrapper address (no longer used; kept for external callers).
DEFAULT_WRAPPER_IP = "127.0.0.1:10020"


@dataclass
class SampleInfo:
    """Information about a single audio sample."""

    data: bytes
    duration: int
    desc_index: int
    iv: bytes = b""  # Per-sample IV from senc; empty if constant IV is used
    subsamples: List[tuple] = field(
        default_factory=list
    )  # list of (clear_bytes, encrypted_bytes) tuples


@dataclass
class EncryptionInfo:
    """Encryption scheme info extracted from sinf/schm + sinf/schi/tenc."""

    scheme_type: str = "cbcs"
    per_sample_iv_size: int = 0
    constant_iv: bytes = b""
    kid: bytes = b""


@dataclass
class SongInfo:
    """Extracted song information from MP4 file."""

    samples: List[SampleInfo] = field(default_factory=list)
    moov_data: bytes = b""
    ftyp_data: bytes = b""
    encryption_info: Optional[EncryptionInfo] = None


def read_box_header(f: BinaryIO) -> tuple[int, str, int]:
    """Read MP4 box header, return (size, type, header_size)."""
    header = f.read(8)
    if len(header) < 8:
        return 0, "", 0

    size = struct.unpack(">I", header[:4])[0]
    box_type = header[4:8].decode("ascii", errors="replace")
    header_size = 8

    if size == 1:
        ext_size = f.read(8)
        size = struct.unpack(">Q", ext_size)[0]
        header_size = 16
    elif size == 0:
        pos = f.tell()
        f.seek(0, 2)
        size = f.tell() - pos + header_size
        f.seek(pos)

    return size, box_type, header_size


def find_box(data: bytes, box_path: List[str]) -> Optional[bytes]:
    """Find a box in MP4 data by path (e.g., ['moov', 'trak', 'mdia'])."""
    f = io.BytesIO(data)

    for target_type in box_path:
        found = False
        while True:
            pos = f.tell()
            size, box_type, header_size = read_box_header(f)
            if size == 0:
                break

            if box_type == target_type:
                f.seek(pos + header_size)
                found = True
                break
            else:
                f.seek(pos + size)

        if not found:
            return None

    # Return remaining data from current position
    return f.read()


def extract_song(input_path: str) -> SongInfo:
    """
    Extract song samples and metadata from encrypted MP4 file.

    This parses the MP4 structure to extract:
    - ftyp and moov boxes (for reassembly)
    - Individual audio samples from mdat boxes
    - Sample durations and description indices from moof boxes
    """
    with open(input_path, "rb") as f:
        raw_data = f.read()

    song_info = SongInfo()

    boxes = []
    offset = 0
    while offset < len(raw_data) - 8:
        size = struct.unpack(">I", raw_data[offset : offset + 4])[0]
        box_type = raw_data[offset + 4 : offset + 8].decode("ascii", errors="replace")

        header_size = 8
        if size == 0:
            break
        if size == 1:
            # Extended size
            if offset + 16 > len(raw_data):
                break
            size = struct.unpack(">Q", raw_data[offset + 8 : offset + 16])[0]
            header_size = 16

        boxes.append(
            {
                "offset": offset,
                "size": size,
                "type": box_type,
                "header_size": header_size,
                "data": raw_data[offset : offset + size],
            }
        )
        offset += size

    logger.debug(f"Found {len(boxes)} top-level boxes")

    # Extract ftyp and moov
    for box in boxes:
        if box["type"] == "ftyp":
            song_info.ftyp_data = box["data"]
        elif box["type"] == "moov":
            song_info.moov_data = box["data"]

    audio_track_id = (
        _extract_audio_track_id(song_info.moov_data) if song_info.moov_data else 1
    )
    logger.debug(f"Audio track ID: {audio_track_id}")

    trex_defaults = (
        _extract_trex_defaults(song_info.moov_data, audio_track_id)
        if song_info.moov_data
        else None
    )
    if trex_defaults:
        default_sample_duration = trex_defaults["default_sample_duration"]
        default_sample_size = trex_defaults["default_sample_size"]
    else:
        # ALAC uses 4096 samples/frame, AAC uses 1024.
        is_alac = song_info.moov_data and b"alac" in song_info.moov_data
        default_sample_duration = 4096 if is_alac else 1024
        default_sample_size = 0
    logger.debug(
        f"Default sample duration: {default_sample_duration}, "
        f"default sample size: {default_sample_size}"
    )

    if song_info.moov_data:
        song_info.encryption_info = _extract_encryption_info(song_info.moov_data)

    moof_box = None
    for box in boxes:
        if box["type"] == "moof":
            moof_box = box
        elif box["type"] == "mdat" and moof_box is not None:
            moof_data = moof_box["data"]
            mdat_data = box["data"][box["header_size"] :]  # Skip mdat header

            _iv_size = (
                song_info.encryption_info.per_sample_iv_size
                if song_info.encryption_info
                else 0
            )
            samples_from_pair = _parse_moof_mdat(
                moof_data,
                mdat_data,
                default_sample_duration,
                default_sample_size,
                audio_track_id=audio_track_id,
                moof_offset=moof_box["offset"],
                mdat_data_offset=box["offset"] + box["header_size"],
                per_sample_iv_size=_iv_size,
            )
            song_info.samples.extend(samples_from_pair)
            moof_box = None

    # ALAC: force duration 4096 for all samples except the last (may be partial).
    is_alac = song_info.moov_data and (b"alac" in song_info.moov_data or b"ALAC" in song_info.moov_data)
    if is_alac and song_info.samples:

        for i, sample in enumerate(song_info.samples):
            is_last_sample = (i == len(song_info.samples) - 1)

            if sample.duration == 0:
                sample.duration = 4096
            elif sample.duration == 1024 and not is_last_sample:
                sample.duration = 4096

    logger.debug(f"Extracted {len(song_info.samples)} samples from {input_path}")
    return song_info


def _parse_moof_mdat(
    moof_data: bytes,
    mdat_data: bytes,
    default_sample_duration: int,
    default_sample_size: int,
    audio_track_id: int = 1,
    moof_offset: int = 0,
    mdat_data_offset: int = 0,
    per_sample_iv_size: int = 0,
) -> List[SampleInfo]:
    """Parse a moof box and extract samples from corresponding mdat.

    Handles multi-track fragmented MP4s by only extracting samples from
    the traf matching the audio track ID.

    Args:
        audio_track_id: Track ID of the audio track to extract.
        moof_offset: Absolute file offset of the moof box.
        mdat_data_offset: Absolute file offset of the mdat content (after header).
        per_sample_iv_size: IV size per sample from tenc (0, 8, or 16).
    """
    samples = []

    offset = 8
    while offset < len(moof_data) - 8:
        size = struct.unpack(">I", moof_data[offset : offset + 4])[0]
        box_type = moof_data[offset + 4 : offset + 8].decode("ascii", errors="replace")

        if size == 0 or offset + size > len(moof_data):
            break

        if box_type == "traf":
            tfhd_info = {
                "track_id": 0,
                "desc_index": 0,
                "default_duration": default_sample_duration,
                "default_size": default_sample_size,
                "flags": 0,
                "base_data_offset": None,
            }
            trun_entries = []
            first_trun_data_offset = None
            senc_entries = []  # Per-sample encryption info from senc box

            traf_offset = offset + 8
            traf_end = offset + size
            while traf_offset < traf_end - 8:
                inner_size = struct.unpack(
                    ">I", moof_data[traf_offset : traf_offset + 4]
                )[0]
                inner_type = moof_data[traf_offset + 4 : traf_offset + 8].decode(
                    "ascii", errors="replace"
                )

                if inner_size == 0:
                    break

                if inner_type == "tfhd":
                    _parse_tfhd(
                        moof_data[traf_offset + 8 : traf_offset + inner_size], tfhd_info
                    )
                elif inner_type == "trun":
                    entries, data_off = _parse_trun(
                        moof_data[traf_offset + 8 : traf_offset + inner_size], tfhd_info
                    )
                    if first_trun_data_offset is None:
                        first_trun_data_offset = data_off
                    trun_entries.extend(entries)
                elif inner_type == "senc":
                    senc_entries = _parse_senc(
                        moof_data[traf_offset + 8 : traf_offset + inner_size],
                        per_sample_iv_size,
                    )

                traf_offset += inner_size

            if tfhd_info["track_id"] != audio_track_id:
                offset += size
                continue

            base = tfhd_info.get("base_data_offset")
            if base is None:
                base = moof_offset  # Default: first byte of containing moof

            if first_trun_data_offset is not None:
                mdat_idx = (base + first_trun_data_offset) - mdat_data_offset
            else:
                mdat_idx = 0

            mdat_read_offset = max(0, mdat_idx)
            desc_index = tfhd_info["desc_index"]
            if desc_index > 0:
                desc_index -= 1  # Convert to 0-indexed

            for i, entry in enumerate(trun_entries):
                sample_size = entry.get("size", tfhd_info["default_size"])
                sample_duration = entry.get("duration", tfhd_info["default_duration"])

                if sample_size > 0 and mdat_read_offset + sample_size <= len(mdat_data):
                    sample_iv = b""
                    sample_subsamples = []
                    if i < len(senc_entries):
                        sample_iv = senc_entries[i]["iv"]
                        sample_subsamples = senc_entries[i]["subsamples"]

                    sample = SampleInfo(
                        data=mdat_data[
                            mdat_read_offset : mdat_read_offset + sample_size
                        ],
                        duration=sample_duration,
                        desc_index=desc_index,
                        iv=sample_iv,
                        subsamples=sample_subsamples,
                    )
                    samples.append(sample)
                    mdat_read_offset += sample_size

        offset += size

    return samples


def _parse_tfhd(data: bytes, tfhd_info: dict):
    """Parse track fragment header (tfhd) box."""
    if len(data) < 8:
        return

    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    tfhd_info["flags"] = flags
    tfhd_info["track_id"] = struct.unpack(">I", data[4:8])[0]
    offset = 8  # version+flags(4) + track_id(4)

    if flags & 0x01 and offset + 8 <= len(data):
        tfhd_info["base_data_offset"] = struct.unpack(">Q", data[offset : offset + 8])[
            0
        ]
        offset += 8
    if flags & 0x02 and offset + 4 <= len(data):
        tfhd_info["desc_index"] = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
    if flags & 0x08 and offset + 4 <= len(data):
        tfhd_info["default_duration"] = struct.unpack(">I", data[offset : offset + 4])[
            0
        ]
        offset += 4
    if flags & 0x10 and offset + 4 <= len(data):
        tfhd_info["default_size"] = struct.unpack(">I", data[offset : offset + 4])[0]


def _parse_trun(data: bytes, tfhd_info: dict) -> tuple[List[dict], Optional[int]]:
    """Parse track run box to get sample entries and data_offset.

    Returns:
        Tuple of (entries, data_offset). data_offset is the signed offset from
        base_data_offset to the first sample's data, or None if not present.
    """
    entries = []
    data_offset_value = None
    if len(data) < 8:  # version(1) + flags(3) + sample_count(4)
        return entries, data_offset_value

    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    sample_count = struct.unpack(">I", data[4:8])[0]
    offset = 8
    if flags & 0x01:
        data_offset_value = struct.unpack(">i", data[offset : offset + 4])[0]
        offset += 4
    if flags & 0x04:
        offset += 4

    for _ in range(sample_count):
        entry = {}
        if flags & 0x100 and offset + 4 <= len(data):
            entry["duration"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x200 and offset + 4 <= len(data):
            entry["size"] = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
        if flags & 0x400:
            offset += 4
        if flags & 0x800:
            offset += 4
        entries.append(entry)

    return entries, data_offset_value


def _parse_senc(data: bytes, per_sample_iv_size: int) -> List[dict]:
    """Parse Sample Encryption Box (senc) content.

    Returns a list of dicts: {"iv": bytes, "subsamples": [(clear_bytes, encrypted_bytes)]}
    per_sample_iv_size=0 means cbcs with constant IV; subsamples may still be present.
    """
    if len(data) < 8:
        return []

    version = data[0]
    flags = struct.unpack(">I", b"\x00" + data[1:4])[0]
    sample_count = struct.unpack(">I", data[4:8])[0]

    entries: List[dict] = []
    offset = 8
    for _ in range(sample_count):
        iv = b""
        if per_sample_iv_size > 0:
            if offset + per_sample_iv_size > len(data):
                break
            iv = data[offset : offset + per_sample_iv_size]
            offset += per_sample_iv_size

        subsamples = []
        if flags & 0x02:
            if offset + 2 > len(data):
                break
            subsample_count = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            for _ in range(subsample_count):
                if offset + 6 > len(data):
                    break
                clear_bytes = struct.unpack(">H", data[offset : offset + 2])[0]
                encrypted_bytes = struct.unpack(">I", data[offset + 2 : offset + 6])[0]
                subsamples.append((clear_bytes, encrypted_bytes))
                offset += 6

        entries.append({"iv": iv, "subsamples": subsamples})

    return entries


async def decrypt_samples(
    wrapper_ip: str,
    track_id: str,
    fairplay_key: str,
    samples: List[SampleInfo],
    progress_callback=None,
) -> bytes:
    """
    Decrypt samples via the old TCP socket wrapper (legacy). See decrypt_file_hex for current usage.

    CBCS: only the 16-byte-aligned prefix of each sample is encrypted; trailing bytes are kept clear.
    Protocol: send key metadata, then sample data; read back decrypted data; close with 5-zero sentinel.
    """
    host, port = wrapper_ip.split(":")
    port = int(port)

    reader, writer = await asyncio.open_connection(host, port)

    try:
        decrypted_data = bytearray()
        last_desc_index = 255

        keys = [PREFETCH_KEY, fairplay_key]
        total_samples = len(samples)
        bytes_processed = 0
        start_time = time.time()
        last_progress_time = start_time

        for i, sample in enumerate(samples):
            if last_desc_index != sample.desc_index:
                if last_desc_index != 255:
                    writer.write(struct.pack("<I", 0))
                    await writer.drain()

                key_uri = keys[min(sample.desc_index, len(keys) - 1)]

                if key_uri == PREFETCH_KEY:
                    id_bytes = b"0"
                else:
                    id_bytes = track_id.encode("utf-8")
                writer.write(struct.pack("B", len(id_bytes)))
                writer.write(id_bytes)

                key_bytes = key_uri.encode("utf-8")
                writer.write(struct.pack("B", len(key_bytes)))
                writer.write(key_bytes)
                await writer.drain()

                last_desc_index = sample.desc_index

            sample_len = len(sample.data)
            truncated_len = sample_len & ~0xF

            if truncated_len > 0:
                writer.write(struct.pack("<I", truncated_len))
                writer.write(sample.data[:truncated_len])
                await writer.drain()

                decrypted_sample = await reader.readexactly(truncated_len)
                if len(decrypted_sample) != truncated_len:
                    raise IOError(
                        f"Short read: got {len(decrypted_sample)}, expected {truncated_len}"
                    )
                decrypted_data.extend(decrypted_sample)
                bytes_processed += truncated_len

            if truncated_len < sample_len:
                decrypted_data.extend(sample.data[truncated_len:])
                bytes_processed += sample_len - truncated_len

            now = time.time()
            if progress_callback and (
                i % 50 == 0 or now - last_progress_time > 0.5 or i == total_samples - 1
            ):
                elapsed = now - start_time
                speed = bytes_processed / elapsed if elapsed > 0 else 0
                progress_callback(i + 1, total_samples, bytes_processed, speed)
                last_progress_time = now

        writer.write(bytes([0, 0, 0, 0, 0]))
        await writer.drain()

        logger.debug(f"Decrypted {len(samples)} samples ({len(decrypted_data)} bytes)")
        return bytes(decrypted_data)

    finally:
        writer.close()
        await writer.wait_closed()


def write_decrypted_m4a(
    output_path: str,
    song_info: SongInfo,
    decrypted_data: bytes,
    original_path: str = None,
) -> None:
    """
    Write decrypted MP4 file as non-fragmented MP4.

    Creates a new MP4 from scratch with:
    - ftyp box (M4A compatible)
    - moov box with proper sample tables (stts, stsc, stsz, stco)
    - Single mdat box with all decrypted samples

    This matches the output format of Go's amdecrypt which is required
    for ALAC playback.
    """
    # _extract_stsd_content automatically strips encryption metadata
    stsd_content = None
    orig_mvhd = None
    orig_tkhd = None
    orig_mdhd = None
    orig_smhd = None
    orig_dinf = None
    orig_hdlr = None
    timescale = 44100  # Default fallback

    if original_path:
        with open(original_path, "rb") as f:
            orig_data = f.read()
    elif song_info.moov_data:
        orig_data = song_info.ftyp_data + song_info.moov_data
    else:
        orig_data = None

    if orig_data:
        stsd_content = _extract_stsd_content(orig_data)
        # Extract the REAL sample rate from the codec configuration
        timescale = _extract_sample_rate_from_stsd(stsd_content) or _extract_timescale(orig_data)

        moov_idx = orig_data.find(b"moov")
        if moov_idx >= 4:
            moov_size = struct.unpack(">I", orig_data[moov_idx - 4 : moov_idx])[0]
            moov_data = orig_data[moov_idx - 4 : moov_idx - 4 + moov_size]

            orig_mvhd = _find_child_box(moov_data, b"mvhd")

            audio_trak = _find_audio_trak(moov_data)
            if audio_trak:
                orig_tkhd = _find_child_box(audio_trak, b"tkhd")
                mdia = _find_child_box(audio_trak, b"mdia")
                if mdia:
                    orig_mdhd = _find_child_box(mdia, b"mdhd")
                    orig_hdlr = _find_child_box(mdia, b"hdlr")
                    minf = _find_child_box(mdia, b"minf")
                    if minf:
                        orig_smhd = _find_child_box(minf, b"smhd")
                        orig_dinf = _find_child_box(minf, b"dinf")

    with open(output_path, "wb") as f:
        _write_ftyp(f)

        # Calculate total duration
        total_duration = sum(s.duration for s in song_info.samples)

        # Write moov with sample tables
        _write_moov(
            f,
            song_info.samples,
            total_duration,
            timescale,
            stsd_content,
            decrypted_data,
            orig_mvhd=orig_mvhd,
            orig_tkhd=orig_tkhd,
            orig_mdhd=orig_mdhd,
            orig_hdlr=orig_hdlr,
            orig_smhd=orig_smhd,
            orig_dinf=orig_dinf,
        )

        _write_mdat(f, decrypted_data)

    logger.debug(f"Wrote decrypted file to {output_path}")


def _write_box(f, box_type: bytes, content: bytes):
    """Write a simple MP4 box."""
    size = len(content) + 8
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.write(content)


def _write_ftyp(f):
    """Write ftyp box for M4A."""
    content = b"M4A " + struct.pack(">I", 0)  # major brand + minor version
    content += b"M4A mp42isom\x00\x00\x00\x00"  # compatible brands
    _write_box(f, b"ftyp", content)


def _write_fullbox(f, box_type: bytes, version: int, flags: int, content: bytes):
    """Write a FullBox (with version and flags)."""
    size = len(content) + 12
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.write(struct.pack("B", version))
    f.write(struct.pack(">I", flags)[1:])  # 3 bytes for flags
    f.write(content)


def _write_moov(
    f,
    samples: List[SampleInfo],
    total_duration: int,
    timescale: int,
    stsd_content: bytes,
    decrypted_data: bytes,
    orig_mvhd: Optional[bytes] = None,
    orig_tkhd: Optional[bytes] = None,
    orig_mdhd: Optional[bytes] = None,
    orig_hdlr: Optional[bytes] = None,
    orig_smhd: Optional[bytes] = None,
    orig_dinf: Optional[bytes] = None,
):
    """Write moov box with sample tables, patching duration fields from originals when available."""
    moov_start = f.tell()
    f.write(b"\x00" * 8)

    if orig_mvhd:
        f.write(_patch_mvhd_duration(orig_mvhd, total_duration, timescale))
    else:
        mvhd_content = struct.pack(">II", 0, 0)  # creation, modification
        mvhd_content += struct.pack(">I", timescale)
        mvhd_content += struct.pack(">I", total_duration)
        mvhd_content += struct.pack(">I", 0x00010000)  # rate (1.0)
        mvhd_content += struct.pack(">H", 0x0100)  # volume (1.0)
        mvhd_content += b"\x00" * 10  # reserved
        mvhd_content += struct.pack(
            ">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )  # matrix
        mvhd_content += b"\x00" * 24  # pre_defined
        mvhd_content += struct.pack(">I", 2)  # next_track_id
        _write_fullbox(f, b"mvhd", 0, 0, mvhd_content)

    trak_start = f.tell()
    f.write(b"\x00" * 8)

    # tkhd (track header)
    if orig_tkhd:
        f.write(_patch_tkhd_duration(orig_tkhd, total_duration))
    else:
        tkhd_content = struct.pack(">II", 0, 0)  # creation, modification
        tkhd_content += struct.pack(">I", 1)  # track_id
        tkhd_content += struct.pack(">I", 0)  # reserved
        tkhd_content += struct.pack(">I", total_duration)
        tkhd_content += b"\x00" * 8  # reserved
        tkhd_content += struct.pack(">HH", 0, 0)  # layer, alternate_group
        tkhd_content += struct.pack(">H", 0x0100)  # volume
        tkhd_content += struct.pack(">H", 0)  # reserved
        tkhd_content += struct.pack(
            ">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000
        )  # matrix
        tkhd_content += struct.pack(">II", 0, 0)  # width, height
        _write_fullbox(f, b"tkhd", 0, 7, tkhd_content)

    mdia_start = f.tell()
    f.write(b"\x00" * 8)

    # mdhd - preserves original language code
    if orig_mdhd:
        f.write(_patch_mdhd_duration(orig_mdhd, total_duration, timescale))
    else:
        mdhd_content = struct.pack(">II", 0, 0)  # creation, modification
        mdhd_content += struct.pack(">I", timescale)
        mdhd_content += struct.pack(">I", total_duration)
        mdhd_content += struct.pack(">H", 0x55C4)  # language (und)
        mdhd_content += struct.pack(">H", 0)  # quality
        _write_fullbox(f, b"mdhd", 0, 0, mdhd_content)

    # hdlr - preserves original handler name (e.g. "Core Media Audio")
    if orig_hdlr:
        f.write(orig_hdlr)
    else:
        hdlr_content = struct.pack(">I", 0)  # pre_defined
        hdlr_content += b"soun"  # handler_type
        hdlr_content += b"\x00" * 12  # reserved
        handler_name = b"SoundHandler"
        hdlr_content += struct.pack("B", len(handler_name)) + handler_name + b"\x00"
        _write_fullbox(f, b"hdlr", 0, 0, hdlr_content)

    minf_start = f.tell()
    f.write(b"\x00" * 8)

    # smhd (sound media header)
    if orig_smhd:
        f.write(orig_smhd)
    else:
        smhd_content = struct.pack(">HH", 0, 0)  # balance, reserved
        _write_fullbox(f, b"smhd", 0, 0, smhd_content)

    # dinf + dref
    if orig_dinf:
        f.write(orig_dinf)
    else:
        dinf_start = f.tell()
        f.write(b"\x00" * 8)
        dref_content = struct.pack(">I", 1)  # entry_count
        dref_content += (
            struct.pack(">I", 12) + b"url " + struct.pack(">I", 1)
        )  # url entry (self-contained)
        _write_fullbox(f, b"dref", 0, 0, dref_content)
        _fixup_box_size(f, dinf_start, b"dinf")

    # stbl (sample table)
    stbl_start = f.tell()
    f.write(b"\x00" * 8)

    _write_stsd(f, stsd_content)

    _write_stts(f, samples)

    # stsc: all samples in one chunk
    stsc_content = struct.pack(">I", 1)  # entry_count
    stsc_content += struct.pack(
        ">III", 1, len(samples), 1
    )  # first_chunk, samples_per_chunk, sample_description_index
    _write_fullbox(f, b"stsc", 0, 0, stsc_content)

    stsz_content = struct.pack(">I", 0)  # sample_size (0 = variable)
    stsz_content += struct.pack(">I", len(samples))  # sample_count
    for sample in samples:
        stsz_content += struct.pack(">I", len(sample.data))
    _write_fullbox(f, b"stsz", 0, 0, stsz_content)

    stco_pos = f.tell()
    stco_content = struct.pack(">I", 1)  # entry_count
    stco_content += struct.pack(">I", 0)  # chunk_offset (placeholder)
    _write_fullbox(f, b"stco", 0, 0, stco_content)

    _fixup_box_size(f, stbl_start, b"stbl")
    _fixup_box_size(f, minf_start, b"minf")
    _fixup_box_size(f, mdia_start, b"mdia")
    _fixup_box_size(f, trak_start, b"trak")

    # udta > meta > hdlr(mdir) + ilst - metadata container
    _write_udta(f)

    _fixup_box_size(f, moov_start, b"moov")

    # Fix stco with actual mdat offset
    mdat_offset = f.tell() + 8  # +8 for mdat header
    f.seek(stco_pos + 16)  # +12 for box header + version/flags, +4 for entry_count
    f.write(struct.pack(">I", mdat_offset))
    f.seek(0, 2)  # Back to end

def _extract_sample_rate_from_stsd(stsd_content: bytes) -> Optional[int]:
    """Extract the actual audio sample rate from the stsd box content."""
    # sample_rate fixed-point field is at offset 40 in stsd content.
    if not stsd_content or len(stsd_content) < 44:
        return None
        
    samplerate_offset = 40
    sample_rate_fixed = struct.unpack(">I", stsd_content[samplerate_offset : samplerate_offset + 4])[0]
    sample_rate = sample_rate_fixed >> 16
    if 8000 <= sample_rate <= 384000:
        return sample_rate
    return None


def _write_stsd(f, stsd_content: bytes):
    """Write stsd box using codec info from the original file."""
    if stsd_content:
        size = len(stsd_content) + 8
        f.write(struct.pack(">I", size))
        f.write(b"stsd")
        f.write(stsd_content)
    else:
        _write_stsd_alac_fallback(f)


def _write_stsd_alac_fallback(f):
    """Write a default ALAC sample description box (fallback)."""
    stsd_start = f.tell()
    f.write(b"\x00" * 12)

    f.write(struct.pack(">I", 1))  # entry_count

    alac_start = f.tell()
    f.write(b"\x00" * 8)

    f.write(b"\x00" * 6)  # reserved
    f.write(struct.pack(">H", 1))  # data_reference_index
    f.write(b"\x00" * 8)  # reserved
    f.write(struct.pack(">H", 2))  # channel_count
    f.write(struct.pack(">H", 16))  # sample_size (bits)
    f.write(struct.pack(">H", 0))  # pre_defined
    f.write(struct.pack(">H", 0))  # reserved
    f.write(struct.pack(">I", 44100 << 16))

    # alac magic cookie box
    default_config = bytes(
        [
            0x00,
            0x00,
            0x10,
            0x00,  # frame_length
            0x00,  # compatible_version
            0x18,  # bit_depth (24)
            0x28,
            0x28,
            0x0A,  # pb, mb, kb
            0x02,  # num_channels
            0x00,
            0x00,  # max_run
            0x00,
            0x00,
            0xFF,
            0xFF,  # max_frame_bytes
            0x00,
            0x0D,
            0x00,
            0x80,  # avg_bit_rate
            0x00,
            0x00,
            0xAC,
            0x44,  # sample_rate
        ]
    )
    _write_box(f, b"alac", default_config)

    _fixup_box_size(f, alac_start, b"alac")

    end_pos = f.tell()
    size = end_pos - stsd_start
    f.seek(stsd_start)
    f.write(struct.pack(">I", size))
    f.write(b"stsd")
    f.write(struct.pack(">I", 0))
    f.seek(end_pos)


def _write_stts(f, samples: List[SampleInfo]):
    """Write time-to-sample box (stts), run-length encoded."""
    entries = []
    for sample in samples:
        if entries and entries[-1][1] == sample.duration:
            entries[-1] = (entries[-1][0] + 1, sample.duration)
        else:
            entries.append((1, sample.duration))

    content = struct.pack(">I", len(entries))
    for count, delta in entries:
        content += struct.pack(">II", count, delta)
    _write_fullbox(f, b"stts", 0, 0, content)


def _fixup_box_size(f, start_pos: int, box_type: bytes):
    """Fix up the size field of a box that was written with placeholder."""
    end_pos = f.tell()
    size = end_pos - start_pos
    f.seek(start_pos)
    f.write(struct.pack(">I", size))
    f.write(box_type)
    f.seek(end_pos)


def _write_mdat(f, data: bytes):
    """Write mdat box."""
    size = len(data) + 8
    f.write(struct.pack(">I", size))
    f.write(b"mdat")
    f.write(data)


def _extract_stsd_content(data: bytes) -> Optional[bytes]:
    """Extract stsd box content from moov (supports any codec)."""
    idx = data.find(b"stsd")
    if idx < 4:
        return None

    # Get stsd box size
    size = struct.unpack(">I", data[idx - 4 : idx])[0]
    if size < 16 or size > 10000:
        return None

    raw_content = data[idx + 4 : idx - 4 + size]

    return _clean_stsd_content(raw_content)


def _clean_stsd_content(stsd_content: bytes) -> bytes:
    """Remove encryption metadata from stsd content; keep only the first sample entry."""
    if len(stsd_content) < 8:
        return stsd_content

    version_flags = stsd_content[:4]
    entry_count = struct.unpack(">I", stsd_content[4:8])[0]

    offset = 8
    first_cleaned_entry = b""

    for _ in range(entry_count):
        if offset + 8 > len(stsd_content):
            break

        entry_size = struct.unpack(">I", stsd_content[offset : offset + 4])[0]
        entry_type = stsd_content[offset + 4 : offset + 8]

        if entry_size < 8 or offset + entry_size > len(stsd_content):
            break

        entry_data = stsd_content[offset : offset + entry_size]

        # Check if this is an encrypted entry
        if entry_type in (b"enca", b"encv", b"encs", b"encm"):
            cleaned_entry = _clean_encrypted_sample_entry(entry_data)
        else:
            cleaned_entry = _remove_sinf_from_entry(entry_data)

        # We only need the FIRST valid entry for a decrypted, non-fragmented file
        first_cleaned_entry = cleaned_entry
        break

    # Rebuild stsd content with EXACTLY 1 entry
    if first_cleaned_entry:
        result = version_flags + struct.pack(">I", 1) + first_cleaned_entry
        return result

    return stsd_content


def _clean_encrypted_sample_entry(entry_data: bytes) -> bytes:
    """Strip encryption wrapper from an enca/encv sample entry: replace type with original format, remove sinf."""
    if len(entry_data) < 36:  # Minimum audio sample entry size
        return entry_data

    entry_size = struct.unpack(">I", entry_data[:4])[0]
    entry_type = entry_data[4:8]

    original_format = _find_original_format(entry_data)
    if not original_format:
        # If we can't find frma, try common mappings
        if entry_type == b"enca":
            original_format = b"mp4a"  # Default to AAC
        elif entry_type == b"encv":
            original_format = b"avc1"  # Default to H.264
        else:
            original_format = entry_type  # Keep as-is

    # Fixed header is 36 bytes; child boxes follow.
    new_entry = entry_data[:4] + original_format + entry_data[8:36]

    child_offset = 36
    while child_offset + 8 <= len(entry_data):
        child_size = struct.unpack(">I", entry_data[child_offset : child_offset + 4])[0]
        child_type = entry_data[child_offset + 4 : child_offset + 8]

        if child_size < 8 or child_offset + child_size > len(entry_data):
            break

        if child_type != b"sinf":
            new_entry += entry_data[child_offset : child_offset + child_size]

        child_offset += child_size

    new_size = len(new_entry)
    new_entry = struct.pack(">I", new_size) + new_entry[4:]

    return new_entry


def _find_original_format(entry_data: bytes) -> Optional[bytes]:
    """Find the original codec format (frma) from the sinf box of an encrypted entry."""
    sinf_idx = entry_data.find(b"sinf")
    if sinf_idx < 4:
        return None

    sinf_size = struct.unpack(">I", entry_data[sinf_idx - 4 : sinf_idx])[0]
    if sinf_size < 16 or sinf_idx + sinf_size > len(entry_data) + 4:
        return None

    sinf_data = entry_data[sinf_idx - 4 : sinf_idx - 4 + sinf_size]

    frma_idx = sinf_data.find(b"frma")
    if frma_idx < 4:
        return None

    frma_size = struct.unpack(">I", sinf_data[frma_idx - 4 : frma_idx])[0]
    if frma_size != 12:
        return None
    return sinf_data[frma_idx + 4 : frma_idx + 8]


def _remove_sinf_from_entry(entry_data: bytes) -> bytes:
    """Remove sinf box from a sample entry if present."""
    if len(entry_data) < 36:
        return entry_data

    if b"sinf" not in entry_data:
        return entry_data

    new_entry = entry_data[:36]

    child_offset = 36
    while child_offset + 8 <= len(entry_data):
        child_size = struct.unpack(">I", entry_data[child_offset : child_offset + 4])[0]
        child_type = entry_data[child_offset + 4 : child_offset + 8]

        if child_size < 8 or child_offset + child_size > len(entry_data):
            break

        if child_type != b"sinf":
            new_entry += entry_data[child_offset : child_offset + child_size]
        child_offset += child_size

    new_size = len(new_entry)
    new_entry = struct.pack(">I", new_size) + new_entry[4:]

    return new_entry


def _extract_alac_config(data: bytes) -> Optional[bytes]:
    """Extract ALAC configuration from moov/stsd (for backwards compatibility)."""
    idx = data.find(b"alac")
    if idx < 4:
        return None

    alac_idx = idx
    while alac_idx < len(data) - 100:
        if data[alac_idx : alac_idx + 4] == b"alac":
            size = struct.unpack(">I", data[alac_idx - 4 : alac_idx])[0]
            if 20 < size < 100:  # Reasonable ALAC config size
                return data[alac_idx + 4 : alac_idx - 4 + size]
        alac_idx += 1
        if alac_idx > idx + 200:
            break
    return None


def _extract_timescale(data: bytes) -> int:
    """Extract timescale from mdhd (or mvhd fallback)."""
    idx = data.find(b"mdhd")
    if idx > 0 and idx + 28 < len(data):
        version = data[idx + 4]
        if version == 0:
            return struct.unpack(">I", data[idx + 16 : idx + 20])[0]
        else:
            return struct.unpack(">I", data[idx + 24 : idx + 28])[0]
    return 44100  # Default fallback


def _find_child_box(
    container_data: bytes, target_type: bytes, skip_header: int = 8
) -> Optional[bytes]:
    """Find a direct child box in container_data. Returns full box bytes or None."""
    offset = skip_header
    while offset + 8 <= len(container_data):
        size = struct.unpack(">I", container_data[offset : offset + 4])[0]
        box_type = container_data[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(container_data):
            break
        if box_type == target_type:
            return container_data[offset : offset + size]
        offset += size
    return None


def _find_audio_trak(moov_data: bytes) -> Optional[bytes]:
    """Return the first audio (soun handler) trak box from moov data, or None."""
    offset = 8  # Skip moov header
    while offset + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[offset : offset + 4])[0]
        box_type = moov_data[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(moov_data):
            break
        if box_type == b"trak":
            trak_data = moov_data[offset : offset + size]
            hdlr_idx = trak_data.find(b"hdlr")
            if hdlr_idx > 0:
                # hdlr FullBox: version+flags(4) + pre_defined(4) + handler_type(4)
                handler_offset = hdlr_idx + 4 + 4 + 4
                if handler_offset + 4 <= len(trak_data):
                    if trak_data[handler_offset : handler_offset + 4] == b"soun":
                        return trak_data
        offset += size
    return None


def _patch_mvhd_duration(box_data: bytes, duration: int, timescale: int) -> bytes:
    """Return a copy of the mvhd box with duration and timescale patched."""
    data = bytearray(box_data)
    version = data[8]
    if version == 0:
        struct.pack_into(">I", data, 20, timescale)
        struct.pack_into(">I", data, 24, duration)
    else:
        struct.pack_into(">I", data, 28, timescale)
        struct.pack_into(">Q", data, 32, duration)
    return bytes(data)


def _patch_tkhd_duration(box_data: bytes, duration: int) -> bytes:
    """Return a copy of the tkhd box with duration patched and flags set to 7 (enabled|in_movie|in_preview)."""
    data = bytearray(box_data)
    version = data[8]
    data[9:12] = struct.pack(">I", 7)[1:]
    if version == 0:
        struct.pack_into(">I", data, 28, duration)
    else:
        struct.pack_into(">Q", data, 36, duration)
    return bytes(data)



def _patch_mdhd_duration(box_data: bytes, duration: int, timescale: int) -> bytes:
    """Return a copy of the mdhd box with duration and timescale patched."""
    data = bytearray(box_data)
    version = data[8]
    if version == 0:
        struct.pack_into(">I", data, 20, timescale)
        struct.pack_into(">I", data, 24, duration)
    else:
        struct.pack_into(">I", data, 28, timescale)
        struct.pack_into(">Q", data, 32, duration)
    return bytes(data)


def _write_udta(f):
    """Write empty udta > meta > hdlr(mdir) + ilst container (matches Go amdecrypt output)."""
    udta_start = f.tell()
    f.write(b"\x00" * 8)

    meta_start = f.tell()
    f.write(b"\x00" * 8)
    f.write(struct.pack(">I", 0))  # meta FullBox: version + flags

    hdlr_content = struct.pack(">I", 0)  # pre_defined
    hdlr_content += b"mdir"  # handler_type
    hdlr_content += struct.pack(">III", 0x6170706C, 0, 0)
    hdlr_content += b"\x00"
    _write_fullbox(f, b"hdlr", 0, 0, hdlr_content)

    _write_box(f, b"ilst", b"")

    _fixup_box_size(f, meta_start, b"meta")
    _fixup_box_size(f, udta_start, b"udta")


def _extract_trex_defaults(moov_data: bytes, target_track_id: int = 0) -> dict:
    """Extract default sample duration/size/flags from moov/mvex/trex for the given track."""
    # ALAC = 4096 samples/frame, AAC = 1024.
    is_alac = b"alac" in moov_data or b"ALAC" in moov_data
    fallback_duration = 4096 if is_alac else 1024

    defaults = {
        "default_sample_duration": fallback_duration,
        "default_sample_size": 0,
        "default_sample_description_index": 1,
        "default_sample_flags": 0,
    }

    mvex = _find_child_box(moov_data, b"mvex")
    if mvex is None:
        return defaults

    offset = 8
    while offset + 8 <= len(mvex):
        size = struct.unpack(">I", mvex[offset : offset + 4])[0]
        box_type = mvex[offset + 4 : offset + 8]
        if size < 8 or offset + size > len(mvex):
            break
        if box_type == b"trex" and size >= 32:
            trex_data = mvex[offset : offset + size]
            track_id = struct.unpack(">I", trex_data[12:16])[0]
            if target_track_id == 0 or track_id == target_track_id:
                defaults["default_sample_description_index"] = struct.unpack(
                    ">I", trex_data[16:20]
                )[0]

                parsed_duration = struct.unpack(">I", trex_data[20:24])[0]

                if parsed_duration == 0:
                    defaults["default_sample_duration"] = fallback_duration
                else:
                    defaults["default_sample_duration"] = parsed_duration

                defaults["default_sample_size"] = struct.unpack(">I", trex_data[24:28])[0]
                defaults["default_sample_flags"] = struct.unpack(
                    ">I", trex_data[28:32]
                )[0]
                logger.debug(
                    f"trex defaults for track {track_id}: "
                    f"duration={defaults['default_sample_duration']}, "
                    f"size={defaults['default_sample_size']}"
                )
                break
        offset += size

    return defaults

def _extract_encryption_info(moov_data: bytes) -> Optional[EncryptionInfo]:
    """Extract encryption info (scheme, IV size, KID) from the audio track sinf box."""
    trak_data = _find_audio_trak(moov_data)
    if trak_data is None:
        return None

    mdia = _find_child_box(trak_data, b"mdia")
    if mdia is None:
        return None
    minf = _find_child_box(mdia, b"minf")
    if minf is None:
        return None
    stbl = _find_child_box(minf, b"stbl")
    if stbl is None:
        return None
    stsd = _find_child_box(stbl, b"stsd")
    if stsd is None:
        return None

    if len(stsd) < 16:
        return None
    entry_offset = 16
    if entry_offset + 8 > len(stsd):
        return None
    entry_size = struct.unpack(">I", stsd[entry_offset : entry_offset + 4])[0]
    entry_data = stsd[entry_offset : entry_offset + entry_size]

    sinf = _find_child_box(entry_data, b"sinf", skip_header=36)
    if sinf is None:
        return None

    info = EncryptionInfo()

    schm = _find_child_box(sinf, b"schm")
    if schm and len(schm) >= 20:
        # schm: 4(size) + 4(type) + 4(ver+flags) + 4(scheme_type) + 4(scheme_version)
        info.scheme_type = schm[12:16].decode("ascii", errors="replace")
        logger.debug(f"Encryption scheme: {info.scheme_type}")

    schi = _find_child_box(sinf, b"schi")
    if schi:
        tenc = _find_child_box(schi, b"tenc")
        if tenc and len(tenc) >= 32:
            tenc_version = tenc[8]
            per_sample_iv_size = tenc[15]
            kid = tenc[16:32]

            info.per_sample_iv_size = per_sample_iv_size
            info.kid = kid
            logger.debug(
                f"tenc: per_sample_iv_size={per_sample_iv_size}, " f"kid={kid.hex()}"
            )

            if per_sample_iv_size == 0 and len(tenc) > 32:
                constant_iv_size = tenc[32]
                if len(tenc) >= 33 + constant_iv_size:
                    info.constant_iv = tenc[33 : 33 + constant_iv_size]
                    logger.debug(f"Constant IV: {info.constant_iv.hex()}")

    return info


def _extract_encryption_info_per_stsd(moov_data: bytes) -> Optional[dict]:
    """Return a dict mapping desc_index → EncryptionInfo for each encrypted stsd entry."""
    trak_data = _find_audio_trak(moov_data)
    if trak_data is None:
        return None

    mdia = _find_child_box(trak_data, b"mdia")
    if mdia is None:
        return None
    minf = _find_child_box(mdia, b"minf")
    if minf is None:
        return None
    stbl = _find_child_box(minf, b"stbl")
    if stbl is None:
        return None
    stsd = _find_child_box(stbl, b"stsd")
    if stsd is None:
        return None

    if len(stsd) < 16:
        return None

    entry_count = struct.unpack(">I", stsd[12:16])[0]
    if entry_count == 0:
        return None

    encryption_info_per_desc = {}
    entry_offset = 16  # past header+version+flags+entry_count

    for desc_idx in range(entry_count):
        if entry_offset + 8 > len(stsd):
            break

        entry_size = struct.unpack(">I", stsd[entry_offset : entry_offset + 4])[0]
        if entry_size < 8 or entry_offset + entry_size > len(stsd):
            break

        entry_data = stsd[entry_offset : entry_offset + entry_size]

        sinf = _find_child_box(entry_data, b"sinf", skip_header=36)
        if sinf is not None:
            info = EncryptionInfo()

            schm = _find_child_box(sinf, b"schm")
            if schm and len(schm) >= 20:
                info.scheme_type = schm[12:16].decode("ascii", errors="replace")
                logger.debug(
                    f"Encryption scheme for desc_index {desc_idx}: {info.scheme_type}"
                )

            schi = _find_child_box(sinf, b"schi")
            if schi:
                tenc = _find_child_box(schi, b"tenc")
                if tenc and len(tenc) >= 32:
                    per_sample_iv_size = tenc[15]
                    kid = tenc[16:32]

                    info.per_sample_iv_size = per_sample_iv_size
                    info.kid = kid
                    logger.debug(
                        f"tenc (desc {desc_idx}): per_sample_iv_size={per_sample_iv_size}"
                    )

                    if per_sample_iv_size == 0 and len(tenc) > 32:
                        constant_iv_size = tenc[32]
                        if len(tenc) >= 33 + constant_iv_size:
                            info.constant_iv = tenc[33 : 33 + constant_iv_size]
                            logger.debug(
                                f"Constant IV (desc {desc_idx}): {info.constant_iv.hex()}"
                            )

            encryption_info_per_desc[desc_idx] = info

        entry_offset += entry_size

    return encryption_info_per_desc if encryption_info_per_desc else None


def _extract_audio_track_id(moov_data: bytes) -> int:
    """Extract the track ID of the audio (soun handler) track from moov. Defaults to 1."""
    offset = 8
    while offset < len(moov_data) - 8:
        size = struct.unpack(">I", moov_data[offset : offset + 4])[0]
        box_type = moov_data[offset + 4 : offset + 8]

        if size < 8 or offset + size > len(moov_data):
            break

        if box_type == b"trak":
            trak_data = moov_data[offset : offset + size]

            hdlr_idx = trak_data.find(b"hdlr")
            if hdlr_idx > 0:
                # hdlr FullBox: after 'hdlr' type comes version+flags(4) + pre_defined(4) + handler_type(4)
                handler_offset = hdlr_idx + 4 + 4 + 4
                if handler_offset + 4 <= len(trak_data):
                    handler_type = trak_data[handler_offset : handler_offset + 4]
                    if handler_type == b"soun":
                        tkhd_idx = trak_data.find(b"tkhd")
                        if tkhd_idx > 0:
                            version = trak_data[tkhd_idx + 4]
                            if version == 0:
                                tid_offset = tkhd_idx + 4 + 4 + 4 + 4
                            else:
                                tid_offset = tkhd_idx + 4 + 4 + 8 + 8
                            if tid_offset + 4 <= len(trak_data):
                                return struct.unpack(
                                    ">I", trak_data[tid_offset : tid_offset + 4]
                                )[0]

        offset += size

    return 1  # Default to track 1


async def decrypt_file(
    wrapper_ip: str,
    track_id: str,
    fairplay_key: str,
    input_path: str,
    output_path: str,
    progress_callback=None,
) -> None:
    """[LEGACY] Decrypt via old TCP socket wrapper. Kept for compatibility; use decrypt_file_temari instead."""
    logger.debug(f"Decrypting {input_path} -> {output_path}")

    song_info = await asyncio.to_thread(extract_song, input_path)
    decrypted_data = await decrypt_samples(
        wrapper_ip,
        track_id,
        fairplay_key,
        song_info.samples,
        progress_callback,
    )

    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,  # Pass original path for codec info extraction
    )


def decrypt_samples_hex(
    samples: List[SampleInfo],
    keys: dict,
    encryption_info: EncryptionInfo,
    encryption_info_per_desc: Optional[dict] = None,
) -> bytes:
    """Decrypt samples using hex AES keys. Supports CENC (AES-CTR) and CBCS (AES-CBC)."""
    is_cenc = encryption_info.scheme_type == "cenc"
    decrypted = bytearray()

    for sample in samples:
        key = keys.get(sample.desc_index)
        if key is None:
            # No key for this desc_index — keep data as-is (shouldn't happen)
            decrypted.extend(sample.data)
            continue

        if encryption_info_per_desc and sample.desc_index in encryption_info_per_desc:
            enc_info = encryption_info_per_desc[sample.desc_index]
        else:
            enc_info = encryption_info

        if is_cenc:
            iv = sample.iv
            if len(iv) < 16:
                iv = iv + b"\x00" * (16 - len(iv))
            cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

            if sample.subsamples:
                plaintext = bytearray()
                offset = 0
                for clear_bytes, encrypted_bytes in sample.subsamples:
                    plaintext.extend(sample.data[offset : offset + clear_bytes])
                    offset += clear_bytes
                    plaintext.extend(
                        cipher.decrypt(sample.data[offset : offset + encrypted_bytes])
                    )
                    offset += encrypted_bytes
                plaintext.extend(sample.data[offset:])
                decrypted.extend(plaintext)
            else:
                decrypted.extend(cipher.decrypt(sample.data))

        else:
            iv = sample.iv if sample.iv else enc_info.constant_iv
            if len(iv) < 16:
                iv = iv + b"\x00" * (16 - len(iv))

            if sample.subsamples:
                # Concatenate all encrypted regions, decrypt as one CBC stream, then split back.
                encrypted_concat = bytearray()
                subsample_sizes = []
                offset = 0
                for clear_bytes, encrypted_bytes in sample.subsamples:
                    offset += clear_bytes
                    if encrypted_bytes > 0:
                        encrypted_concat.extend(
                            sample.data[offset : offset + encrypted_bytes]
                        )
                        subsample_sizes.append(encrypted_bytes)
                    offset += encrypted_bytes

                total_enc_len = len(encrypted_concat)
                decrypted_concat = bytearray()
                if total_enc_len > 0:
                    cbc_len = total_enc_len & ~0xF
                    if cbc_len > 0:
                        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
                        decrypted_concat.extend(
                            cipher.decrypt(bytes(encrypted_concat[:cbc_len]))
                        )
                    if cbc_len < total_enc_len:
                        decrypted_concat.extend(encrypted_concat[cbc_len:])

                plaintext = bytearray()
                dec_offset = 0
                offset = 0
                for clear_bytes, encrypted_bytes in sample.subsamples:
                    plaintext.extend(sample.data[offset : offset + clear_bytes])
                    offset += clear_bytes
                    if encrypted_bytes > 0:
                        plaintext.extend(
                            decrypted_concat[dec_offset : dec_offset + encrypted_bytes]
                        )
                        dec_offset += encrypted_bytes
                    offset += encrypted_bytes
                plaintext.extend(sample.data[offset:])
                decrypted.extend(plaintext)
            else:
                sample_len = len(sample.data)
                if sample_len % 16 == 0:
                    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
                    decrypted.extend(cipher.decrypt(sample.data))
                else:
                    truncated_len = sample_len & ~0xF
                    if truncated_len > 0:
                        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
                        decrypted.extend(cipher.decrypt(sample.data[:truncated_len]))
                        decrypted.extend(sample.data[truncated_len:])
                    else:
                        decrypted.extend(sample.data)

    logger.debug(
        f"Decrypted {len(samples)} samples ({len(decrypted)} bytes) with hex keys"
    )
    return bytes(decrypted)


async def decrypt_file_hex(
    input_path: str,
    output_path: str,
    decryption_key: str,
    legacy: bool = False,
) -> None:
    """Decrypt an encrypted MP4 using a raw hex AES key (no wrapper/mp4decrypt).

    Replaces the mp4decrypt+remux pipeline with pure-Python AES (CTR for cenc, CBC for cbcs).
    """
    logger.debug(f"Hex-key decrypt: {input_path} -> {output_path}")

    song_info = await asyncio.to_thread(extract_song, input_path)
    track_key = bytes.fromhex(decryption_key)

    if legacy:
        # Legacy AAC (cenc): single key for all samples
        keys = {0: track_key}
    else:
        # cbcs: desc_index 0 = prefetch key, desc_index 1 = track key
        keys = {0: DEFAULT_SONG_DECRYPTION_KEY, 1: track_key}

    enc_info = song_info.encryption_info or EncryptionInfo(
        scheme_type="cenc" if legacy else "cbcs"
    )

    enc_info_per_desc = None
    if song_info.moov_data and not legacy:
        enc_info_per_desc = await asyncio.to_thread(
            _extract_encryption_info_per_stsd, song_info.moov_data
        )

    decrypted_data = decrypt_samples_hex(
        song_info.samples, keys, enc_info, enc_info_per_desc
    )

    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,
    )
