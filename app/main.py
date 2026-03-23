from __future__ import annotations

"""FastAPI entrypoint and orchestration for call processing pipeline."""

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.audio import inspect_wav, normalize_audio_for_stt, validate_upload_declared_type
from app.classifier import ClassificationService
from app.config import Settings, get_settings
from app.errors import APIError, FileTooLargeError, InvalidInputError, ProcessingError, UnauthorizedError
from app.logging_setup import configure_logging
from app.models import (
    ErrorResponse,
    ProcessCallResponse,
    SttMetadata,
    TimingsMs,
    TranscriptionResult,
)
from app.speech import SpeechService
from app.taxonomy import Taxonomy, load_taxonomy

logger = logging.getLogger("calls_category_api")
security = HTTPBearer(auto_error=False)

app = FastAPI(
    title="Call Categorization API",
    version="0.1.0",
)


def _verbose_ai_logs_enabled(settings: Settings | None = None) -> bool:
    """Return whether verbose AI payload logging is enabled."""
    resolved_settings = settings or get_settings()
    return resolved_settings.verbose_ai_logs


def _ffmpeg_is_available(ffmpeg_binary: str) -> bool:
    """Check whether configured ffmpeg binary is available."""
    if Path(ffmpeg_binary).is_file():
        return True
    return shutil.which(ffmpeg_binary) is not None


def _validate_startup_requirements(settings: Settings) -> None:
    """Fail fast if critical runtime dependencies are missing or invalid."""
    logger.info("main._validate_startup_requirements started")
    if not settings.api_bearer_token.strip():
        raise RuntimeError("API_BEARER_TOKEN must be set and non-empty")
    if not _ffmpeg_is_available(settings.ffmpeg_binary):
        raise RuntimeError(f"ffmpeg binary not found: {settings.ffmpeg_binary}")
    taxonomy = load_taxonomy(settings.taxonomy_file)
    logger.info(
        "main._validate_startup_requirements completed taxonomy_version=%s taxonomy_categories=%s stt_languages=%s max_concurrent_calls=%s",
        taxonomy.version,
        len(taxonomy.categories),
        settings.stt_languages,
        settings.max_concurrent_calls,
    )


@lru_cache
def _processing_semaphore(max_concurrent_calls: int) -> asyncio.Semaphore:
    """Return cached semaphore used to cap parallel processing."""
    return asyncio.Semaphore(max_concurrent_calls)


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize logging and validate runtime prerequisites."""
    settings = get_settings()
    configure_logging(settings)
    logger.info("main.startup settings=%s", settings.redacted_dict())
    _validate_startup_requirements(settings)
    logger.info("main.startup_event completed")


@app.middleware("http")
async def enforce_content_length(request: Request, call_next):
    """Reject obviously oversized requests before body is fully processed."""
    logger.debug(
        "main.enforce_content_length started method=%s path=%s content_length=%s",
        request.method,
        request.url.path,
        request.headers.get("content-length"),
    )
    if request.url.path == "/v1/calls/process":
        settings = get_settings()
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_upload_bytes + (2 * 1024 * 1024):
                logger.warning(
                    "main.enforce_content_length rejected request content_length=%s max_upload_bytes=%s",
                    content_length,
                    settings.max_upload_bytes,
                )
                payload = ErrorResponse(
                    error_code="file_too_large",
                    message=f"Request exceeds {settings.max_upload_mb} MB limit",
                    call_id=None,
                )
                return JSONResponse(status_code=413, content=payload.model_dump())
    response = await call_next(request)
    logger.debug(
        "main.enforce_content_length completed method=%s path=%s status_code=%s",
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


@lru_cache
def _cached_speech_service() -> SpeechService:
    """Create and cache SpeechService instance."""
    settings = get_settings()
    logger.info("main._cached_speech_service creating new SpeechService")
    return SpeechService(
        key=settings.azure_speech_key,
        region=settings.azure_speech_region,
        languages=settings.stt_languages,
        timeout_seconds=settings.speech_timeout_seconds,
        max_attempts=settings.speech_max_attempts,
        retry_base_delay_ms=settings.speech_retry_base_delay_ms,
        verbose_ai_logs=settings.verbose_ai_logs,
    )


def get_speech_service() -> SpeechService:
    """FastAPI dependency provider for SpeechService."""
    logger.debug("main.get_speech_service called")
    return _cached_speech_service()


@lru_cache
def _cached_taxonomy() -> Taxonomy:
    """Load and cache taxonomy for classifier usage."""
    settings = get_settings()
    logger.info("main._cached_taxonomy loading taxonomy from %s", settings.taxonomy_file)
    return load_taxonomy(settings.taxonomy_file)


@lru_cache
def _cached_classifier_service() -> ClassificationService:
    """Create and cache ClassificationService instance."""
    settings = get_settings()
    taxonomy = _cached_taxonomy()
    logger.info("main._cached_classifier_service creating new ClassificationService")
    return ClassificationService(
        endpoint=str(settings.azure_openai_endpoint),
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        deployment=settings.azure_openai_deployment,
        prompt_version=settings.prompt_version,
        taxonomy=taxonomy,
        timeout_seconds=settings.openai_timeout_seconds,
        max_attempts=settings.openai_max_attempts,
        retry_base_delay_ms=settings.openai_retry_base_delay_ms,
        verbose_ai_logs=settings.verbose_ai_logs,
    )


def get_classifier_service() -> ClassificationService:
    """FastAPI dependency provider for ClassificationService."""
    logger.debug("main.get_classifier_service called")
    return _cached_classifier_service()


def _parse_metadata(metadata_raw: str | None, call_id: str) -> dict[str, Any]:
    """Parse optional metadata JSON string into a dictionary."""
    metadata_raw_len = len(metadata_raw) if metadata_raw else 0
    logger.info("main._parse_metadata started call_id=%s metadata_raw_len=%s", call_id, metadata_raw_len)
    if metadata_raw is None or metadata_raw.strip() == "":
        logger.info("main._parse_metadata empty metadata call_id=%s", call_id)
        return {}
    try:
        parsed = json.loads(metadata_raw)
    except json.JSONDecodeError as exc:
        logger.exception("main._parse_metadata invalid JSON call_id=%s", call_id)
        raise InvalidInputError("invalid_metadata", "metadata must be a valid JSON object", call_id=call_id) from exc
    if not isinstance(parsed, dict):
        logger.error("main._parse_metadata metadata is not object call_id=%s type=%s", call_id, type(parsed))
        raise InvalidInputError("invalid_metadata", "metadata must be a JSON object", call_id=call_id)
    logger.info("main._parse_metadata completed call_id=%s metadata_keys=%s", call_id, sorted(parsed.keys()))
    if _verbose_ai_logs_enabled():
        logger.debug("main._parse_metadata parsed=%s", parsed)
    return parsed


async def _save_upload_to_file(upload: UploadFile, destination: Path, max_size_bytes: int, call_id: str) -> None:
    """Stream multipart upload content to disk with strict size checks."""
    logger.info(
        "main._save_upload_to_file started call_id=%s filename=%s destination=%s max_size_bytes=%s",
        call_id,
        upload.filename,
        destination,
        max_size_bytes,
    )
    bytes_written = 0
    chunk_count = 0
    with destination.open("wb") as output:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            chunk_count += 1
            bytes_written += len(chunk)
            if bytes_written > max_size_bytes:
                logger.warning(
                    "main._save_upload_to_file size exceeded call_id=%s bytes_written=%s max_size_bytes=%s",
                    call_id,
                    bytes_written,
                    max_size_bytes,
                )
                raise FileTooLargeError(max_upload_mb=max_size_bytes // (1024 * 1024), call_id=call_id)
            output.write(chunk)
    if bytes_written == 0:
        raise InvalidInputError("empty_file", "Uploaded file is empty", call_id=call_id)
    logger.info(
        "main._save_upload_to_file completed call_id=%s bytes_written=%s chunk_count=%s",
        call_id,
        bytes_written,
        chunk_count,
    )


def _pick_call_id(call_id: str | None, filename: str | None) -> str:
    """Resolve call id from explicit value, filename stem, or random UUID."""
    logger.debug("main._pick_call_id started call_id=%s filename=%s", call_id, filename)
    if call_id and call_id.strip():
        resolved = call_id.strip()
        logger.info("main._pick_call_id resolved from call_id=%s", resolved)
        return resolved
    if filename:
        stem = Path(filename).stem.strip()
        if stem:
            logger.info("main._pick_call_id resolved from filename stem=%s", stem)
            return stem
    generated = f"call-{uuid.uuid4()}"
    logger.info("main._pick_call_id generated call_id=%s", generated)
    return generated


def _auth_guard(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> None:
    """Validate Bearer token for protected endpoints."""
    logger.debug("main._auth_guard started")
    if credentials is None or credentials.scheme.lower() != "bearer":
        logger.warning("main._auth_guard missing bearer token")
        raise UnauthorizedError("Missing bearer token")
    if credentials.credentials != settings.api_bearer_token:
        logger.warning("main._auth_guard invalid bearer token")
        raise UnauthorizedError("Invalid bearer token")
    logger.info("main._auth_guard authorized")


@app.exception_handler(APIError)
async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    """Convert known APIError exceptions into JSON responses."""
    logger.error(
        "main.api_error_handler status_code=%s error_code=%s call_id=%s message=%s",
        exc.status_code,
        exc.error_code,
        exc.call_id,
        exc.message,
    )
    payload = ErrorResponse(error_code=exc.error_code, message=exc.message, call_id=exc.call_id)
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert unexpected exceptions into generic safe JSON error payload."""
    call_id = getattr(request.state, "call_id", None)
    logger.exception("Unhandled processing error for call_id=%s: %s", call_id, exc)
    payload = ErrorResponse(
        error_code="processing_error",
        message="Unexpected processing error",
        call_id=call_id,
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    """Liveness probe endpoint."""
    logger.info("main.healthcheck called")
    return {"status": "ok"}


@app.post("/v1/calls/process", response_model=ProcessCallResponse)
async def process_call(
    request: Request,
    _auth: None = Depends(_auth_guard),
    file: UploadFile = File(...),
    call_id: str | None = Form(default=None),
    metadata: str | None = Form(default=None),
    return_transcript_segments: bool = Form(default=False),
    include_extras: bool = Form(default=True),
    settings: Settings = Depends(get_settings),
    speech_service: SpeechService = Depends(get_speech_service),
    classifier_service: ClassificationService = Depends(get_classifier_service),
) -> ProcessCallResponse:
    """Process one uploaded WAV file and return transcription + classification.

    Pipeline:
    1. Validate metadata and upload constraints.
    2. Save uploaded file to temporary workspace.
    3. Inspect and normalize audio for STT.
    4. Transcribe with Azure Speech.
    5. Classify transcript with Azure OpenAI.
    6. Return structured JSON payload with timings.
    """
    logger.info(
        "main.process_call started filename=%s content_type=%s call_id=%s return_transcript_segments=%s include_extras=%s",
        file.filename,
        file.content_type,
        call_id,
        return_transcript_segments,
        include_extras,
    )
    resolved_call_id = _pick_call_id(call_id=call_id, filename=file.filename)
    request.state.call_id = resolved_call_id

    total_started = perf_counter()
    normalize_ms = 0
    stt_ms = 0
    clf_ms = 0
    semaphore = _processing_semaphore(settings.max_concurrent_calls)

    try:
        logger.info(
            "main.process_call waiting for concurrency slot call_id=%s max_concurrent_calls=%s",
            resolved_call_id,
            settings.max_concurrent_calls,
        )
        async with semaphore:
            logger.info("main.process_call acquired concurrency slot call_id=%s", resolved_call_id)
            logger.info("main.process_call step=validate_upload call_id=%s", resolved_call_id)
            validate_upload_declared_type(file.filename, file.content_type)
            logger.info("main.process_call step=parse_metadata call_id=%s", resolved_call_id)
            metadata_obj = _parse_metadata(metadata_raw=metadata, call_id=resolved_call_id)

            with tempfile.TemporaryDirectory(prefix="callproc_") as tmp_dir:
                logger.info("main.process_call tmp_dir=%s call_id=%s", tmp_dir, resolved_call_id)
                input_path = Path(tmp_dir) / "input.wav"
                normalized_path = Path(tmp_dir) / "normalized.wav"
                logger.info("main.process_call step=save_upload call_id=%s", resolved_call_id)
                await _save_upload_to_file(
                    upload=file,
                    destination=input_path,
                    max_size_bytes=settings.max_upload_bytes,
                    call_id=resolved_call_id,
                )
                logger.info("main.process_call step=inspect_wav call_id=%s", resolved_call_id)
                wav_info = await run_in_threadpool(
                    inspect_wav,
                    path=input_path,
                    max_duration_seconds=settings.max_duration_seconds,
                )
                logger.info("main.process_call wav_info call_id=%s wav_info=%s", resolved_call_id, wav_info)

                normalize_started = perf_counter()
                logger.info("main.process_call step=normalize_audio call_id=%s", resolved_call_id)
                await run_in_threadpool(
                    normalize_audio_for_stt,
                    input_path=input_path,
                    output_path=normalized_path,
                    ffmpeg_binary=settings.ffmpeg_binary,
                )
                normalize_ms = int((perf_counter() - normalize_started) * 1000)
                logger.info(
                    "main.process_call step=normalize_audio completed call_id=%s normalize_ms=%s",
                    resolved_call_id,
                    normalize_ms,
                )

                stt_started = perf_counter()
                logger.info("main.process_call step=stt call_id=%s", resolved_call_id)
                transcription = await run_in_threadpool(
                    speech_service.transcribe,
                    audio_path=normalized_path,
                    include_segments=return_transcript_segments,
                )
                stt_ms = int((perf_counter() - stt_started) * 1000)
                logger.info(
                    "main.process_call step=stt completed call_id=%s stt_ms=%s transcript_chars=%s detected_languages=%s",
                    resolved_call_id,
                    stt_ms,
                    len(transcription.text),
                    transcription.detected_languages,
                )
                if _verbose_ai_logs_enabled(settings):
                    logger.debug("main.process_call stt_transcript call_id=%s text=%s", resolved_call_id, transcription.text)

                clf_started = perf_counter()
                logger.info(
                    "main.process_call step=classification call_id=%s metadata_keys=%s",
                    resolved_call_id,
                    sorted(metadata_obj.keys()),
                )
                if _verbose_ai_logs_enabled(settings):
                    logger.debug("main.process_call metadata call_id=%s metadata=%s", resolved_call_id, metadata_obj)
                classification = await run_in_threadpool(
                    classifier_service.classify,
                    transcript=transcription.text,
                    metadata=metadata_obj,
                    include_extras=include_extras,
                )
                clf_ms = int((perf_counter() - clf_started) * 1000)
                logger.info(
                    "main.process_call step=classification completed call_id=%s clf_ms=%s caller_type=%s call_category=%s",
                    resolved_call_id,
                    clf_ms,
                    classification.caller_type,
                    classification.call_category,
                )
                if _verbose_ai_logs_enabled(settings):
                    logger.debug(
                        "main.process_call classification_payload call_id=%s payload=%s",
                        resolved_call_id,
                        classification.model_dump(),
                    )

                if settings.log_transcripts:
                    logger.info("Processed call_id=%s transcript_len=%s", resolved_call_id, len(transcription.text))
                else:
                    logger.info("Processed call_id=%s", resolved_call_id)

                timings = TimingsMs(
                    normalize=normalize_ms,
                    stt=stt_ms,
                    clf=clf_ms,
                    total=int((perf_counter() - total_started) * 1000),
                )
                transcription_payload = TranscriptionResult(
                    text=transcription.text,
                    segments=transcription.segments if return_transcript_segments else None,
                    detected_languages=transcription.detected_languages or None,
                    stt_metadata=SttMetadata(
                        source_sample_rate_hz=wav_info.sample_rate_hz,
                        source_channels=wav_info.channels,
                        source_sample_width_bytes=wav_info.sample_width_bytes,
                        duration_sec=wav_info.duration_sec,
                    ),
                )
                return ProcessCallResponse(
                    call_id=resolved_call_id,
                    transcription=transcription_payload,
                    classification=classification,
                    timings_ms=timings,
                )
    except APIError as exc:
        logger.exception("main.process_call APIError call_id=%s error=%s", resolved_call_id, exc)
        if exc.call_id is None:
            exc.call_id = resolved_call_id
        raise
    except Exception as exc:
        logger.exception("main.process_call unexpected exception call_id=%s", resolved_call_id)
        raise ProcessingError(
            error_code="processing_error",
            message=f"Internal processing failed: {exc}",
            call_id=resolved_call_id,
        ) from exc
    finally:
        logger.info("main.process_call closing upload file call_id=%s", resolved_call_id)
        await file.close()
