from __future__ import annotations

"""Audio validation and normalization utilities for uploaded WAV files."""

import logging
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.errors import InvalidInputError, ProcessingError

logger = logging.getLogger("calls_category_api.audio")

ALLOWED_MIME_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/vnd.wave",
    "application/octet-stream",
}


@dataclass(frozen=True)
class WavInfo:
    """Basic WAV header information used for validation and metadata."""

    channels: int
    sample_rate_hz: int
    sample_width_bytes: int
    frame_count: int
    duration_sec: float


@dataclass(frozen=True)
class PreparedSttAudio:
    """Information about the audio file that will actually be sent to STT."""

    path: Path
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
    normalization_applied: bool


def validate_upload_declared_type(filename: Optional[str], content_type: Optional[str]) -> None:
    """Validate declared filename extension and request MIME type.

    This is an early coarse filter; real file integrity is checked later by
    reading the WAV header with `inspect_wav`.
    """
    logger.info(
        "audio.validate_upload_declared_type started filename=%s content_type=%s",
        filename,
        content_type,
    )
    if not filename:
        raise InvalidInputError("missing_filename", "Uploaded file must include a filename")
    if not filename.lower().endswith(".wav"):
        raise InvalidInputError("unsupported_extension", "Only .wav files are supported")
    if content_type:
        mime_type = content_type.split(";")[0].strip().lower()
        if mime_type not in ALLOWED_MIME_TYPES:
            raise InvalidInputError("unsupported_mime_type", f"Unsupported content type: {content_type}")
    logger.info("audio.validate_upload_declared_type completed filename=%s", filename)


def inspect_wav(path: Path, max_duration_seconds: int) -> WavInfo:
    """Read WAV header and validate codec, channels, sample rate, and duration."""
    logger.info(
        "audio.inspect_wav started path=%s max_duration_seconds=%s",
        path,
        max_duration_seconds,
    )
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate_hz = wav.getframerate()
            sample_width_bytes = wav.getsampwidth()
            frame_count = wav.getnframes()
            comptype = wav.getcomptype()
    except wave.Error as exc:
        logger.exception("audio.inspect_wav failed invalid wave header path=%s", path)
        raise InvalidInputError("invalid_wav", f"Invalid WAV file: {exc}") from exc

    if comptype != "NONE":
        raise InvalidInputError("unsupported_wav_codec", "WAV must be PCM/uncompressed")
    if channels < 1:
        raise InvalidInputError("invalid_audio_channels", "WAV must include at least one channel")
    if sample_rate_hz <= 0:
        raise InvalidInputError("invalid_sample_rate", "WAV sample rate must be positive")
    if sample_width_bytes <= 0:
        raise InvalidInputError("invalid_bit_depth", "WAV sample width must be positive")

    duration_sec = frame_count / sample_rate_hz if sample_rate_hz > 0 else 0.0
    if duration_sec > max_duration_seconds:
        max_minutes = max_duration_seconds // 60
        raise InvalidInputError(
            "audio_too_long",
            f"Audio duration exceeds {max_minutes} minutes limit",
        )

    info = WavInfo(
        channels=channels,
        sample_rate_hz=sample_rate_hz,
        sample_width_bytes=sample_width_bytes,
        frame_count=frame_count,
        duration_sec=round(duration_sec, 3),
    )
    logger.info(
        "audio.inspect_wav completed path=%s channels=%s sample_rate_hz=%s sample_width_bytes=%s frame_count=%s duration_sec=%s",
        path,
        info.channels,
        info.sample_rate_hz,
        info.sample_width_bytes,
        info.frame_count,
        info.duration_sec,
    )
    return info


def ffmpeg_is_available(ffmpeg_binary: str) -> bool:
    """Return whether the configured ffmpeg binary is available on this host."""
    if Path(ffmpeg_binary).is_file():
        return True
    return shutil.which(ffmpeg_binary) is not None


def supports_direct_stt_input(wav_info: WavInfo) -> bool:
    """Return whether the original WAV can be used directly without conversion.

    The fallback path intentionally stays conservative and only accepts the
    format this project is designed around: mono PCM16 WAV at 8kHz or 16kHz.
    """
    return (
        wav_info.channels == 1
        and wav_info.sample_width_bytes == 2
        and wav_info.sample_rate_hz in {8000, 16000}
    )


def prepare_audio_for_stt(
    input_path: Path,
    output_path: Path,
    source_wav_info: WavInfo,
    ffmpeg_binary: str,
    enable_ffmpeg: bool,
) -> PreparedSttAudio:
    """Prepare the audio file for STT.

    If `ffmpeg` is enabled and available, the audio is normalized to mono
    16kHz PCM16. Otherwise, a compatible source WAV is used directly.
    """
    if enable_ffmpeg and ffmpeg_is_available(ffmpeg_binary):
        normalize_audio_for_stt(input_path=input_path, output_path=output_path, ffmpeg_binary=ffmpeg_binary)
        return PreparedSttAudio(
            path=output_path,
            sample_rate_hz=16000,
            channels=1,
            sample_width_bytes=2,
            normalization_applied=True,
        )

    if supports_direct_stt_input(source_wav_info):
        reason = "ffmpeg disabled by configuration" if not enable_ffmpeg else "ffmpeg not available"
        logger.warning(
            "audio.prepare_audio_for_stt %s; using original WAV directly input=%s sample_rate_hz=%s channels=%s sample_width_bytes=%s",
            reason,
            input_path,
            source_wav_info.sample_rate_hz,
            source_wav_info.channels,
            source_wav_info.sample_width_bytes,
        )
        return PreparedSttAudio(
            path=input_path,
            sample_rate_hz=source_wav_info.sample_rate_hz,
            channels=source_wav_info.channels,
            sample_width_bytes=source_wav_info.sample_width_bytes,
            normalization_applied=False,
        )

    reason = "ffmpeg is disabled" if not enable_ffmpeg else "ffmpeg is not available"
    raise ProcessingError(
        "audio_normalization_unavailable",
        f"{reason} and the uploaded WAV cannot be sent directly to STT. "
        "Provide mono PCM16 WAV at 8kHz or 16kHz, or install ffmpeg.",
    )


def normalize_audio_for_stt(input_path: Path, output_path: Path, ffmpeg_binary: str) -> None:
    """Normalize source audio into mono 16kHz PCM16 WAV using ffmpeg."""
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(output_path),
    ]
    logger.info(
        "audio.normalize_audio_for_stt started input=%s output=%s ffmpeg_binary=%s",
        input_path,
        output_path,
        ffmpeg_binary,
    )
    logger.debug("audio.normalize_audio_for_stt command=%s", command)
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        details = process.stderr.strip() or "ffmpeg failed without stderr output"
        logger.error(
            "audio.normalize_audio_for_stt failed returncode=%s stderr=%s",
            process.returncode,
            details,
        )
        raise ProcessingError("audio_normalization_failed", details)
    if not output_path.exists():
        logger.error("audio.normalize_audio_for_stt failed output file missing output=%s", output_path)
        raise ProcessingError("audio_normalization_failed", "ffmpeg did not produce output file")
    logger.info(
        "audio.normalize_audio_for_stt completed output=%s output_size_bytes=%s",
        output_path,
        output_path.stat().st_size,
    )
