"""Коучинг-инсайт по звонку (F-5): персональный разбор для менеджера.

Отдельный LLM-вызов ПОСЛЕ quality — не раздуваем strict-схему отчёта и держим
независимый домен сбоя (best-effort, только дашборд). Результат ложится в
`coaching.json`; в quality_results и AmoCRM НЕ уходит.
"""
import logging
import os

from llm.egress import structured_completion

logger = logging.getLogger(__name__)

# Потолок выхода (reasoning-токены GPT-5.4 тоже сюда входят) — разбор компактный.
COACH_MAX_OUTPUT_TOKENS = 4_000
# Кап входного транскрипта: длинные звонки режем с начала (старейшие реплики),
# коучинг важнее для концовки разговора (закрытие/договорённости).
COACH_MAX_TRANSCRIPT_CHARS = 24_000

# === Strict-схема контракта coaching.json (единый для T1/T2/T3, менять нельзя) ===
_COACH_SCHEMA = {
    "name": "coaching",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "Главный совет одной фразой",
            },
            "strengths": {
                "type": "array",
                "description": "1-2 сильные стороны разговора",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Что было сильно"},
                        "quote": {
                            "type": ["string", "null"],
                            "description": "Дословная цитата из транскрипта или null",
                        },
                    },
                    "required": ["text", "quote"],
                    "additionalProperties": False,
                },
            },
            "growth_areas": {
                "type": "array",
                "description": "1-3 разбора «момент → почему важно → как лучше»",
                "items": {
                    "type": "object",
                    "properties": {
                        "moment_quote": {
                            "type": "string",
                            "description": "Дословная цитата реплики менеджера",
                        },
                        "why_it_matters": {
                            "type": "string",
                            "description": "Почему это важно",
                        },
                        "better_version": {
                            "type": "string",
                            "description": "Как лучше сформулировать (готовая фраза от первого лица)",
                        },
                        "time": {
                            "type": ["number", "null"],
                            "description": "Секунда start процитированной реплики (число из скобок [t]) или null",
                        },
                    },
                    "required": ["moment_quote", "why_it_matters", "better_version", "time"],
                    "additionalProperties": False,
                },
            },
            "drill": {
                "type": "string",
                "description": "Фокус-упражнение на следующий звонок",
            },
        },
        "required": ["headline", "strengths", "growth_areas", "drill"],
        "additionalProperties": False,
    },
}

_SYSTEM = """Ты — коуч по продажам/переговорам. По транскрипту и оценке дай менеджеру персональный разбор.

Правила:
- Цитаты — ДОСЛОВНО из транскрипта (копируй, не пересказывай).
- moment_quote и better_version бери ТОЛЬКО из реплик менеджера — коучим менеджера, не клиента (если приведена легенда ролей — ориентируйся на неё; иначе определи менеджера по контексту разговора).
- time — число секунд из скобок [t] у процитированной реплики.
- Тон — поддерживающий и конкретный, без общих слов ("будь внимательнее" — запрещено, только конкретика "вместо X скажи Y").
- better_version — 1-2 живые фразы от первого лица, готовые к произнесению.
- headline — самое важное одно.
- drill — одно микро-упражнение на следующий звонок.
- strengths: 1-2 пункта. growth_areas: 1-3 пункта, минимум 1 — даже на сильном звонке найди зону роста.
- Не дублируй generic-рекомендации из отчёта — давай именно разбор моментов этого разговора."""


# Роли из speaker_roles (quality.py) → человекочитаемо для легенды в user-промпте.
_ROLE_RU = {
    "manager": "менеджер",
    "broker": "менеджер",
    "client": "клиент",
    "doctor": "врач",
    "other": "другой",
}


def _speaker_legend(speaker_roles) -> str:
    """Строит легенду ролей спикеров для user-промпта.

    На проде transcript[].speaker = сырые ID (SPEAKER_00…), а карта ролей лежит в
    quality_report["speaker_roles"]. Нормализует оба формата значения:
    `{id: {"role": str, "name": str|null}}` (после _convert_speaker_roles) и легаси
    `{id: "role"}`-строку. Пусто/не dict/нет валидных записей → "" (легенду не печатаем).
    """
    if not isinstance(speaker_roles, dict) or not speaker_roles:
        return ""
    parts: list[str] = []
    for sid, val in speaker_roles.items():
        if isinstance(val, dict):
            role = (val.get("role") or "").strip()
            name = (val.get("name") or "").strip()
        else:  # легаси: голая строка-роль
            role = str(val or "").strip()
            name = ""
        if not sid:
            continue
        role_ru = _ROLE_RU.get(role.lower(), role) if role else "?"
        entry = f"{sid} — {role_ru}"
        if name:
            entry += f" ({name})"
        parts.append(entry)
    if not parts:
        return ""
    return "Роли спикеров: " + "; ".join(parts)


def _format_transcript(transcript: list[dict]) -> str:
    """Форматирует транскрипт строками `[start] speaker: text`.

    Обрезает с начала (старейшие реплики) при превышении капа, помечая усечение.
    Пустой/безречевой транскрипт → пустая строка.
    """
    lines = []
    for seg in transcript or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start") or 0)
        except (TypeError, ValueError):
            start = 0.0
        speaker = seg.get("speaker") or "?"
        lines.append(f"[{start:.1f}] {speaker}: {text}")

    if not lines:
        return ""

    # Держим новейшие реплики (конец разговора), режем старейшие с начала.
    kept: list[str] = []
    total = 0
    truncated = False
    for line in reversed(lines):
        add = len(line) + 1  # +1 за перевод строки
        if kept and total + add > COACH_MAX_TRANSCRIPT_CHARS:
            truncated = True
            break
        kept.append(line)
        total += add
    kept.reverse()

    result = "\n".join(kept)
    if truncated:
        result = "(начало усечено)\n" + result
    return result


def _compact_report(quality_report: dict) -> str:
    """Компакт отчёта: overall_score + слабые критерии (≤6) + нерешённые возражения."""
    parts = [f"Общая оценка: {quality_report.get('overall_score')}/10"]

    weak = []
    for c in quality_report.get("criteria") or []:
        score = c.get("score")
        if isinstance(score, bool):  # bool — подтип int, но это не оценка
            continue
        if isinstance(score, (int, float)) and score <= 6:
            weak.append(f"- {c.get('name', '')} ({score}/10): {c.get('comment', '')}")
    if weak:
        parts.append("Слабые критерии (проседают):\n" + "\n".join(weak))

    unresolved = []
    for o in quality_report.get("objections") or []:
        if not o.get("resolved"):
            unresolved.append(
                f"- Клиент: «{o.get('text', '')}» → брокер: {o.get('broker_response', '')}"
            )
    if unresolved:
        parts.append("Нерешённые возражения:\n" + "\n".join(unresolved))

    return "\n\n".join(parts)


def generate_coaching(transcript_with_speakers: list[dict], quality_report: dict) -> dict | None:
    """Генерирует коучинг-инсайт по звонку.

    Returns dict (контракт coaching.json) либо None — при пустом/безречевом
    транскрипте или отсутствии OPENAI_API_KEY (тогда LLM не зовётся). Любой сбой
    LLM всплывает наверх — вызывающий (pipeline / бэкфилл) держит best-effort try/except.
    """
    transcript_text = _format_transcript(transcript_with_speakers)
    if not transcript_text:
        logger.info("Coaching: пустой транскрипт — пропускаю без вызова LLM")
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set, skipping coaching")
        return None

    sections = []
    legend = _speaker_legend(quality_report.get("speaker_roles"))
    if legend:
        sections.append(legend)
    sections.append("## Транскрипт разговора\n" + transcript_text)
    sections.append("## Оценка звонка\n" + _compact_report(quality_report))
    user = "\n\n".join(sections)

    return structured_completion(
        system=_SYSTEM,
        user=user,
        schema=_COACH_SCHEMA["schema"],
        schema_name=_COACH_SCHEMA["name"],
        max_output_tokens=COACH_MAX_OUTPUT_TOKENS,
        cache_key="sqa-coach",
        model=os.getenv("SQA_LLM_MODEL_COACH", "gpt-5.4"),
        api_key=api_key,
    )
