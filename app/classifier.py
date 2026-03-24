from __future__ import annotations

"""Azure OpenAI based call classification service."""

import json
import logging
import random
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from app.errors import ProcessingError, RateLimitError, UpstreamTimeoutError, UpstreamUnavailableError
from app.models import ClassificationExtras, ClassificationResult
from app.taxonomy import Taxonomy

logger = logging.getLogger("calls_category_api.classifier")


class _RawExtras(BaseModel):
    """Raw extras shape expected from the model JSON output."""

    intent: Optional[str] = None
    sentiment: Optional[str] = None
    compliance_flags: list[str] = Field(default_factory=list)
    escalation: Optional[bool] = None
    summary: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)


class _RawClassification(BaseModel):
    """Raw classification payload prior to conversion into API model."""

    caller_type: Literal["NATURAL", "JURIDICAL", "UNKNOWN"]
    caller_type_confidence: float = Field(ge=0, le=1)
    call_category: str
    call_category_confidence: float = Field(ge=0, le=1)
    extras: Optional[_RawExtras] = None


def _import_openai_client():
    """Import Azure OpenAI client lazily and raise API error if missing."""
    logger.info("classifier._import_openai_client started")
    try:
        from openai import AzureOpenAI
    except ImportError as exc:  # pragma: no cover
        logger.exception("classifier._import_openai_client failed")
        raise ProcessingError("openai_sdk_missing", "openai package is not installed") from exc
    logger.info("classifier._import_openai_client completed")
    return AzureOpenAI


class ClassificationService:
    """Classify transcripts into caller type and taxonomy category."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        deployment: str,
        prompt_version: str,
        taxonomy: Taxonomy,
        timeout_seconds: int,
        max_attempts: int,
        retry_base_delay_ms: int,
        verbose_ai_logs: bool,
    ) -> None:
        """Initialize classifier configuration and retry policy."""
        self.endpoint = endpoint
        self.api_key = api_key
        self.api_version = api_version
        self.deployment = deployment
        self.prompt_version = prompt_version
        self.taxonomy = taxonomy
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_base_delay_ms = retry_base_delay_ms
        self.verbose_ai_logs = verbose_ai_logs
        logger.info(
            "classifier.ClassificationService initialized endpoint=%s deployment=%s api_version=%s prompt_version=%s timeout_seconds=%s max_attempts=%s retry_base_delay_ms=%s",
            endpoint,
            deployment,
            api_version,
            prompt_version,
            timeout_seconds,
            max_attempts,
            retry_base_delay_ms,
        )

    def classify(
        self,
        transcript: str,
        metadata: Optional[dict[str, Any]],
        include_extras: bool,
    ) -> ClassificationResult:
        """Run one full classification cycle and validate strict JSON output."""
        logger.info(
            "classifier.classify started transcript_chars=%s include_extras=%s metadata_keys=%s",
            len(transcript),
            include_extras,
            sorted((metadata or {}).keys()),
        )
        if self.verbose_ai_logs:
            logger.debug("classifier.classify transcript=%s", transcript)
        client = self._create_client()
        system_prompt, user_prompt = self._build_prompts(transcript=transcript, metadata=metadata)
        if self.verbose_ai_logs:
            logger.debug("classifier.classify system_prompt=%s", system_prompt)
            logger.debug("classifier.classify user_prompt=%s", user_prompt)

        logger.info(
            "classifier.classify azure_request endpoint=%s deployment=%s api_version=%s temperature=%s response_format=%s",
            self.endpoint,
            self.deployment,
            self.api_version,
            0,
            "json_object",
        )
        response = self._chat_completion_with_retry(
            client=client,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            operation_name="classification",
        )
        raw_content = self._extract_content(response)
        if self.verbose_ai_logs:
            logger.debug("classifier.classify azure_raw_response=%s", raw_content)

        try:
            parsed = self._validate_payload(raw_content, include_extras=include_extras)
        except (json.JSONDecodeError, ValidationError, ValueError):
            logger.warning("classifier.classify initial validation failed, trying repair")
            repair_content = self._repair_response(client, raw_content)
            if self.verbose_ai_logs:
                logger.debug("classifier.classify repaired_raw_response=%s", repair_content)
            try:
                parsed = self._validate_payload(repair_content, include_extras=include_extras)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise ProcessingError(
                    "classification_validation_failed",
                    f"Classifier returned invalid JSON after repair: {exc}",
                ) from exc

        logger.info(
            "classifier.classify completed caller_type=%s call_category=%s caller_type_confidence=%s call_category_confidence=%s",
            parsed.caller_type,
            parsed.call_category,
            parsed.caller_type_confidence,
            parsed.call_category_confidence,
        )
        return ClassificationResult(
            caller_type=parsed.caller_type,
            caller_type_confidence=parsed.caller_type_confidence,
            call_category=parsed.call_category,
            call_category_confidence=parsed.call_category_confidence,
            extras=ClassificationExtras.model_validate(parsed.extras.model_dump()) if parsed.extras else None,
            model=getattr(response, "model", self.deployment),
            prompt_version=self.prompt_version,
        )

    def _create_client(self):
        """Create Azure OpenAI SDK client instance."""
        AzureOpenAI = _import_openai_client()
        logger.info(
            "classifier._create_client endpoint=%s api_version=%s deployment=%s",
            self.endpoint,
            self.api_version,
            self.deployment,
        )
        client = AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.endpoint,
        )
        logger.info("classifier._create_client completed")
        return client

    def _build_prompts(self, transcript: str, metadata: Optional[dict[str, Any]]) -> tuple[str, str]:
        """Build system and user prompts with taxonomy constraints."""
        logger.info("classifier._build_prompts started")
        natural_block = self.taxonomy.prompt_block_for_caller_type("NATURAL")
        juridical_block = self.taxonomy.prompt_block_for_caller_type("JURIDICAL")
        natural_keys = sorted(self.taxonomy.keys_for_caller_type("NATURAL"))
        juridical_keys = sorted(self.taxonomy.keys_for_caller_type("JURIDICAL"))
        extras_instruction = (
            "Include extras object with: intent, sentiment, compliance_flags, escalation, summary, evidence, key_entities."
        )

        system_prompt = (
            "You classify phone calls.\n"
            "Return JSON only. No markdown.\n"
            "Rules:\n"
            "- caller_type must be NATURAL, JURIDICAL, or UNKNOWN.\n"
            "- If caller_type is NATURAL, call_category must be one of NATURAL categories.\n"
            "- If caller_type is JURIDICAL, call_category must be one of JURIDICAL categories.\n"
            "- If caller_type is UNKNOWN, use the best match from full taxonomy and keep confidence low when uncertain.\n"
            "- caller_type_confidence and call_category_confidence must be numbers in [0,1].\n"
            f"- {extras_instruction}\n"
            "- If uncertain, prefer UNKNOWN and lower confidence.\n"
        )
        user_prompt = (
            f"prompt_version: {self.prompt_version}\n"
            f"taxonomy_version: {self.taxonomy.version}\n"
            "Allowed categories by caller_type:\n"
            f"NATURAL keys: {natural_keys}\n"
            f"NATURAL detailed list:\n{natural_block}\n\n"
            f"JURIDICAL keys: {juridical_keys}\n"
            f"JURIDICAL detailed list:\n{juridical_block}\n\n"
            "Output JSON schema:\n"
            "{\n"
            '  "caller_type": "NATURAL|JURIDICAL|UNKNOWN",\n'
            '  "caller_type_confidence": 0.0,\n'
            '  "call_category": "ONE_OF_ALLOWED_KEYS",\n'
            '  "call_category_confidence": 0.0,\n'
            '  "extras": {\n'
            '    "intent": "string|null",\n'
            '    "sentiment": "positive|neutral|negative|mixed|null",\n'
            '    "compliance_flags": ["string"],\n'
            '    "escalation": true,\n'
            '    "summary": "string|null",\n'
            '    "evidence": ["short transcript quote"],\n'
            '    "key_entities": ["name/id/phone/product"]\n'
            "  }\n"
            "}\n\n"
            f"Metadata JSON:\n{json.dumps(metadata or {}, ensure_ascii=False)}\n\n"
            f"Transcript:\n{transcript}"
        )
        logger.info(
            "classifier._build_prompts completed natural_categories=%s juridical_categories=%s",
            len(natural_keys),
            len(juridical_keys),
        )
        return system_prompt, user_prompt

    def _validate_payload(self, raw_content: str, include_extras: bool) -> _RawClassification:
        """Parse and validate model JSON against schema and taxonomy rules."""
        logger.info("classifier._validate_payload started include_extras=%s", include_extras)
        if self.verbose_ai_logs:
            logger.debug("classifier._validate_payload raw_content=%s", raw_content)
        parsed_json = json.loads(raw_content)
        payload = _RawClassification.model_validate(parsed_json)
        allowed_categories = self.taxonomy.keys_for_caller_type(payload.caller_type)
        if payload.call_category not in allowed_categories:
            raise ValueError(
                f"Invalid call_category '{payload.call_category}' for caller_type '{payload.caller_type}'"
            )
        if not include_extras:
            payload.extras = None
        logger.info(
            "classifier._validate_payload completed caller_type=%s call_category=%s",
            payload.caller_type,
            payload.call_category,
        )
        return payload

    def _repair_response(self, client, invalid_response: str) -> str:
        """Attempt one repair call when initial model JSON is invalid."""
        logger.info("classifier._repair_response started")
        if self.verbose_ai_logs:
            logger.debug("classifier._repair_response invalid_response=%s", invalid_response)
        natural_categories = sorted(self.taxonomy.keys_for_caller_type("NATURAL"))
        juridical_categories = sorted(self.taxonomy.keys_for_caller_type("JURIDICAL"))
        all_categories = sorted(self.taxonomy.keys)
        repair_prompt = (
            "Fix the following JSON so it matches the required schema.\n"
            "Output JSON only.\n"
            "caller_type must be NATURAL, JURIDICAL, or UNKNOWN.\n"
            f"If caller_type=NATURAL, call_category must be exactly one of: {natural_categories}\n"
            f"If caller_type=JURIDICAL, call_category must be exactly one of: {juridical_categories}\n"
            f"If caller_type=UNKNOWN, call_category must be one of: {all_categories}\n"
            "Keep confidence fields within [0,1].\n\n"
            f"Broken payload:\n{invalid_response}"
        )
        if self.verbose_ai_logs:
            logger.debug("classifier._repair_response prompt=%s", repair_prompt)
        response = self._chat_completion_with_retry(
            client=client,
            messages=[
                {"role": "system", "content": "You repair and return valid JSON only."},
                {"role": "user", "content": repair_prompt},
            ],
            response_format={"type": "json_object"},
            operation_name="classification_repair",
        )
        repaired = self._extract_content(response)
        logger.info("classifier._repair_response completed")
        if self.verbose_ai_logs:
            logger.debug("classifier._repair_response output=%s", repaired)
        return repaired

    def _chat_completion_with_retry(self, client, messages: list[dict[str, str]], response_format: dict[str, str], operation_name: str):
        """Execute chat completion with retry/error mapping for transient failures."""
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                logger.info(
                    "classifier._chat_completion_with_retry operation=%s attempt=%s/%s timeout_seconds=%s",
                    operation_name,
                    attempt,
                    self.max_attempts,
                    self.timeout_seconds,
                )
                return client.chat.completions.create(
                    model=self.deployment,
                    temperature=0,
                    response_format=response_format,
                    messages=messages,
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - provider SDK raises several custom exceptions
                last_exception = exc
                status_code = self._extract_status_code(exc)
                is_timeout = self._is_timeout_error(exc)
                is_rate_limited = status_code == 429 or "rate limit" in str(exc).lower() or "throttl" in str(exc).lower()
                is_retryable_server = (
                    status_code is not None and 500 <= status_code <= 599
                ) or self._is_connection_error(exc)

                if is_rate_limited:
                    if attempt >= self.max_attempts:
                        logger.error(
                            "classifier._chat_completion_with_retry rate-limited operation=%s after %s attempts",
                            operation_name,
                            attempt,
                        )
                        raise RateLimitError("Azure OpenAI rate limited") from exc
                    self._sleep_before_retry(operation_name, attempt, exc)
                    continue

                if is_timeout:
                    if attempt >= self.max_attempts:
                        logger.error(
                            "classifier._chat_completion_with_retry timeout operation=%s after %s attempts",
                            operation_name,
                            attempt,
                        )
                        raise UpstreamTimeoutError("Azure OpenAI request timed out") from exc
                    self._sleep_before_retry(operation_name, attempt, exc)
                    continue

                if is_retryable_server:
                    if attempt >= self.max_attempts:
                        logger.error(
                            "classifier._chat_completion_with_retry unavailable operation=%s after %s attempts status_code=%s",
                            operation_name,
                            attempt,
                            status_code,
                        )
                        raise UpstreamUnavailableError("Azure OpenAI temporarily unavailable") from exc
                    self._sleep_before_retry(operation_name, attempt, exc)
                    continue

                logger.exception(
                    "classifier._chat_completion_with_retry non-retryable failure operation=%s status_code=%s",
                    operation_name,
                    status_code,
                )
                raise ProcessingError("classification_failed", f"Azure OpenAI request failed: {exc}") from exc

        if last_exception is not None:
            raise ProcessingError("classification_failed", f"Azure OpenAI request failed: {last_exception}") from last_exception
        raise ProcessingError("classification_failed", "Azure OpenAI request failed with unknown error")

    def _sleep_before_retry(self, operation_name: str, attempt: int, exc: Exception) -> None:
        """Sleep before retrying, using exponential backoff with jitter."""
        delay = self._retry_delay_seconds(attempt)
        logger.warning(
            "classifier retry scheduled operation=%s attempt=%s/%s delay_seconds=%.3f error=%s",
            operation_name,
            attempt,
            self.max_attempts,
            delay,
            exc,
        )
        time.sleep(delay)

    def _retry_delay_seconds(self, attempt: int) -> float:
        """Compute capped exponential backoff delay with jitter."""
        base_delay_seconds = self.retry_base_delay_ms / 1000.0
        jitter = random.uniform(0, base_delay_seconds)
        return min(10.0, base_delay_seconds * (2 ** (attempt - 1)) + jitter)

    @staticmethod
    def _extract_status_code(exc: Exception) -> Optional[int]:
        """Extract HTTP status code from SDK exception if present."""
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
        return None

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        """Heuristically detect timeout-like SDK errors."""
        class_name = exc.__class__.__name__.lower()
        message = str(exc).lower()
        return "timeout" in class_name or "timed out" in message or "timeout" in message

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """Heuristically detect network/connectivity SDK errors."""
        class_name = exc.__class__.__name__.lower()
        message = str(exc).lower()
        markers = ("connection", "network", "temporary failure", "unreachable", "service unavailable")
        return "connection" in class_name or any(marker in message for marker in markers)

    @staticmethod
    def _extract_content(response) -> str:
        """Extract text content from OpenAI response object."""
        if not response.choices:
            raise ProcessingError("classification_failed", "OpenAI response did not include choices")
        message = response.choices[0].message
        content = getattr(message, "content", None)
        if isinstance(content, list):
            merged_parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    merged_parts.append(str(part.get("text", "")))
                    continue
                text_value = getattr(part, "text", None)
                if isinstance(text_value, str):
                    merged_parts.append(text_value)
            merged = "".join(merged_parts)
            content = merged
        if not content or not isinstance(content, str):
            raise ProcessingError("classification_failed", "OpenAI returned empty content")
        return content
