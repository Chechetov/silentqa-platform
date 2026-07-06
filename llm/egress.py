"""Единая точка выхода на облачный LLM (ревью D-1/C-4-задел).

Все structured-вызовы проекта идут сюда: модель/потолок/кэш-ключ/ретраи
живут в одном месте, и это же — будущий шов псевдонимизации (152-ФЗ)
или локальной модели: замена провайдера = правка одного модуля.
"""
import json
import logging
import os
import time

import openai as _openai
from openai import OpenAI

logger = logging.getLogger(__name__)

# Транзиентные классы SDK: ретраим сами (поверх встроенных ретраев SDK),
# при исчерпании отдаём исключение наверх — там его ловит task-retry Celery.
_TRANSIENT = (_openai.APITimeoutError, _openai.APIConnectionError,
              _openai.RateLimitError, _openai.InternalServerError)


class LLMEgressError(Exception):
    """База детерминированных ошибок LLM-выхода (ретрай не поможет)."""


class LLMTruncated(LLMEgressError):
    """Выход усечён потолком max_output_tokens."""


class LLMBadOutput(LLMEgressError):
    """Модель вернула невалидный JSON при strict-схеме."""


def structured_completion(*, system, user, schema, schema_name,
                          max_output_tokens, cache_key,
                          model=None, retries=2, api_key=None,
                          backoff_base=2.0) -> dict:
    client = OpenAI(api_key=api_key) if api_key else OpenAI()
    attempt = 0
    while True:
        try:
            resp = client.responses.create(
                model=model or os.getenv("SQA_LLM_MODEL_DEFAULT", "gpt-5.4"),
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_output_tokens=max_output_tokens,
                prompt_cache_key=cache_key,
                text={"format": {"type": "json_schema", "name": schema_name,
                                 "strict": True, "schema": schema}},
            )
            break
        except _TRANSIENT as e:
            attempt += 1
            if attempt > retries:
                raise
            delay = backoff_base * (4 ** (attempt - 1))  # 2с, 8с
            logger.warning("LLM transient %s (%s, попытка %d/%d), повтор через %.0fс",
                           type(e).__name__, schema_name, attempt, retries, delay)
            time.sleep(delay)
    if getattr(resp, "status", None) == "incomplete":
        raise LLMTruncated(f"{schema_name}: выход усечён на {max_output_tokens} токенов")
    try:
        return json.loads(resp.output_text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMBadOutput(f"{schema_name}: невалидный JSON от модели: {e}") from e
