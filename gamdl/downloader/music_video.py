import asyncio
import json
from pathlib import Path

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia, DecryptionKeyAv
from ..utils import async_subprocess
from .base import AppleMusicBaseDownloader
from .enums import RemuxFormatMusicVideo, RemuxMode
from .types import DownloadItem

# Subtitle codecs that ffprobe may report but ffmpeg cannot transcode to mov_text.
# c608/c708 = CEA-608/708 closed captions embedded in H.264 SEI — not a real
# standalone stream, so converting them always fails with "Invalid data found".
_UNTRANSCODABLE_SUBTITLE_CODECS = {"eia_608", "eia_608_ccdt", "eia_708", "c608", "c708"}


class AppleMusicMusicVideoDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
        remux_mode: RemuxMode = RemuxMode.FFMPEG,
        remux_format: RemuxFormatMusicVideo = RemuxFormatMusicVideo.M4V,
    ):
        self.base = base
        self.remux_mode = remux_mode
        self.remux_format = remux_format

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _probe_has_convertible_subtitles(self, input_path: str) -> bool:
        """
        Return True  → video has a subtitle stream that ffmpeg CAN transcode to mov_text.
        Return False → no subtitle stream, or only untranscodable CC tracks (c608/c708).
        """
        ffprobe_path = self.base.full_ffmpeg_path
        if ffprobe_path:
            # Prefer ffprobe sitting next to ffmpeg
            candidate = str(Path(ffprobe_path).parent / "ffprobe")
            import shutil
            ffprobe_path = shutil.which(candidate) or shutil.which("ffprobe") or None

        if not ffprobe_path:
            # ffprobe not available: fall back to safe behaviour (skip mov_text)
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-select_streams", "s",   # subtitle streams only
                input_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return False

            info = json.loads(stdout.decode())
            streams = info.get("streams", [])

            if not streams:
                return False   # no subtitle streams at all

            for s in streams:
                codec = s.get("codec_name", "").lower()
                if codec not in _UNTRANSCODABLE_SUBTITLE_CODECS:
                    return True  # at least one convertible subtitle track exists

            return False  # only untranscodable CC tracks found

        except Exception:
            return False  # probe failed → safe default

    # ── remux methods ─────────────────────────────────────────────────────────

    async def _remux_mp4box(
        self,
        input_path_video: str,
        input_path_audio: str,
        output_path: str,
    ):
        await async_subprocess(
            self.base.full_mp4box_path,
            "-quiet",
            "-add",
            input_path_audio,
            "-add",
            input_path_video,
            "-itags",
            "artist=placeholder",
            "-keep-utc",
            "-new",
            output_path,
            silent=self.base.silent,
        )

    async def _remux_ffmpeg(
        self,
        input_path_video: str,
        input_path_audio: str,
        output_path: str,
    ):
        # Probe first: only pass -c:s mov_text when the video actually has a
        # subtitle track that ffmpeg can transcode.  CEA-608/708 closed-caption
        # tracks embedded in H.264 SEI cause "Invalid data found" and must be
        # dropped with -sn instead.
        has_convertible_subs = await self._probe_has_convertible_subtitles(
            input_path_video
        )

        subtitle_args: list[str]
        if has_convertible_subs:
            subtitle_args = ["-c:s", "mov_text"]
        else:
            subtitle_args = ["-sn"]   # drop untranscodable / absent subtitle tracks

        await async_subprocess(
            self.base.full_ffmpeg_path,
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path_video,
            "-i",
            input_path_audio,
            "-c",
            "copy",
            *subtitle_args,
            "-movflags",
            "+faststart",
            output_path,
            silent=self.base.silent,
        )

    async def _decrypt_mp4decrypt(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
    ):
        await async_subprocess(
            self.base.full_mp4decrypt_path,
            "--key",
            f"1:{decryption_key}",
            input_path,
            output_path,
            silent=self.base.silent,
        )

    async def stage(
        self,
        encrypted_path_video: str,
        encrypted_path_audio: str,
        decrypted_path_video: str,
        decrypted_path_audio: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
    ):
        await self._decrypt_mp4decrypt(
            encrypted_path_video,
            decrypted_path_video,
            decryption_key.video_track.key,
        )
        await self._decrypt_mp4decrypt(
            encrypted_path_audio,
            decrypted_path_audio,
            decryption_key.audio_track.key,
        )

        if self.remux_mode == RemuxMode.MP4BOX:
            await self._remux_mp4box(
                decrypted_path_video,
                decrypted_path_audio,
                staged_path,
            )
        else:
            await self._remux_ffmpeg(
                decrypted_path_video,
                decrypted_path_audio,
                staged_path,
            )

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
    ) -> str:
        return str(Path(final_path).with_suffix(file_extension))

    async def get_download_item(
        self,
        media: AppleMusicMedia,
    ) -> DownloadItem:
        download_item = DownloadItem(media)

        download_item.staged_path = self.base.get_temp_path(
            media.media_metadata["id"],
            download_item.uuid_,
            "staged",
            "." + media.stream_info.file_format.value,
        )

        final_extension = (
            "." + self.remux_format.value
            if self.remux_format is not None
            else "." + media.stream_info.file_format.value
        )

        download_item.final_path = self.base.get_final_path(
            media.tags,
            final_extension,
            media.playlist_tags,
        )

        if media.playlist_tags:
            download_item.playlist_file_path = self.base.get_playlist_file_path(
                media.playlist_tags,
            )

        download_item.cover_path = self.get_cover_path(
            download_item.final_path,
            media.cover.file_extension,
        )

        return download_item

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        encrypted_path_video = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "encrypted_video",
            ".mp4",
        )
        encrypted_path_audio = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "encrypted_audio",
            ".m4a",
        )

        await self.base.download_stream(
            download_item.media.stream_info.video_track.stream_url,
            encrypted_path_video,
        )
        await self.base.download_stream(
            download_item.media.stream_info.audio_track.stream_url,
            encrypted_path_audio,
        )

        decrypted_path_video = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "decrypted_video",
            ".mp4",
        )
        decrypted_path_audio = self.base.get_temp_path(
            download_item.media.media_metadata["id"],
            download_item.uuid_,
            "decrypted_audio",
            ".m4a",
        )

        await self.stage(
            encrypted_path_video,
            encrypted_path_audio,
            decrypted_path_video,
            decrypted_path_audio,
            download_item.staged_path,
            download_item.media.decryption_key,
        )

        cover_bytes = (
            await self.base.interface.base.get_cover_bytes(
                download_item.media.cover.url
            )
            if self.base.interface.base.cover_format != CoverFormat.RAW
            else None
        )
        await self.base.apply_tags(
            download_item.staged_path,
            download_item.media.tags,
            cover_bytes,
        )
