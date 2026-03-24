from __future__ import annotations

"""Azure Speech transcription service wrapper."""

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.errors import ProcessingError, RateLimitError, UpstreamTimeoutError, UpstreamUnavailableError
from app.models import TranscriptionSegment

logger = logging.getLogger("calls_category_api.speech")


@dataclass
class SpeechTranscription:
    """Structured STT output consumed by the API endpoint."""

    text: str
    segments: list[TranscriptionSegment] = field(default_factory=list)
    detected_languages: list[str] = field(default_factory=list)


def _import_speech_sdk():
    """Import Azure Speech SDK lazily and raise API error if missing."""
    logger.info("speech._import_speech_sdk started")
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:  # pragma: no cover
        logger.exception("speech._import_speech_sdk failed")
        raise ProcessingError(
            "speech_sdk_missing",
            "azure-cognitiveservices-speech is not installed",
        ) from exc
    logger.info("speech._import_speech_sdk completed")
    return speechsdk


class SpeechService:
    """Transcribe normalized audio with Azure Speech and retry policy."""

    def __init__(
        self,
        key: str,
        region: str,
        languages: list[str],
        timeout_seconds: int,
        max_attempts: int,
        retry_base_delay_ms: int,
        verbose_ai_logs: bool,
    ) -> None:
        """Initialize speech client settings and retry configuration."""
        self.key = key
        self.region = region
        self.languages = languages
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_base_delay_ms = retry_base_delay_ms
        self.verbose_ai_logs = verbose_ai_logs
        logger.info(
            "speech.SpeechService initialized region=%s languages=%s timeout_seconds=%s max_attempts=%s retry_base_delay_ms=%s",
            region,
            languages,
            timeout_seconds,
            max_attempts,
            retry_base_delay_ms,
        )

    def transcribe(self, audio_path: Path, include_segments: bool) -> SpeechTranscription:
        """Run transcription with retries for transient upstream failures."""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._transcribe_once(audio_path=audio_path, include_segments=include_segments, attempt=attempt)
            except (RateLimitError, UpstreamUnavailableError, UpstreamTimeoutError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    logger.error(
                        "speech.transcribe exhausted retries attempt=%s max_attempts=%s error=%s",
                        attempt,
                        self.max_attempts,
                        exc,
                    )
                    raise
                delay_seconds = self._retry_delay_seconds(attempt)
                logger.warning(
                    "speech.transcribe transient failure attempt=%s/%s delay_seconds=%.3f error=%s",
                    attempt,
                    self.max_attempts,
                    delay_seconds,
                    exc,
                )
                time.sleep(delay_seconds)
        if last_error is not None:
            raise last_error
        raise ProcessingError("stt_failed", "Speech transcription failed with unknown error")

    def _transcribe_once(self, audio_path: Path, include_segments: bool, attempt: int) -> SpeechTranscription:
        """Execute a single Azure Speech transcription attempt."""
        logger.info(
            "speech._transcribe_once started attempt=%s audio_path=%s include_segments=%s",
            attempt,
            audio_path,
            include_segments,
        )
        speechsdk = _import_speech_sdk()
        speech_config = speechsdk.SpeechConfig(subscription=self.key, region=self.region)
        speech_config.output_format = speechsdk.OutputFormat.Detailed
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
            "Continuous",
        )

        auto_detect = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
            languages=self.languages
        )
        logger.info(
            "speech.transcribe azure_request region=%s languages=%s output_format=%s",
            self.region,
            self.languages,
            "Detailed",
        )
        audio_config = speechsdk.audio.AudioConfig(filename=str(audio_path))
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config,
            auto_detect_source_language_config=auto_detect,
        )

        done = threading.Event()
        errors: list[str] = []
        text_parts: list[str] = []
        segments: list[TranscriptionSegment] = []
        languages: set[str] = set()

        def on_recognized(event):
            """Handle recognized utterance events from Speech SDK."""
            result = event.result
            if result.reason != speechsdk.ResultReason.RecognizedSpeech:
                logger.debug("speech.on_recognized skipped reason=%s", result.reason)
                return
            text = (result.text or "").strip()
            if not text:
                logger.debug("speech.on_recognized skipped empty text")
                return
            text_parts.append(text)
            start_ms = int(result.offset / 10_000)
            duration_ms = int(result.duration / 10_000)
            language: Optional[str] = None
            try:
                language = speechsdk.AutoDetectSourceLanguageResult(result).language
            except Exception:  # pragma: no cover - SDK best-effort parsing.
                language = None

            if language:
                languages.add(language)
            if include_segments:
                segments.append(
                    TranscriptionSegment(
                        start_ms=start_ms,
                        end_ms=max(start_ms, start_ms + duration_ms),
                        text=text,
                        language=language,
                    )
                )
            if self.verbose_ai_logs:
                logger.debug(
                    "speech.on_recognized text=%s start_ms=%s duration_ms=%s language=%s",
                    text,
                    start_ms,
                    duration_ms,
                    language,
                )
            else:
                logger.debug(
                    "speech.on_recognized start_ms=%s duration_ms=%s language=%s text_chars=%s",
                    start_ms,
                    duration_ms,
                    language,
                    len(text),
                )

        def on_canceled(event):
            """Capture cancellation reason and signal recognition completion."""
            reason = str(event.reason)
            if event.reason == speechsdk.CancellationReason.Error and event.error_details:
                reason = event.error_details
            errors.append(reason)
            logger.error("speech.on_canceled reason=%s", reason)
            done.set()

        def on_stopped(_event):
            """Handle end-of-session event and unblock waiting thread."""
            logger.info("speech.on_stopped event received")
            done.set()

        recognizer.recognized.connect(on_recognized)
        recognizer.canceled.connect(on_canceled)
        recognizer.session_stopped.connect(on_stopped)

        recognizer.start_continuous_recognition()
        logger.info("speech.transcribe recognition started attempt=%s", attempt)
        completed = done.wait(timeout=self.timeout_seconds)
        recognizer.stop_continuous_recognition()
        logger.info("speech.transcribe recognition stopped attempt=%s completed=%s", attempt, completed)

        if not completed:
            raise UpstreamTimeoutError("Azure Speech transcription timed out")
        if errors:
            raise self._map_speech_error(errors[0])

        transcript = " ".join(text_parts).strip()
        if not transcript:
            raise ProcessingError("empty_transcript", "Speech service returned empty transcript")

        if self.verbose_ai_logs:
            logger.debug("speech.transcribe transcript_text=%s", transcript)
        logger.info(
            "speech.transcribe completed transcript_chars=%s segments=%s detected_languages=%s",
            len(transcript),
            len(segments),
            sorted(languages),
        )
        return SpeechTranscription(
            text=transcript,
            segments=segments,
            detected_languages=sorted(languages),
        )

    def _retry_delay_seconds(self, attempt: int) -> float:
        """Compute capped exponential backoff delay with jitter."""
        base_delay_seconds = self.retry_base_delay_ms / 1000.0
        jitter = random.uniform(0, base_delay_seconds)
        return min(10.0, base_delay_seconds * (2 ** (attempt - 1)) + jitter)

    @staticmethod
    def _map_speech_error(reason: str):
        """Map Speech SDK cancellation text to API-level error classes."""
        normalized = reason.lower()
        if "429" in normalized or "throttl" in normalized or "too many requests" in normalized:
            return RateLimitError("Azure Speech rate limited")
        if "timeout" in normalized or "timed out" in normalized:
            return UpstreamTimeoutError("Azure Speech timeout")
        transient_markers = (
            "service unavailable",
            "temporarily unavailable",
            "connection",
            "network",
            "unavailable",
            "502",
            "503",
            "504",
        )
        if any(marker in normalized for marker in transient_markers):
            return UpstreamUnavailableError(f"Azure Speech unavailable: {reason}")
        return ProcessingError("stt_failed", reason)
