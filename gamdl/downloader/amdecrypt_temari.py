"""
Temari-based CBCS decryption for wrapper-lite.

Replaces the old TCP-socket `decrypt_samples` protocol with:
  1. HTTP GET /key on wrapper-lite  → JSON {ctx, state, rcx, rax, rdx, r9, rbp}
  2. temari.Temari.from_json(body)  → parse decryption template
  3. t.decrypt_par(samples)         → batch-decrypt all CBCS samples in-process

No separate decrypt port / TCP socket needed.
"""

import asyncio
import json
import logging
import urllib.parse
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)

# Prefetch key used for the first sample description (desc_index 0)
PREFETCH_URI = "skd://itunes.apple.com/P000000000/s1/e1"


def _fetch_key_json(wrapper_base_url: str, adam_id: str, uri: str) -> bytes:
    """
    Synchronous HTTP call to wrapper-lite /key endpoint.
    Returns the raw JSON response body.

    wrapper_base_url: e.g. "http://127.0.0.1:12340"
    """
    url = (
        f"{wrapper_base_url.rstrip('/')}/key"
        f"?adamId={urllib.parse.quote(adam_id)}"
        f"&uri={urllib.parse.quote(uri)}"
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        body = r.read()

    resp = json.loads(body)
    if resp.get("code") != 0:
        raise RuntimeError(
            f"wrapper-lite /key failed for adamId={adam_id} uri={uri}: {resp}"
        )

    data = resp["data"]
    # wrapper-lite /key returns: adamId, keyUri, contentKey, ctx, state, rcx, rax, rdx, r9, rbp
    # Temari.from_json needs the subset: ctx, state, rcx, rax, rdx, r9, rbp
    temari_json = json.dumps(
        {
            "ctx":   data["ctx"],
            "state": data["state"],
            "rcx":   data.get("rcx", "0x0"),
            "rax":   data.get("rax", "0x0"),
            "rdx":   data.get("rdx", "0x0"),
            "r9":    data.get("r9",  "0x0"),
            "rbp":   data.get("rbp", "0x0"),
        }
    ).encode()
    return temari_json


def _decrypt_samples_temari(
    temari_json_prefetch: bytes,
    temari_json_track: bytes,
    samples,  # List[SampleInfo]
) -> bytes:
    """
    Decrypt CBCS samples with Temari using the two per-description templates.

    desc_index 0 → prefetch template (temari_json_prefetch)
    desc_index 1 → track template    (temari_json_track)

    CBCS full-subsample rule: only the 16-byte-aligned prefix is encrypted;
    the trailing (len % 16) bytes are kept as-is — same as the old TCP path.
    """
    try:
        from temari import Temari  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "temari is not installed. Run: pip install temari"
        ) from exc

    t0 = Temari.from_json(temari_json_prefetch)
    t1 = Temari.from_json(temari_json_track)
    templates = {0: t0, 1: t1}

    try:
        result = bytearray()
        for sample in samples:
            tmpl = templates.get(sample.desc_index, t1)
            data = sample.data
            sample_len = len(data)
            truncated_len = sample_len & ~0xF

            if truncated_len > 0:
                enc_chunk = data[:truncated_len]
                plain_chunk = tmpl.decrypt(enc_chunk)
                result.extend(plain_chunk)
            # Clear tail bytes
            if truncated_len < sample_len:
                result.extend(data[truncated_len:])

        return bytes(result)
    finally:
        for t in templates.values():
            t.close()


async def decrypt_file_temari(
    wrapper_base_url: str,
    adam_id: str,
    fairplay_key: str,
    input_path: str,
    output_path: str,
    progress_callback=None,
) -> None:
    """
    Decrypt an encrypted MP4 file via wrapper-lite + Temari.

    Replaces the old `decrypt_file(wrapper_ip, ...)` that used a TCP socket.

    Args:
        wrapper_base_url: wrapper-lite HTTP base URL, e.g. "http://127.0.0.1:12340"
        adam_id:          Apple Music track ID
        fairplay_key:     FairPlay key URI (skd://...)
        input_path:       Path to encrypted M4A/MP4
        output_path:      Path for decrypted output
        progress_callback: Optional callback(current, total, bytes, speed)
    """
    from .amdecrypt import extract_song, write_decrypted_m4a  # local import avoids circular

    logger.debug(f"[temari] decrypting {input_path} -> {output_path}")

    # 1. Extract samples in a thread
    song_info = await asyncio.to_thread(extract_song, input_path)

    # 2. Fetch both key JSONs from wrapper-lite (concurrently)
    json_prefetch, json_track = await asyncio.gather(
        asyncio.to_thread(_fetch_key_json, wrapper_base_url, adam_id, PREFETCH_URI),
        asyncio.to_thread(_fetch_key_json, wrapper_base_url, adam_id, fairplay_key),
    )

    # 3. Decrypt all samples in a thread (CPU-bound)
    def _run_decrypt():
        data = _decrypt_samples_temari(json_prefetch, json_track, song_info.samples)
        if progress_callback:
            total = len(song_info.samples)
            total_bytes = sum(len(s.data) for s in song_info.samples)
            progress_callback(total, total, total_bytes, 0)
        return data

    decrypted_data = await asyncio.to_thread(_run_decrypt)

    # 4. Write decrypted M4A
    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,
    )

    logger.debug(f"[temari] done: {output_path}")
