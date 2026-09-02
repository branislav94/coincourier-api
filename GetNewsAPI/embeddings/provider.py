"""Provider-neutral embedding contract and Phase 6B1 adapters."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Protocol, Sequence

from .models import EmbeddingBatch


class EmbeddingProviderError(RuntimeError):
    """Base class for provider failures safe for job classification."""


class EmbeddingProviderUnavailable(EmbeddingProviderError):
    pass


class InvalidEmbeddingResponse(EmbeddingProviderError):
    pass


class EmbeddingConfigurationError(EmbeddingProviderError):
    pass


class EmbeddingProvider(Protocol):
    provider_name: str
    model: str

    def embed(self, texts: Sequence[str], dimensions: int) -> EmbeddingBatch:
        ...


def _classify_openai_request_error(error: Exception) -> str:
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            OpenAIError,
        )
    except ImportError:
        return "unexpected"

    if isinstance(error, (APIConnectionError, APITimeoutError)):
        return "transient"
    if isinstance(error, APIStatusError):
        status_code = getattr(error, "status_code", None)
        if status_code in {408, 409, 429} or (
            isinstance(status_code, int) and status_code >= 500
        ):
            return "transient"
        return "terminal"
    if isinstance(error, OpenAIError):
        return "terminal"
    return "unexpected"


def validate_embedding_batch(
    batch: EmbeddingBatch,
    *,
    expected_provider: str,
    expected_model: str,
    expected_dimensions: int,
    expected_count: int,
) -> tuple[tuple[float, ...], ...]:
    if batch.provider != expected_provider:
        raise InvalidEmbeddingResponse("embedding response provider mismatch")
    if batch.model != expected_model:
        raise InvalidEmbeddingResponse("embedding response model mismatch")
    if batch.dimensions != expected_dimensions:
        raise InvalidEmbeddingResponse("embedding response dimension metadata mismatch")
    try:
        vector_count = len(batch.vectors) if batch.vectors is not None else -1
    except TypeError as exc:
        raise InvalidEmbeddingResponse("embedding response vectors are invalid") from exc
    if vector_count != expected_count:
        raise InvalidEmbeddingResponse("embedding response count mismatch")

    validated: list[tuple[float, ...]] = []
    for vector in batch.vectors:
        try:
            vector_dimensions = len(vector) if vector is not None else -1
        except TypeError as exc:
            raise InvalidEmbeddingResponse("embedding vector is invalid") from exc
        if vector_dimensions != expected_dimensions:
            raise InvalidEmbeddingResponse("embedding vector dimension mismatch")
        normalized: list[float] = []
        for value in vector:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise InvalidEmbeddingResponse("embedding vector contains invalid values") from exc
            if not math.isfinite(number):
                raise InvalidEmbeddingResponse("embedding vector contains non-finite values")
            normalized.append(number)
        validated.append(tuple(normalized))
    return tuple(validated)


class FakeEmbeddingProvider:
    """Deterministic finite vectors for local tests and failure simulation."""

    def __init__(
        self,
        *,
        provider_name: str = "openai",
        model: str = "text-embedding-3-small",
        fail_with: Exception | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.fail_with = fail_with
        self.call_count = 0
        self.input_counts: list[int] = []

    def embed(self, texts: Sequence[str], dimensions: int) -> EmbeddingBatch:
        self.call_count += 1
        self.input_counts.append(len(texts))
        if self.fail_with is not None:
            raise self.fail_with
        vectors = tuple(
            self._vector(text, dimensions)
            for text in texts
        )
        return EmbeddingBatch(
            provider=self.provider_name,
            model=self.model,
            dimensions=dimensions,
            vectors=vectors,
            usage_tokens=None,
        )

    def _vector(self, text: str, dimensions: int) -> tuple[float, ...]:
        seed = f"{self.provider_name}:{self.model}:{dimensions}:{text}".encode("utf-8")
        digest = hashlib.shake_256(seed).digest(dimensions * 2)
        values = [
            (int.from_bytes(digest[index : index + 2], "big") / 32767.5) - 1.0
            for index in range(0, len(digest), 2)
        ]
        magnitude = math.sqrt(sum(value * value for value in values)) or 1.0
        return tuple(value / magnitude for value in values)


class OpenAIEmbeddingProvider:
    provider_name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        if client is None:
            if not api_key:
                raise EmbeddingConfigurationError("OpenAI embedding API key is missing")
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        self._client = client

    @classmethod
    def from_config(cls, *, client: Any | None = None) -> "OpenAIEmbeddingProvider":
        from config import EMBEDDING_MODEL, OPENAI_API_KEY

        return cls(
            model=EMBEDDING_MODEL,
            api_key=OPENAI_API_KEY,
            client=client,
        )

    def embed(self, texts: Sequence[str], dimensions: int) -> EmbeddingBatch:
        inputs = list(texts)
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=inputs,
                dimensions=dimensions,
            )
        except Exception as exc:
            classification = _classify_openai_request_error(exc)
            if classification == "transient":
                raise EmbeddingProviderUnavailable(
                    "OpenAI embedding request failed"
                ) from exc
            if classification == "terminal":
                raise EmbeddingConfigurationError(
                    "OpenAI embedding request was rejected"
                ) from exc
            raise

        data = getattr(response, "data", None)
        if data is None or len(data) != len(inputs):
            raise InvalidEmbeddingResponse("OpenAI embedding response count mismatch")
        indexed: list[tuple[int, tuple[float, ...]]] = []
        for fallback_index, item in enumerate(data):
            index = getattr(item, "index", fallback_index)
            embedding = getattr(item, "embedding", None)
            if embedding is None:
                raise InvalidEmbeddingResponse("OpenAI embedding vector is missing")
            try:
                indexed.append((int(index), tuple(embedding)))
            except (TypeError, ValueError) as exc:
                raise InvalidEmbeddingResponse(
                    "OpenAI embedding response item is invalid"
                ) from exc
        indexed.sort(key=lambda item: item[0])
        if [index for index, _vector in indexed] != list(range(len(inputs))):
            raise InvalidEmbeddingResponse("OpenAI embedding response indexes are invalid")

        usage = getattr(response, "usage", None)
        usage_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        try:
            normalized_usage = int(usage_tokens) if usage_tokens is not None else None
        except (TypeError, ValueError) as exc:
            raise InvalidEmbeddingResponse("OpenAI embedding usage is invalid") from exc
        return EmbeddingBatch(
            provider=self.provider_name,
            model=getattr(response, "model", None) or self.model,
            dimensions=dimensions,
            vectors=tuple(vector for _index, vector in indexed),
            usage_tokens=normalized_usage,
        )
