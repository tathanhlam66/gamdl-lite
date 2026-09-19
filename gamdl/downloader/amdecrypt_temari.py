"""
Temari-based CBCS decryption for wrapper-lite.

Replaces the old TCP-socket `decrypt_samples` protocol with:
  1. HTTP GET /key on wrapper-lite  → JSON {ctx, state, rcx, rax, rdx, r9, rbp}
  2. temari.Temari.from_json(body)  → parse decryption template
  3. t.decrypt_par(samples)         → batch-decrypt all CBCS samples in-process

No separate decrypt port / TCP socket needed.

Performance notes (Termux / ARM):
  - `extract_song` (MP4 demux) and the two /key fetches run **concurrently**
    via asyncio.gather so I/O and CPU overlap; total wall time ≈ max of the
    three, not their sum.
  - The prefetch template (desc_index 0) is universal across all tracks in the
    same process. It is created once and cached in _PREFETCH_TEMPLATE so that
    `from_json` / `tmpl_from_json` is not called on every track.
  - Samples are grouped by desc_index and decrypted with decrypt_par() so the
    Rust thread pool is invoked once per group instead of once per sample,
    eliminating thousands of ctypes round-trips per track.
  - Retry sleep uses time.sleep (in a thread) so the async event loop is not
    blocked.
"""

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

import structlog

logger = structlog.get_logger(__name__)

# Prefetch key used for the first sample description (desc_index 0)
PREFETCH_URI = "skd://itunes.apple.com/P000000000/s1/e1"

_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY = 1.0   # seconds; doubles each attempt (1 → 2 → 4 → 8)
_RETRY_ON_CODES  = {500, 503}

# ---------------------------------------------------------------------------
# Prefetch template cache — one instance for the entire process lifetime.
# prefetch.rs documents that the prefetch template (adamId=0) is universal:
# the content key never enters the round chain, so the same template decrypts
# every track produced by the same binary.
# ---------------------------------------------------------------------------
_PREFETCH_TEMPLATE = None  # temari.Temari | None


def _get_prefetch_template():
    """Return the process-global prefetch Temari handle, creating it on first call."""
    global _PREFETCH_TEMPLATE
    if _PREFETCH_TEMPLATE is None:
        try:
            from temari import Temari  # type: ignore
            _PREFETCH_TEMPLATE = Temari.prefetch()
        except Exception:
            pass  # will be created from JSON fallback in _decrypt_samples_temari
    return _PREFETCH_TEMPLATE


def _fetch_key_json(wrapper_base_url: str, adam_id: str, uri: str) -> tuple[bytes, list[str]]:
    """HTTP GET /key on wrapper-lite with exponential-backoff retry.

    Returns (temari_json_bytes, warnings) where warnings is a list of
    human-readable strings for any retried attempts. Logging is deferred
    to the async caller so it never races with the terminal spinner.

    Raises RuntimeError on final failure or non-retryable error.
    """
    url = (
        f"{wrapper_base_url.rstrip('/')}/key"
        f"?adamId={urllib.parse.quote(adam_id)}"
        f"&uri={urllib.parse.quote(uri)}"
    )

    warnings: list[str] = []
    last_exc: Exception | None = None

    for attempt in range(_RETRY_ATTEMPTS):
        if attempt:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            warnings.append(
                f"/key transient error (attempt {attempt}/{_RETRY_ATTEMPTS - 1})"
                f", retrying in {delay:.0f}s"
            )
            time.sleep(delay)

        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read()
        except urllib.error.URLError as exc:
            last_exc = exc
            warnings.append(f"/key network error (attempt {attempt + 1}): {exc}")
            continue

        resp = json.loads(body)
        code = resp.get("code")

        if code == 0:
            data = resp["data"]
            return (
                json.dumps(
                    {
                        "ctx":   data["ctx"],
                        "state": data["state"],
                        "rcx":   data.get("rcx", "0x0"),
                        "rax":   data.get("rax", "0x0"),
                        "rdx":   data.get("rdx", "0x0"),
                        "r9":    data.get("r9",  "0x0"),
                        "rbp":   data.get("rbp", "0x0"),
                    }
                ).encode(),
                warnings,
            )

        if code in _RETRY_ON_CODES:
            last_exc = RuntimeError(f"code={code} msg={resp.get('msg', '')}")
            warnings.append(f"/key server error code={code} (attempt {attempt + 1})")
            continue

        # Non-retryable (auth, bad request, …)
        raise RuntimeError(
            f"wrapper-lite /key failed for adamId={adam_id} uri={uri}: {resp}"
        )

    raise RuntimeError(
        f"wrapper-lite /key failed after {_RETRY_ATTEMPTS} attempts "
        f"(adamId={adam_id} uri={uri}): {last_exc}"
    )


def _decrypt_samples_temari(
    temari_json_prefetch: bytes,
    temari_json_track: bytes,
    samples,  # List[SampleInfo]
) -> bytes:
    """
    Decrypt CBCS samples with Temari using the two per-description templates.

    desc_index 0 → prefetch template (process-global cached, or from JSON)
    desc_index 1 → track template    (temari_json_track)

    Samples are grouped by desc_index and decrypted with decrypt_par() so the
    Rust worker pool is invoked once per group (not once per sample), cutting
    ctypes overhead from O(n_samples) to O(2) calls regardless of track length.

    CBCS full-subsample rule: only the 16-byte-aligned prefix is encrypted;
    the trailing (len % 16) bytes are kept as-is.
    """
    try:
        from temari import Temari  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "temari is not installed. Run: pip install temari"
        ) from exc

    # Prefetch template: reuse cached handle; only create from JSON as fallback
    # (e.g. if Temari.prefetch() failed because bundled template is unavailable).
    t0 = _get_prefetch_template()
    t0_owned = False
    if t0 is None:
        t0 = Temari.from_json(temari_json_prefetch)
        t0_owned = True

    t1 = Temari.from_json(temari_json_track)
    templates = {0: t0, 1: t1}

    try:
        # --- Group samples by desc_index to enable batch decrypt_par() -------
        # Each sample may have a trailing unencrypted tail (len % 16 != 0).
        # We record (original_index, enc_bytes, tail_bytes) so we can
        # reconstruct the output in submission order after batch decryption.
        groups: dict[int, list[tuple[int, bytes, bytes]]] = {}  # desc → [(idx, enc, tail)]
        for idx, sample in enumerate(samples):
            data = sample.data
            sample_len = len(data)
            truncated_len = sample_len & ~0xF
            enc  = data[:truncated_len] if truncated_len > 0 else b""
            tail = data[truncated_len:] if truncated_len < sample_len else b""
            di = sample.desc_index
            groups.setdefault(di, []).append((idx, enc, tail))

        # --- Batch decrypt each group ----------------------------------------
        plaintexts: list[bytes | None] = [None] * len(samples)
        for di, items in groups.items():
            tmpl = templates.get(di, t1)
            enc_chunks = [enc for (_, enc, _) in items]

            # decrypt_par handles empty slices gracefully (returns b"")
            decrypted = tmpl.decrypt_par(enc_chunks)

            for (orig_idx, _enc, tail), plain_enc in zip(items, decrypted):
                plaintexts[orig_idx] = plain_enc + tail

        return b"".join(plaintexts)  # type: ignore[arg-type]

    finally:
        if t0_owned:
            t0.close()
        t1.close()


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

    Pipeline: `extract_song` (MP4 demux, CPU-bound) and both /key fetches
    (network I/O) run **concurrently** via asyncio.gather.  On a typical
    Termux session the /key round-trips (~50–200 ms each) now overlap with
    the demux pass (~20–80 ms) instead of serialising, saving up to ~300 ms
    per track before decryption even starts.

    Args:
        wrapper_base_url: wrapper-lite HTTP base URL, e.g. "http://127.0.0.1:12340"
        adam_id:          Apple Music track ID
        fairplay_key:     FairPlay key URI (skd://...)
        input_path:       Path to encrypted M4A/MP4
        output_path:      Path for decrypted output
        progress_callback: Optional callback(current, total, bytes, speed)
    """
    from .amdecrypt import extract_song, write_decrypted_m4a  # local import avoids circular

    logger.debug("decrypt_temari", input=input_path, output=output_path)

    # Run MP4 demux and both /key HTTP fetches concurrently.
    # NOTE: prefetch URI must use adamId="0" — wrapper-lite rejects other IDs for it.
    (
        song_info,
        (json_prefetch, warns_pre),
        (json_track, warns_track),
    ) = await asyncio.gather(
        asyncio.to_thread(extract_song, input_path),
        asyncio.to_thread(_fetch_key_json, wrapper_base_url, "0", PREFETCH_URI),
        asyncio.to_thread(_fetch_key_json, wrapper_base_url, adam_id, fairplay_key),
    )
    key_warnings = warns_pre + warns_track

    def _run_decrypt():
        data = _decrypt_samples_temari(json_prefetch, json_track, song_info.samples)
        if progress_callback:
            total = len(song_info.samples)
            total_bytes = sum(len(s.data) for s in song_info.samples)
            progress_callback(total, total, total_bytes, 0)
        return data

    decrypted_data = await asyncio.to_thread(_run_decrypt)

    await asyncio.to_thread(
        write_decrypted_m4a,
        output_path,
        song_info,
        decrypted_data,
        input_path,
    )

    logger.debug("decrypt_temari_done", output=output_path)
    return key_warnings
