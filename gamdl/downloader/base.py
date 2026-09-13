import asyncio
import os
import re
import shutil
from pathlib import Path

import structlog
from mutagen.mp4 import MP4, MP4Cover
from yt_dlp import YoutubeDL

from ..interface.enums import CoverFormat
from ..interface.interface import AppleMusicInterface
from ..interface.types import MediaTags, PlaylistTags
from ..utils import CustomStringFormatter, async_subprocess
from .constants import ILLEGAL_CHAR_REPLACEMENT, ILLEGAL_CHARS_RE, TEMP_PATH_TEMPLATE
from .enums import DownloadMode

logger = structlog.get_logger(__name__)


def _is_termux() -> bool:
    """Detect Termux: PREFIX env var points to /data/data/com.termux/files/usr."""
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix


class _StructlogYtdlpLogger:
    """Bridge yt-dlp's logger interface to structlog.

    When yt-dlp is given a logger object it uses MultilineLogger which routes
    all output through debug/warning/error instead of writing directly to stdout.
    That means no ANSI codes, no \\r, no spam — just structured log lines that
    structlog can format and filter normally.
    """

    # yt-dlp warning substrings that are irrelevant noise for gamdl users.
    _SUPPRESSED_WARNINGS = (
        # Emitted whenever allow_unplayable_formats=True; gamdl always sets
        # this flag intentionally to download encrypted HLS segments.
        "You have asked for UNPLAYABLE formats",
    )

    def __init__(self, bound_logger):
        self._log = bound_logger

    def debug(self, msg: str) -> None:
        # yt-dlp prefixes every progress line with "[download]"; skip at debug
        # level — _TermuxProgressBar handles visual feedback instead.
        if msg.startswith("[download]"):
            return
        self._log.debug("ytdlp", msg=msg.strip())

    def info(self, msg: str) -> None:
        self._log.info("ytdlp", msg=msg.strip())

    def warning(self, msg: str) -> None:
        stripped = msg.strip()
        if any(s in stripped for s in self._SUPPRESSED_WARNINGS):
            return
        self._log.warning("ytdlp", msg=stripped)

    def error(self, msg: str) -> None:
        self._log.error("ytdlp", msg=msg.strip())


try:
    from rich.console import Console as _RichConsole
    from rich.progress import (
        BarColumn as _BarColumn,
        DownloadColumn as _DownloadColumn,
        Progress as _RichProgress,
        ProgressColumn as _ProgressColumn,
        Task as _RichTask,
        TaskProgressColumn as _TaskProgressColumn,
    )
    from rich.text import Text as _RichText

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RICH_AVAILABLE = False


if _RICH_AVAILABLE:

    class _YtdlpSpeedColumn(_ProgressColumn):
        """Renders the real-time speed supplied by yt-dlp's progress hook.

        Rich's built-in TransferSpeedColumn measures elapsed wall-clock time
        inside the Progress context, which gives incorrect results when the
        hook fires irregularly.  Injecting the speed reported directly by
        yt-dlp (``task.fields['speed']``) is both more accurate and simpler.
        """

        def render(self, task: "_RichTask") -> "_RichText":
            bps = task.fields.get("speed")
            if not bps:
                return _RichText("···", style="dim")
            if bps >= 1_048_576:
                return _RichText(f"{bps / 1_048_576:.1f} MiB/s", style="bold green")
            if bps >= 1024:
                return _RichText(f"{bps / 1024:.0f} KiB/s", style="bold green")
            return _RichText(f"{bps:.0f} B/s", style="dim green")

    class _TermuxProgressBar:
        """Rich-powered progress bar for Termux.

        Uses ``rich.progress.Progress`` with a custom speed column that
        injects the real-time speed value supplied by yt-dlp's progress hook
        instead of measuring wall-clock elapsed time.

        Features:
        - Smooth animated bar with colour (cyan → green on completion)
        - Accurate speed display sourced directly from yt-dlp
        - Downloaded / total size (e.g. ``5.2/10.0 MiB``)
        - ``transient=True`` — bar disappears cleanly after each download
        - Pulse animation when total size is unknown
        - Graceful fallback to plain-text bar if rich is ever unavailable

        Example output (Termux, ~44 cols):
            ━━━━━╺━━━━  52% 5.2/10.0 MiB  5.0 MiB/s
            ━━━━━━━━━━ 100% 10.0/10.0 MiB  4.8 MiB/s
        """

        def __init__(self) -> None:
            cols = self._get_cols()
            self._console = _RichConsole(stderr=True, width=cols, force_terminal=True)
            self._progress = _RichProgress(
                _BarColumn(
                    bar_width=10,
                    complete_style="bold cyan",
                    finished_style="bold green",
                    pulse_style="dim cyan",
                ),
                _TaskProgressColumn(),
                _DownloadColumn(binary_units=True),
                _YtdlpSpeedColumn(),
                console=self._console,
                transient=True,
                auto_refresh=False,
            )
            self._task_id = None
            self._progress.start()

        @staticmethod
        def _get_cols() -> int:
            try:
                return shutil.get_terminal_size((40, 24)).columns
            except Exception:
                return int(os.environ.get("COLUMNS", 40))

        # ------------------------------------------------------------------
        # progress hook — called by yt-dlp on every update
        # ------------------------------------------------------------------
        def __call__(self, d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                self._render(d)
            elif status == "finished":
                self._finish(d)
            elif status == "error":
                self._abort()

        # ------------------------------------------------------------------
        # internal helpers
        # ------------------------------------------------------------------
        def _render(self, d: dict) -> None:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0) or 0
            speed = d.get("speed")

            if self._task_id is None:
                self._task_id = self._progress.add_task(
                    "", total=total, speed=speed
                )
            else:
                self._progress.update(
                    self._task_id,
                    total=total,
                    completed=downloaded,
                    speed=speed,
                )
            self._progress.refresh()

        def _finish(self, d: dict) -> None:
            total = d.get("total_bytes") or d.get("downloaded_bytes")
            if self._task_id is not None:
                self._progress.update(
                    self._task_id, completed=total, speed=None
                )
                self._progress.refresh()
            self._progress.stop()
            self._task_id = None

        def _abort(self) -> None:
            self._progress.stop()
            self._task_id = None

else:  # pragma: no cover — rich not installed

    class _TermuxProgressBar:  # type: ignore[no-redef]
        """Fallback slim bar (plain Unicode blocks) used when rich is absent."""

        _FILL = "█"
        _EMPTY = "░"
        _BOUNCE = "»"
        _BAR_WIDTH = 10

        def __init__(self) -> None:
            self._started = False

        def __call__(self, d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                self._render(d)
            elif status == "finished":
                self._finish(d)
            elif status == "error":
                self._clear()

        @staticmethod
        def _fmt_speed(bps) -> str:
            if bps is None:
                return "---"
            if bps >= 1_048_576:
                return f"{bps / 1_048_576:.1f}M/s"
            if bps >= 1024:
                return f"{bps / 1024:.0f}K/s"
            return f"{bps:.0f}B/s"

        @staticmethod
        def _fmt_size(b) -> str:
            if b is None:
                return "?"
            if b >= 1_048_576:
                return f"{b / 1_048_576:.1f}M"
            if b >= 1024:
                return f"{b / 1024:.0f}K"
            return f"{b}B"

        def _render(self, d: dict) -> None:
            import sys

            downloaded = d.get("downloaded_bytes", 0) or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            speed = d.get("speed")
            bw = self._BAR_WIDTH

            if total:
                pct = min(downloaded / total, 1.0)
                filled = round(pct * bw)
                bar = self._FILL * filled + self._EMPTY * (bw - filled)
                pct_str = f"{pct * 100:3.0f}%"
            else:
                pos = (downloaded // 65536) % max(bw - 1, 1)
                bar = (
                    self._EMPTY * pos
                    + self._BOUNCE * 2
                    + self._EMPTY * max(bw - pos - 2, 0)
                )
                bar = bar[:bw]
                pct_str = self._fmt_size(downloaded)

            speed_str = self._fmt_speed(speed)
            sys.stderr.write(f"\r{bar} {pct_str} {speed_str:<10}")
            sys.stderr.flush()
            self._started = True

        def _finish(self, d: dict) -> None:
            import sys

            total = d.get("total_bytes") or d.get("downloaded_bytes")
            elapsed = d.get("elapsed")
            speed = (total / elapsed) if (total and elapsed) else None
            bar = self._FILL * self._BAR_WIDTH
            sys.stderr.write(
                f"\r{bar} done {self._fmt_size(total)}@{self._fmt_speed(speed)}          \n"
            )
            sys.stderr.flush()
            self._started = False

        def _clear(self) -> None:
            import sys

            if self._started:
                sys.stderr.write("\r" + " " * 40 + "\r")
                sys.stderr.flush()
                self._started = False


if _RICH_AVAILABLE:

    class _TermuxSpinner:
        """Rich-powered indeterminate spinner for Termux.

        Displayed during blocking operations that have no byte-count progress:
        temari decryption, mp4decrypt, ffmpeg remux, integrity check, tag write.

        Usage (async context manager)::

            async with _TermuxSpinner("⠋ Decrypting..."):
                await some_slow_coroutine()

        The spinner prints to stderr, is ``transient=True`` (disappears on
        ``__aexit__``), and is safe to nest — each instance owns its own
        ``rich.progress.Progress`` context so they never interfere.
        """

        # Braille dots feel snappy and render as a single column on Termux fonts.
        _SPINNER = "dots"

        def __init__(self, label: str) -> None:
            self._label = label
            self._console = _RichConsole(
                stderr=True,
                width=shutil.get_terminal_size((40, 24)).columns,
                force_terminal=True,
            )
            # Import here to keep the top-level import block clean.
            from rich.progress import SpinnerColumn, TextColumn

            self._progress = _RichProgress(
                SpinnerColumn(self._SPINNER, style="bold cyan"),
                TextColumn("[cyan]{task.description}"),
                console=self._console,
                transient=True,
            )

        async def __aenter__(self) -> "_TermuxSpinner":
            self._progress.start()
            self._task_id = self._progress.add_task(self._label)
            return self

        async def __aexit__(self, *_) -> None:
            self._progress.stop()

else:  # pragma: no cover

    class _TermuxSpinner:  # type: ignore[no-redef]
        """Fallback plain-text spinner used when rich is absent."""

        _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

        def __init__(self, label: str) -> None:
            self._label = label
            self._task: "asyncio.Task | None" = None

        async def __aenter__(self) -> "_TermuxSpinner":
            self._task = asyncio.get_event_loop().create_task(self._spin())
            return self

        async def __aexit__(self, *_) -> None:
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            import sys
            sys.stderr.write("\r" + " " * (len(self._label) + 4) + "\r")
            sys.stderr.flush()

        async def _spin(self) -> None:
            import sys
            i = 0
            while True:
                sys.stderr.write(f"\r{self._frames[i % len(self._frames)]} {self._label}")
                sys.stderr.flush()
                await asyncio.sleep(0.1)
                i += 1


class _NoopSpinner:
    """No-op async context manager — used on non-Termux terminals."""

    async def __aenter__(self) -> "_NoopSpinner":
        return self

    async def __aexit__(self, *_) -> None:
        pass


class AppleMusicBaseDownloader:
    def __init__(
        self,
        interface: AppleMusicInterface,
        output_path: str = "./Apple Music",
        temp_path: str = ".",
        nm3u8dlre_path: str = "N_m3u8DL-RE",
        mp4decrypt_path: str = "mp4decrypt",
        ffmpeg_path: str = "ffmpeg",
        mp4box_path: str = "MP4Box",
        wrapper_url: str = "http://127.0.0.1:12340",
        download_mode: DownloadMode = DownloadMode.YTDLP,
        album_folder_template: str = "{album_artist}/{album}",
        compilation_folder_template: str = "Compilations/{album}",
        no_album_folder_template: str = "{artist}/Unknown Album",
        playlist_folder_template: str = "Playlists/{playlist_artist}",
        single_disc_file_template: str = "{track:02d} {title}",
        multi_disc_file_template: str = "{disc}-{track:02d} {title}",
        no_album_file_template: str = "{title}",
        playlist_file_template: str = "{playlist_title}",
        date_tag_template: str = "%Y-%m-%dT%H:%M:%SZ",
        exclude_tags: list[str] = None,
        truncate: int = None,
        silent: bool = False,
        embed_synced_lyrics: bool = False,
    ):
        self.interface = interface
        self.output_path = output_path
        self.temp_path = temp_path
        self.nm3u8dlre_path = nm3u8dlre_path
        self.mp4decrypt_path = mp4decrypt_path
        self.ffmpeg_path = ffmpeg_path
        self.mp4box_path = mp4box_path
        self.wrapper_url = wrapper_url
        self.download_mode = download_mode
        self.album_folder_template = album_folder_template
        self.compilation_folder_template = compilation_folder_template
        self.no_album_folder_template = no_album_folder_template
        self.single_disc_file_template = single_disc_file_template
        self.multi_disc_file_template = multi_disc_file_template
        self.playlist_folder_template = playlist_folder_template
        self.no_album_file_template = no_album_file_template
        self.playlist_file_template = playlist_file_template
        self.date_tag_template = date_tag_template
        self.exclude_tags = exclude_tags
        self.truncate = truncate
        self.silent = silent
        self.embed_synced_lyrics = embed_synced_lyrics

        self._initialize_binary_paths()

    def spinner(self, label: str) -> "_TermuxSpinner":
        """Return a spinner context manager for *label*.

        On Termux (and only when not in silent mode) this renders a Rich
        braille-dot spinner to stderr.  On non-Termux terminals it returns a
        no-op context manager so call-sites can always write::

            async with self.base.spinner("Decrypting..."):
                await slow_operation()

        without any platform check at the call site.
        """
        if _is_termux() and not self.silent:
            return _TermuxSpinner(label)
        return _NoopSpinner()

    def _initialize_binary_paths(self):
        log = logger.bind(action="initialize_binary_paths")

        self.full_nm3u8dlre_path = shutil.which(self.nm3u8dlre_path)
        self.full_mp4decrypt_path = shutil.which(self.mp4decrypt_path)
        self.full_ffmpeg_path = shutil.which(self.ffmpeg_path)
        self.full_mp4box_path = shutil.which(self.mp4box_path)

        log = log.debug(
            "success",
            full_nm3u8dlre_path=self.full_nm3u8dlre_path,
            full_mp4decrypt_path=self.full_mp4decrypt_path,
            full_ffmpeg_path=self.full_ffmpeg_path,
            full_mp4box_path=self.full_mp4box_path,
        )

    def get_temp_path(
        self,
        media_id: str,
        folder_tag: str,
        file_tag: str,
        file_extension: str,
    ) -> str:
        log = logger.bind(action="get_temp_path")

        temp_path = str(
            Path(self.temp_path)
            / TEMP_PATH_TEMPLATE.format(folder_tag)
            / (f"{media_id}_{file_tag}" + file_extension)
        )

        log.debug("success", temp_path=temp_path)

        return temp_path

    def _sanitize_string(
        self,
        dirty_string: str,
        file_ext: str = None,
    ) -> str:
        sanitized_string = re.sub(
            ILLEGAL_CHARS_RE,
            ILLEGAL_CHAR_REPLACEMENT,
            dirty_string,
        )

        if file_ext is None:
            sanitized_string = sanitized_string[: self.truncate]
            if sanitized_string.endswith("."):
                sanitized_string = sanitized_string[:-1] + ILLEGAL_CHAR_REPLACEMENT
        else:
            if self.truncate is not None:
                sanitized_string = sanitized_string[: self.truncate - len(file_ext)]
            sanitized_string += file_ext

        return sanitized_string.strip()

    def get_final_path(
        self,
        tags: MediaTags,
        file_extension: str,
        playlist_tags: PlaylistTags | None,
    ) -> str:
        log = logger.bind(action="get_final_path")

        if tags.album:
            template_folder_parts = (
                self.compilation_folder_template.split("/")
                if tags.compilation
                else self.album_folder_template.split("/")
            )
        else:
            template_folder_parts = self.no_album_folder_template.split("/")

        if tags.album:
            template_file_parts = (
                self.multi_disc_file_template.split("/")
                if isinstance(tags.disc_total, int) and tags.disc_total > 1
                else self.single_disc_file_template.split("/")
            )
        else:
            template_file_parts = self.no_album_file_template.split("/")

        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                album=(tags.album, "Unknown Album"),
                album_artist=(tags.album_artist, "Unknown Artist"),
                album_id=(tags.album_id, "Unknown Album ID"),
                artist=(tags.artist, "Unknown Artist"),
                artist_id=(tags.artist_id, "Unknown Artist ID"),
                composer=(tags.composer, "Unknown Composer"),
                composer_id=(tags.composer_id, "Unknown Composer ID"),
                date=(tags.date, "Unknown Date"),
                disc=(tags.disc, ""),
                disc_total=(tags.disc_total, ""),
                media_type=(tags.media_type, "Unknown Media Type"),
                playlist_artist=(
                    (playlist_tags.artist if playlist_tags else None),
                    "Unknown Playlist Artist",
                ),
                playlist_id=(
                    (playlist_tags.playlist_id if playlist_tags else None),
                    "Unknown Playlist ID",
                ),
                playlist_title=(
                    (playlist_tags.title if playlist_tags else None),
                    "Unknown Playlist Title",
                ),
                playlist_track=(
                    (playlist_tags.track if playlist_tags else None),
                    "",
                ),
                title=(tags.title, "Unknown Title"),
                title_id=(tags.title_id, "Unknown Title ID"),
                track=(tags.track, ""),
                track_total=(tags.track_total, ""),
            )
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_extension if not is_folder else None,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        log.debug("success", final_path=final_path)

        return final_path

    async def download_stream(self, stream_url: str, download_path: str):
        log = logger.bind(
            action="download_stream", stream_url=stream_url, download_path=download_path
        )

        if self.download_mode == DownloadMode.YTDLP:
            await self._download_ytdlp_async(stream_url, download_path)

        if self.download_mode == DownloadMode.NM3U8DLRE:
            await self._download_nm3u8dlre(stream_url, download_path)

        log.debug("success")

    async def _download_ytdlp_async(self, stream_url: str, download_path: str) -> None:
        await asyncio.to_thread(
            self._download_ytdlp_sync,
            stream_url,
            download_path,
        )

    def _download_ytdlp_sync(self, stream_url: str, download_path: str) -> None:
        # On Termux, supports_terminal_sequences() returns False because the
        # TERM env var is often unset inside scripts.  yt-dlp then falls back
        # to BreaklineStatusPrinter which emits one "\n"-terminated line per
        # progress update — producing hundreds of [download] spam lines.
        #
        # Fix: on Termux we route all yt-dlp output through _StructlogYtdlpLogger
        # (which silences [download] lines) and attach _TermuxProgressBar as a
        # progress_hook so the user still gets a compact single-line bar that
        # overwrites itself via "\r".  On normal terminals the existing behaviour
        # is preserved.
        on_termux = _is_termux()

        ydl_opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": download_path,
            "allow_unplayable_formats": True,
            "overwrites": True,
            "fixup": "never",
            "noprogress": self.silent,
            "allowed_extractors": ["generic"],
        }

        if on_termux and not self.silent:
            ydl_opts["logger"] = _StructlogYtdlpLogger(
                logger.bind(action="ytdlp_download")
            )
            # _TermuxProgressBar renders via progress_hooks, which fire
            # independently of noprogress/logger, so we get the bar even
            # though the logger suppresses all [download] text lines.
            ydl_opts["progress_hooks"] = [_TermuxProgressBar()]
            # Bypass the UNPLAYABLE warning entirely: remove the flag from the
            # opts dict passed to YoutubeDL.__init__ (which fires the warning),
            # then inject it directly into ydl.params after construction so the
            # actual download still works with unplayable formats enabled.
            ydl_opts.pop("allow_unplayable_formats", None)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.params["allow_unplayable_formats"] = True
                ydl.download(stream_url)
        elif on_termux and self.silent:
            # silent mode: suppress everything, no bar needed
            ydl_opts["logger"] = _StructlogYtdlpLogger(
                logger.bind(action="ytdlp_download")
            )
            ydl_opts.pop("allow_unplayable_formats", None)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.params["allow_unplayable_formats"] = True
                ydl.download(stream_url)
        else:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download(stream_url)

    async def _download_nm3u8dlre(self, stream_url: str, download_path: str):
        download_path_obj = Path(download_path)

        download_path_obj.parent.mkdir(parents=True, exist_ok=True)
        await async_subprocess(
            self.full_nm3u8dlre_path,
            stream_url,
            "--binary-merge",
            "--no-log",
            "--log-level",
            "off",
            "--ffmpeg-binary-path",
            self.full_ffmpeg_path,
            "--save-name",
            download_path_obj.stem,
            "--save-dir",
            download_path_obj.parent,
            "--tmp-dir",
            download_path_obj.parent,
            silent=self.silent,
        )

    async def apply_tags(
        self,
        media_path: str,
        tags: MediaTags,
        cover_bytes: bytes | None,
        synced_lyrics: str | None = None,
    ):
        log = logger.bind(action="apply_tags", media_path=media_path)

        exclude_tags = self.exclude_tags or []

        filtered_tags = MediaTags(
            **{
                k: v
                for k, v in tags.__dict__.items()
                if v is not None and k not in exclude_tags
            }
        )
        mp4_tags = filtered_tags.as_mp4_tags(self.date_tag_template)

        skip_tagging = "all" in exclude_tags

        async with self.spinner("Writing tags…"):
            await asyncio.to_thread(
                self._apply_mp4_tags,
                media_path,
                mp4_tags,
                cover_bytes,
                skip_tagging,
                synced_lyrics,
            )

        log.debug("success")

    def _apply_mp4_tags(
        self,
        media_path: str,
        tags: dict,
        cover_bytes: bytes | None,
        skip_tagging: bool,
        synced_lyrics: str | None = None,
    ):
        mp4 = MP4(media_path)
        mp4.clear()

        if not skip_tagging:
            if cover_bytes is not None:
                mp4["covr"] = [
                    MP4Cover(
                        data=cover_bytes,
                        imageformat=(
                            MP4Cover.FORMAT_JPEG
                            if self.interface.base.cover_format == CoverFormat.JPG
                            else MP4Cover.FORMAT_PNG
                        ),
                    )
                ]
            mp4.update(tags)

            if synced_lyrics:
                mp4["\xa9lyr"] = [synced_lyrics]

        mp4.save()

    def get_playlist_file_path(
        self,
        tags: PlaylistTags,
    ) -> str:
        log = logger.bind(action="get_playlist_file_path")

        template_folder_parts = self.playlist_folder_template.split("/")
        template_file_parts = self.playlist_file_template.split("/")
        template_parts = template_folder_parts + template_file_parts
        formatted_parts = []

        for i, part in enumerate(template_parts):
            is_folder = i < len(template_parts) - 1
            formatted_part = CustomStringFormatter().format(
                part,
                playlist_artist=(tags.artist, "Unknown Playlist Artist"),
                playlist_id=(tags.playlist_id, "Unknown Playlist ID"),
                playlist_title=(tags.title, "Unknown Playlist Title"),
                playlist_track=(tags.track, ""),
            )
            file_ext = None if is_folder else ".m3u"
            sanitized_formatted_part = self._sanitize_string(
                formatted_part,
                file_ext,
            )
            formatted_parts.append(sanitized_formatted_part)

        final_path = str(Path(self.output_path, *formatted_parts))

        log.debug("success", playlist_file_path=final_path)

        return final_path
