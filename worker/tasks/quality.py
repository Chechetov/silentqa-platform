"""
Оценка качества разговора менеджера через OpenAI GPT-5.4.
Анализирует транскрипт на соответствие регламенту,
качество общения и результат разговора.

Поддерживает два режима:
- Базовый (v2): стандартная оценка для любых звонков
- Расширенный (v3): чек-лист скрипта, классификация, возражения,
  информация от клиента — для сценариев с детальным протоколом
"""
import json
import logging
import os

from openai import OpenAI

from prompts.sales_playbook import get_meeting_playbook_prompt

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL = """
Регламент разговора менеджера:
1. Приветствие и представление (имя, компания)
2. Выяснение потребности клиента
3. Презентация решения / ответ на вопрос
4. Работа с возражениями (если есть)
5. Согласование следующего шага / закрытие
6. Вежливое прощание

Критерии качества:
- Вежливость и профессионализм
- Активное слушание (не перебивает)
- Конкретные ответы на вопросы
- Предложение альтернатив при отказе
- Фиксация договорённостей
"""

# === V2 Schema: базовая оценка (обратная совместимость) ===

QUALITY_JSON_SCHEMA = {
    "name": "quality_assessment",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "overall_score": {"type": "integer", "description": "Общая оценка 0-10"},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "score": {"type": "integer", "description": "0-10"},
                        "comment": {"type": "string"}
                    },
                    "required": ["name", "score", "comment"],
                    "additionalProperties": False
                }
            },
            "protocol_adherence": {
                "type": "object",
                "properties": {
                    "steps_completed": {"type": "array", "items": {"type": "string"}},
                    "steps_missed": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["steps_completed", "steps_missed"],
                "additionalProperties": False
            },
            "conversation_outcome": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["result", "description"],
                "additionalProperties": False
            },
            "key_moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "number", "description": "секунды от начала"},
                        "description": {"type": "string"},
                        "type": {"type": "string"}
                    },
                    "required": ["time", "description", "type"],
                    "additionalProperties": False
                }
            },
            "improvement_suggestions": {"type": "array", "items": {"type": "string"}},
            "detailed_summary": {"type": "string", "description": "Подробный пересказ разговора по блокам: потребности клиента, предложенные объекты/решения, работа с возражениями, ключевые аргументы, итог и договорённости. 5-15 предложений."},
            "summary": {"type": "string"},
            "speaker_roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "speaker_id": {"type": "string"},
                        "role": {"type": "string"},
                        "name": {"type": ["string", "null"]}
                    },
                    "required": ["speaker_id", "role", "name"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["overall_score", "criteria", "protocol_adherence",
                      "conversation_outcome", "key_moments",
                      "improvement_suggestions", "detailed_summary",
                      "summary", "speaker_roles"],
        "additionalProperties": False
    }
}

# === V3 Schema: расширенная оценка с чек-листом, классификацией, возражениями ===

QUALITY_JSON_SCHEMA_V3 = {
    "name": "quality_assessment_v3",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "overall_score": {"type": "integer", "description": "Общая оценка 0-10"},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "score": {"type": "integer", "description": "0-10"},
                        "comment": {"type": "string"}
                    },
                    "required": ["name", "score", "comment"],
                    "additionalProperties": False
                }
            },
            "call_classification": {
                "type": "object",
                "description": "Классификация типа звонка",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "brushoff_short | brushoff_with_attempt | partial | productive | meeting_scheduled"
                    },
                    "description": {"type": "string"},
                    "reason": {"type": "string"}
                },
                "required": ["type", "description", "reason"],
                "additionalProperties": False
            },
            "protocol_checklist": {
                "type": "array",
                "description": "Чек-лист выполнения скрипта по группам",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "name": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "description": "completed | attempted | not_applicable | not_reached"
                                    },
                                    "comment": {"type": "string"}
                                },
                                "required": ["id", "name", "status", "comment"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["id", "name", "items"],
                    "additionalProperties": False
                }
            },
            "protocol_adherence": {
                "type": "object",
                "properties": {
                    "steps_completed": {"type": "array", "items": {"type": "string"}},
                    "steps_missed": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["steps_completed", "steps_missed"],
                "additionalProperties": False
            },
            "conversation_outcome": {
                "type": "object",
                "properties": {
                    "result": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["result", "description"],
                "additionalProperties": False
            },
            "objections": {
                "type": "array",
                "description": "Возражения клиента и их отработка",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Что сказал клиент"},
                        "category": {
                            "type": "string",
                            "description": "already_contacted | no_time | not_interested | too_expensive | has_broker | just_looking | send_info | other"
                        },
                        "broker_response": {"type": "string", "description": "Как брокер отработал возражение"},
                        "handling_quality": {"type": "integer", "description": "Качество отработки 0-10"},
                        "resolved": {"type": "boolean", "description": "Возражение снято?"}
                    },
                    "required": ["text", "category", "broker_response", "handling_quality", "resolved"],
                    "additionalProperties": False
                }
            },
            "client_info": {
                "type": "object",
                "description": "Информация, полученная ОТ КЛИЕНТА (не от менеджера)",
                "properties": {
                    "purchase_goal": {"type": ["string", "null"], "description": "Цель покупки (жизнь/инвестиции)"},
                    "locations": {"type": ["string", "null"], "description": "Предпочтения по районам/локациям"},
                    "apartment_format": {"type": ["string", "null"], "description": "Формат квартиры (спальни, площадь, планировка)"},
                    "timeline": {"type": ["string", "null"], "description": "Сроки покупки/сдачи"},
                    "budget": {"type": ["string", "null"], "description": "Бюджет клиента"},
                    "payment_form": {"type": ["string", "null"], "description": "Форма оплаты (ипотека, ПВ, рассрочка)"},
                    "important_factors": {"type": ["string", "null"], "description": "Что важно (виды, школа, парк, этаж)"},
                    "what_viewed": {"type": ["string", "null"], "description": "Что уже смотрели, что понравилось"},
                    "current_situation": {"type": ["string", "null"], "description": "Текущая ситуация клиента"},
                    "objections_voiced": {"type": ["string", "null"], "description": "Озвученные возражения/сомнения"},
                    "other": {"type": ["string", "null"], "description": "Прочая важная информация"}
                },
                "required": ["purchase_goal", "locations", "apartment_format", "timeline",
                             "budget", "payment_form", "important_factors", "what_viewed",
                             "current_situation", "objections_voiced", "other"],
                "additionalProperties": False
            },
            "general_checks": {
                "type": "array",
                "description": "Бинарные проверки общих требований",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "met": {"type": "boolean"},
                        "count": {"type": ["integer", "null"], "description": "Счётчик (если применимо)"},
                        "comment": {"type": "string"}
                    },
                    "required": ["id", "name", "met", "count", "comment"],
                    "additionalProperties": False
                }
            },
            "brief_summary": {
                "type": "string",
                "description": "1-2 предложения для AmoCRM: суть звонка и ключевой результат"
            },
            "client_info_summary": {
                "type": "string",
                "description": "2-3 предложения: что узнали от клиента (бюджет, район, требования)"
            },
            "key_moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "number", "description": "секунды от начала"},
                        "description": {"type": "string"},
                        "type": {"type": "string"}
                    },
                    "required": ["time", "description", "type"],
                    "additionalProperties": False
                }
            },
            "improvement_suggestions": {"type": "array", "items": {"type": "string"}},
            "detailed_summary": {"type": "string"},
            "summary": {"type": "string"},
            "speaker_roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "speaker_id": {"type": "string"},
                        "role": {"type": "string"},
                        "name": {"type": ["string", "null"]}
                    },
                    "required": ["speaker_id", "role", "name"],
                    "additionalProperties": False
                }
            }
        },
        "required": [
            "overall_score", "criteria", "call_classification",
            "protocol_checklist", "protocol_adherence",
            "conversation_outcome", "objections", "client_info",
            "general_checks", "brief_summary", "client_info_summary",
            "key_moments", "improvement_suggestions",
            "detailed_summary", "summary", "speaker_roles"
        ],
        "additionalProperties": False
    }
}

# === V4 Schema: context-aware evaluation with stage progression ===

QUALITY_JSON_SCHEMA_V4 = {
    "name": "quality_assessment_v4",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            **{k: v for k, v in QUALITY_JSON_SCHEMA_V3["schema"]["properties"].items()},
            "stage_progression": {
                "type": "object",
                "description": "Как этот звонок продвинул сделку по сравнению с прошлыми",
                "properties": {
                    "new_info_learned": {"type": "array", "items": {"type": "string"}},
                    "objections_resolved": {"type": "array", "items": {"type": "string"}},
                    "objections_raised": {"type": "array", "items": {"type": "string"}},
                    "profile_updates": {"type": "array", "items": {"type": "string"}},
                    "progress_delta": {"type": "string"},
                    "stage_advanced": {"type": "boolean"},
                },
                "required": ["new_info_learned", "objections_resolved", "objections_raised",
                             "profile_updates", "progress_delta", "stage_advanced"],
                "additionalProperties": False,
            },
            "applicable_checklist_items": {
                "type": "object",
                "description": "Какие пункты чек-листа пропущены, потому что уже сделаны ранее",
                "properties": {
                    "skipped_as_already_done": {"type": "array", "items": {"type": "string"}},
                    "newly_applicable": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skipped_as_already_done", "newly_applicable"],
                "additionalProperties": False,
            },
            "previous_recommendations_follow_through": {
                "type": "object",
                "description": "Как брокер выполнил рекомендации из плана ПРЕДЫДУЩЕГО звонка",
                "properties": {
                    "total_recommendations": {"type": "integer"},
                    "executed_count": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "recommendation_text": {"type": "string"},
                                "executed": {
                                    "type": "string",
                                    "description": "yes | no | partial",
                                },
                                "evidence": {"type": ["string", "null"]},
                                "effectiveness": {"type": ["string", "null"]},
                            },
                            "required": ["recommendation_text", "executed", "evidence", "effectiveness"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["total_recommendations", "executed_count", "items"],
                "additionalProperties": False,
            },
            "meeting_argumentation_assessment": {
                "type": "object",
                "description": (
                    "Оценка качества аргументации при закрытии на встречу. "
                    "Заполняется на основе MEETING_PLAYBOOK из sales_playbook."
                ),
                "properties": {
                    "attempted": {
                        "type": "boolean",
                        "description": "Пытался ли брокер предложить/закрыть на встречу в этом звонке",
                    },
                    "arguments_used": {
                        "type": "array",
                        "description": "Аргументы из playbook, реально использованные брокером",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category_id": {
                                    "type": "string",
                                    "description": "ID категории из MEETING_PLAYBOOK (см. список в system prompt)",
                                },
                                "quote_from_broker": {
                                    "type": "string",
                                    "description": "Короткая цитата из транскрипта — что именно сказал брокер",
                                },
                                "effectiveness": {
                                    "type": "string",
                                    "description": "strong | adequate | weak — насколько убедительно прозвучало",
                                },
                            },
                            "required": ["category_id", "quote_from_broker", "effectiveness"],
                            "additionalProperties": False,
                        },
                    },
                    "objections_faced": {
                        "type": "array",
                        "description": "Возражения клиента против встречи и реакция брокера",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Что сказал клиент"},
                                "broker_response": {"type": "string", "description": "Как брокер ответил"},
                                "addressed": {
                                    "type": "boolean",
                                    "description": "Был ли применён подходящий аргумент из playbook",
                                },
                            },
                            "required": ["text", "broker_response", "addressed"],
                            "additionalProperties": False,
                        },
                    },
                    "missed_opportunities": {
                        "type": "array",
                        "description": (
                            "Моменты, где следовало применить конкретную категорию из playbook, "
                            "но брокер этого не сделал или сделал слабо"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "trigger_quote": {
                                    "type": "string",
                                    "description": "Реплика клиента, которая должна была запустить аргумент",
                                },
                                "recommended_category_id": {
                                    "type": "string",
                                    "description": "ID категории playbook, которую надо было применить",
                                },
                                "why": {
                                    "type": "string",
                                    "description": "Почему эта категория уместна для данного момента",
                                },
                            },
                            "required": ["trigger_quote", "recommended_category_id", "why"],
                            "additionalProperties": False,
                        },
                    },
                    "overall_push_quality": {
                        "type": "string",
                        "description": (
                            "strong | adequate | weak | not_applicable. "
                            "not_applicable — если встреча уже была назначена ранее или этап сделки "
                            "не требует закрытия на встречу (например, договор)."
                        ),
                    },
                    "meeting_formats_offered": {
                        "type": "array",
                        "description": (
                            "Форматы встречи, которые брокер предлагал: подмножество {zoom, office, client}. "
                            "Пустой массив — если никакой формат не озвучен."
                        ),
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "attempted", "arguments_used", "objections_faced",
                    "missed_opportunities", "overall_push_quality",
                    "meeting_formats_offered",
                ],
                "additionalProperties": False,
            },
        },
        "required": QUALITY_JSON_SCHEMA_V3["schema"]["required"] + [
            "stage_progression", "applicable_checklist_items",
            "previous_recommendations_follow_through",
            "meeting_argumentation_assessment",
        ],
        "additionalProperties": False,
    },
}

# === Next-call plan schema (separate LLM call) ===

NEXT_CALL_PLAN_SCHEMA = {
    "name": "next_call_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["priority", "text"],
                    "additionalProperties": False,
                },
            },
            "unresolved_objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "suggested_response": {"type": "string"},
                        "priority": {"type": "string"},
                    },
                    "required": ["text", "suggested_response", "priority"],
                    "additionalProperties": False,
                },
            },
            "information_gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["field", "why"],
                    "additionalProperties": False,
                },
            },
            "talking_points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "argument": {"type": "string"},
                        "personalized_hook": {"type": "string"},
                    },
                    "required": ["topic", "argument", "personalized_hook"],
                    "additionalProperties": False,
                },
            },
            "recommended_properties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["project", "reason"],
                    "additionalProperties": False,
                },
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "suggested_opener": {"type": "string"},
        },
        "required": ["goals", "unresolved_objections", "information_gaps",
                     "talking_points", "recommended_properties",
                     "risks", "suggested_opener"],
        "additionalProperties": False,
    },
}

_PLAN_SYSTEM_PROMPT_BASE = """Ты — consultative-sales coach для брокера элитной недвижимости Москвы.

Твоя задача: на основе всей истории взаимодействий с клиентом и результата последнего
звонка — построить конкретный план следующего взаимодействия.

Правила:
- Goals: 1-3 цели, упорядоченные по приоритету.
- Unresolved_objections: перечисли ТОЛЬКО возражения, которые сейчас в open
  (resolved опускай). Предложи конкретный ответ брокера.
- Information_gaps: ключевые недостающие поля клиентского профиля, с обоснованием "зачем".
- Talking_points: аргументы, привязанные к конкретным фактам о клиенте
  (используй поля из client_profile как personalized_hook).
- Recommended_properties: если контекст указывает на конкретные ЖК — добавь
  их с обоснованием. НЕ ВЫДУМЫВАЙ факты о ЖК — используй только то,
  что было упомянуто в прошлых звонках или профиле клиента.
- Risks: что может пойти не так, на что обратить внимание.
- Suggested_opener: одна короткая фраза для начала следующего звонка, с именем клиента
  и конкретной зацепкой.

Все тексты — по-русски, естественные формулировки.

## Стадия сделки

Если в user-payload передан `deal_stage`, рекомендации должны быть уместны
для этого этапа:
- «Квалификация»: фокус на discovery-вопросах; глубже узнавать потребности;
  не форсировать встречу.
- «Показы»: аргументы по конкретным объектам; отработка «посмотрю ещё»;
  договорённость о следующем показе.
- «Сделка» / «Договор»: closing-сценарии; детали оформления; снятие
  финальных возражений.
- Для нераспознанных стадий — опирайся на здравый смысл.

## Офлайн-пробелы (gaps)

В interactions_history могут быть элементы с type="offline_gap_inferred"
или has_data=false — это периоды, когда что-то произошло в офлайне
(встреча, мессенджер) и у нас нет записей. Не делай выводов из их
отсутствия. Если клиент в текущем звонке ссылается на обсуждение,
которое не видно в истории — предположи, что оно было в офлайне.

## Аргументация встречи (использовать playbook ниже)

Если встреча ещё не назначена (и стадия сделки этого требует — «Квалификация»
по итогам снятия запроса, «Показы») — одно из главных назначений плана
обеспечить следующее закрытие на встречу. Используй библиотеку аргументов,
приведённую в конце этого промпта:

- Для `talking_points`:
  - Если клиент в прошлом звонке возразил (просил подборку, сказал «я подумаю»,
    «я сам посмотрю», «нет времени» и т.п.) — подбирай категорию из раздела
    «Отработка отказа» по ближайшим триггерам.
  - Если возражения ещё не было, но встреча не закрыта — бери категорию из
    раздела «Первый заход».
  - В поле `topic` ОБЯЗАТЕЛЬНО используй ID категории playbook (например,
    `saves_time`, `buying_strategy`, `portfolio_presentation`) — именно ID,
    не свободный текст. Это нужно для последующей оценки follow-through.
  - В поле `argument` дай готовую формулировку в духе примеров из playbook,
    переформулированную под контекст клиента (можно цитировать, можно адаптировать).
  - В поле `personalized_hook` — привязка к конкретным фактам из `client_profile`
    (бюджет, локация, цели покупки, семейная ситуация).

- Для `unresolved_objections.suggested_response`:
  - Если открытое возражение — это отказ от встречи, пиши ответ брокера в духе
    подходящей категории `objection_responses` и в начале ответа в квадратных
    скобках укажи `[playbook: <category_id>]`, чтобы можно было сопоставить.

- Для `suggested_opener`:
  - Если на прошлом звонке встреча не закрыта — опирайся на категорию
    `joint_preparation` или `portfolio_presentation` при формулировке зацепки.

- Формат встречи: по умолчанию Zoom (30-40 мин). Если клиент уже обозначил
  предпочтение (офис / у клиента) — учитывай. В `argument` можно упомянуть
  «короткая онлайн-встреча» как мягкий дефолт.

"""

_MEETING_PLAYBOOK_PROMPT = get_meeting_playbook_prompt()

PLAN_SYSTEM_PROMPT = _PLAN_SYSTEM_PROMPT_BASE + _MEETING_PLAYBOOK_PROMPT


def plan_next_call(
    prior_context: dict,
    current_quality_report: dict,
    deal_stage: str | None = None,
) -> dict | None:
    """
    Generate a forward-looking plan via a second LLM call.

    Returns the parsed plan dict, or None on failure (caller must not block on this).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set, skipping next-call plan")
        return None

    client = OpenAI(api_key=api_key)

    # Strip heavy fields from current report — plan doesn't need transcript/key_moments
    compact_current = {
        k: current_quality_report.get(k)
        for k in (
            "brief_summary", "summary", "overall_score", "call_classification",
            "conversation_outcome", "objections", "client_info",
            "improvement_suggestions", "stage_progression",
        )
        if current_quality_report.get(k) is not None
    }

    user_payload = {
        "prior_context": prior_context,
        "current_call": compact_current,
        "deal_stage": deal_stage,
    }

    try:
        response = client.responses.create(
            model="gpt-5.4",
            input=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": NEXT_CALL_PLAN_SCHEMA["name"],
                    "strict": True,
                    "schema": NEXT_CALL_PLAN_SCHEMA["schema"],
                }
            },
        )
        plan = json.loads(response.output_text)
        logger.info(f"Next-call plan generated with {len(plan.get('goals', []))} goals")
        return plan
    except Exception:
        logger.exception("Failed to generate next-call plan")
        return None


DEFAULT_CRITERIA = [
    {"id": "protocol_adherence", "name": "Соблюдение регламента", "description": "Выполнение шагов протокола разговора"},
    {"id": "politeness", "name": "Вежливость", "description": "Вежливость и профессионализм в общении"},
    {"id": "listening", "name": "Слушание", "description": "Активное слушание, не перебивает клиента"},
    {"id": "clarity", "name": "Ясность", "description": "Понятные и конкретные ответы на вопросы"},
    {"id": "empathy", "name": "Эмпатия", "description": "Эмоциональная отзывчивость и понимание клиента"},
]

SYSTEM_PROMPT = """Ты — эксперт по оценке качества работы менеджеров в телефонных/видео переговорах.

Правила:
- Все оценки (overall_score, criteria[].score) — целые числа 0-10
- key_moments[].time — число секунд от начала (float)
- conversation_outcome.result — одно из: sale, appointment, complaint_resolved, escalation, lost, info_provided, other
- criteria содержит следующие элементы (каждый оценивается 0-10):
{criteria_instructions}
- speaker_roles: ОБЯЗАТЕЛЬНО определи роль каждого спикера (manager/client/doctor/other).
  Внимательно ищи имена в тексте — приветствия ("Здравствуйте, меня зовут Анна"), обращения ("Иван Петрович"),
  представления ("Это Алексей из компании..."). Если имя упомянуто хотя бы раз — укажи его в поле name.
  Если имя не найдено — укажи null, но попробуй определить роль по контексту разговора.
- key_moments[].type — одно из: positive, negative, neutral
- summary: краткое резюме в 2-4 предложениях — суть разговора и итог. Используй реальные имена участников, если удалось их определить (например, "Менеджер Анна рассказала клиенту Ивану о..." вместо "SPEAKER_00 рассказал SPEAKER_01")
- detailed_summary: подробный пересказ разговора по блокам. Укажи: потребность клиента, какие объекты/решения были предложены, как менеджер работал с возражениями, ключевые аргументы, итог и договорённости. Используй реальные имена. 5-15 предложений.

{custom_instructions}"""

SYSTEM_PROMPT_V3 = """Ты — эксперт по оценке качества работы менеджеров в телефонных/видео переговорах.
Ты оцениваешь звонки по детальному скрипту с чек-листом.

## Основные правила
- Все оценки (overall_score, criteria[].score) — целые числа 0-10
- key_moments[].time — число секунд от начала (float)
- key_moments[].type — одно из: positive, negative, neutral
- conversation_outcome.result — одно из: sale, appointment, complaint_resolved, escalation, lost, info_provided, other

## Критерии оценки (каждый 0-10):
{criteria_instructions}

## Классификация звонка (call_classification.type):
- brushoff_short: клиент сразу отшил, разговор < 2 минут, брокер не успел ничего сделать
- brushoff_with_attempt: клиент отшивает, но брокер ПЫТАЛСЯ перехватить инициативу или отработать возражения
- partial: разговор состоялся, но не все этапы скрипта пройдены (прервали, неудобно говорить и т.д.)
- productive: полноценный разговор, основные этапы скрипта пройдены
- meeting_scheduled: встреча назначена (лучший исход)

## Чек-лист скрипта (protocol_checklist):
Для КАЖДОГО пункта чек-листа определи статус:
- completed: пункт полностью выполнен
- attempted: брокер пытался, но клиент не дал завершить (ВАЖНО для brush-off звонков!)
- not_applicable: пункт не применим к данному звонку (например, "перехват инициативы" если клиент не говорил что уже получил информацию)
- not_reached: до этого пункта разговор не дошёл

ЗАПОЛНИ ВСЕ ГРУППЫ чек-листа:
- greeting: Приветствие и представление (greeting_by_name, mentioned_project, introduced_self, gave_basic_info)
- initiative: Перехват инициативы (initiative_attempt)
- needs_discovery: Снятие запроса (sold_interview, purchase_goal, locations, apartment_format, timeline, budget, budget_pushback, additional_questions, thanked_for_info, summarized_request)
- proposal: Предложение (specific_projects, explained_fit)
- meeting: Назначение встречи (meeting_proposed, meeting_format_offered, meeting_second_attempt, meeting_value_explained, broker_value)
- agreements: Договорённости (meeting_scheduled, next_contact)

## Оценка при brush-off:
- При brushoff_short: оценивай ТОЛЬКО то, что брокер успел сделать. Если клиент сразу повесил трубку — ставь не ниже 3/10 за попытку.
- При brushoff_with_attempt: если брокер пытался перехватить инициативу и отработать возражение — это 6-7/10 даже если клиент отказал. Это ХОРОШАЯ работа.
- Пункты со статусом not_reached НЕ ДОЛЖНЫ снижать overall_score и criteria[].score.

## Возражения (objections):
Отдельно фиксируй КАЖДОЕ возражение клиента:
- text: что именно сказал клиент
- category: already_contacted | no_time | not_interested | too_expensive | has_broker | just_looking | send_info | other
- broker_response: как брокер отработал
- handling_quality: 0-10
- resolved: было ли снято

## Информация от клиента (client_info):
Извлекай ТОЛЬКО то, что сообщил КЛИЕНТ (не менеджер). Это ключевая ценность звонка.
Если информация не была озвучена — ставь null.

## Общие проверки (general_checks):
Проверь каждый пункт:
- client_name_3_times: Имя клиента произнесено 3+ раз (count = сколько раз)
- greeted_client: Поприветствовал клиента
- active_listening: Активное слушание (не перебивает, переспрашивает)
- followed_sequence: Соблюдает последовательность этапов
- probing_questions: Задаёт уточняющие вопросы
- showed_initiative: Проявляет инициативу
- meeting_proposed_2_times: Предложил встречу минимум 2 раза (count = сколько раз)
- polite_no_interrupting: Вежлив, не перебивает
- thanked_at_end: Поблагодарил в конце
- no_uncertainty_words: Нет слов неуверенности (наверное, могли бы, достаточно)
- kept_client_engaged: Удерживает клиента в звонке
- positive_tone: Позитивный настрой в голосе

## brief_summary:
1-2 предложения для CRM-примечания: суть звонка и ключевой результат.

## client_info_summary:
2-3 предложения: что конкретно узнали от клиента (бюджет, район, требования).
Если звонок был brush-off и ничего не узнали — напиши это честно.

## speaker_roles:
ОБЯЗАТЕЛЬНО определи роль каждого спикера (manager/client/other).
Ищи имена в тексте. Если имя не найдено — укажи null.

## summary и detailed_summary:
Используй реальные имена. summary: 2-4 предложения. detailed_summary: 5-15 предложений.

{custom_instructions}"""

CONTEXT_AWARE_INSTRUCTIONS = """

## Контекст взаимодействий с клиентом

Этот звонок может быть не первым. В блоке prior_context (в user-промпте)
есть:
- call_number — номер текущего звонка (1 = первый).
- client_profile — накопленный профиль клиента (бюджет, локации, сроки,
  и т.д.), собранный из прошлых звонков. Если значение изменилось,
  формат "новое (ранее: старое, пересмотрено DATE)".
- interactions_history — краткое описание прошлых звонков.
- open_objections / resolved_objections — история возражений.

Правила при наличии prior_context:
- Если call_number > 1 — НЕ штрафуй брокера за пропуск приветствия,
  представления, упоминания ЖК (пункты greeting_by_name, introduced_self,
  mentioned_project в чек-листе). Ставь им status="not_applicable"
  с comment вида "Уже сделано в звонке от <дата>".
- Если информация уже известна (бюджет, локация, сроки) — не требуй
  задавать эти вопросы заново. Status="not_applicable" + объяснение.
- Оценивай прогресс относительно prior_context. Заполни блок
  stage_progression: что нового узнали, какие возражения сняли/подняли,
  продвинулась ли сделка.
- Если клиент изменил позицию (например, бюджет уменьшился) — зафиксируй
  в stage_progression.profile_updates.
- В applicable_checklist_items.skipped_as_already_done перечисли ID
  пунктов, которые ты проставил not_applicable по причине уже-сделано.
- Используй реальные имена, проекты, детали из prior_context.

## Follow-through по плану прошлого звонка

В prior_context может быть поле `last_call_plan` — рекомендации, которые
были даны после ПРЕДЫДУЩЕГО звонка. Для КАЖДОЙ рекомендации из
`last_call_plan.goals` и `last_call_plan.talking_points` определи:

- `executed`: "yes" | "no" | "partial"
- `evidence`: короткая цитата из транскрипта текущего звонка,
  подтверждающая выполнение; `null` если не выполнено.
- `effectiveness`: если выполнено — как это сработало; `null` если не выполнено.

Заполни `previous_recommendations_follow_through`:
- `total_recommendations` = сумма `len(goals) + len(talking_points)` прошлого плана.
- `executed_count` = сколько из них "yes".
- `items` — перечень с полями выше.

Если `last_call_plan` отсутствует (первый звонок или план не был
сгенерирован) — верни пустой блок: `total_recommendations=0,
executed_count=0, items=[]`.

## Офлайн-пробелы (gaps)

В interactions_history могут быть элементы с type="offline_gap_inferred"
или has_data=false — это периоды, когда что-то произошло в офлайне
(встреча, мессенджер) и у нас нет записей. Не делай выводов из их
отсутствия. Если клиент в текущем звонке ссылается на обсуждение,
которое не видно в истории — предположи, что оно было в офлайне.

## Оценка аргументации встречи (meeting_argumentation_assessment)

Заполни поле `meeting_argumentation_assessment` на основе Библиотеки
аргументов закрытия на встречу (см. конец промпта). Это ключевая
оценка — брокеру она нужна и для коучинга по данной сделке, и для
накопительной аналитики по всем звонкам.

Алгоритм:

1. Определи, УМЕСТНО ли вообще закрытие на встречу в этом звонке:
   - Если встреча уже была назначена раньше, или стадия сделки = «Договор»/
     «Сделка», или звонок — короткий brush-off (< 2 мин, клиент сразу отшил) —
     ставь `overall_push_quality = "not_applicable"`, всё остальное оставляй
     пустыми массивами, `attempted` по факту.
   - Иначе — заполняй поля осмысленно.

2. `attempted`: пытался ли брокер предложить встречу хотя бы один раз.

3. `arguments_used`: для КАЖДОГО озвученного брокером аргумента про встречу:
   - Подбери ближайшую категорию из playbook (`category_id` — snake_case ID).
   - `quote_from_broker` — короткая реальная цитата из транскрипта.
   - `effectiveness`: "strong" (конкретно, с привязкой к клиенту, уверенно) /
     "adequate" (общо, но по делу) / "weak" (вяло, шаблонно, без привязки).
   - Если ни один аргумент про встречу не звучал — оставь пустой массив.

4. `objections_faced`: каждое возражение клиента ПРОТИВ встречи («пришлите
   подборку», «я подумаю», «нет времени» и т.п.):
   - `addressed = true`, если брокер ответил по соответствующей категории
     из playbook. `false` — если проигнорировал или ушёл от темы.

5. `missed_opportunities`: моменты, где брокер МОГ применить конкретную
   категорию, но не сделал этого. Это самый ценный блок для коучинга.
   - `trigger_quote` — реплика клиента, которая должна была запустить аргумент.
   - `recommended_category_id` — ID категории из playbook.
   - `why` — почему именно эта категория уместна здесь.
   - Если `attempted=false` и клиент отказался от встречи — добавь хотя бы
     2-3 рекомендации, чтобы брокер на следующий раз знал, что сказать.

6. `overall_push_quality`:
   - "strong" — встреча назначена, или брокер использовал ≥2 релевантных
     аргументов, покрыл возражения, закрыл осмысленно.
   - "adequate" — 1-2 аргумента, часть возражений отработана.
   - "weak" — встреча не назначена и аргументы либо не прозвучали, либо
     были шаблонными/не попали в возражение клиента.
   - "not_applicable" — по правилу из п.1.

7. `meeting_formats_offered`: массив из [`zoom`, `office`, `client`] — только
   те форматы, которые реально прозвучали в речи брокера. Пустой массив,
   если брокер просто «встретимся» без уточнения формата.

Важно: при заполнении `improvement_suggestions` для звонков, где
`overall_push_quality` = "weak" и встреча не назначена — одной из рекомендаций
должна быть конкретная отсылка к категории из playbook (например: «В ответ
на "я подумаю" применить категорию `buying_strategy` — показать, что встреча
это разбор стратегии, а не просто перечисление объектов»).
"""

SYSTEM_PROMPT_V4 = SYSTEM_PROMPT_V3 + CONTEXT_AWARE_INSTRUCTIONS + "\n\n" + _MEETING_PLAYBOOK_PROMPT


# === Template-driven prompt ===
# Used when an evaluation template (kind=evaluation) is in effect. The template
# bakes its own protocol + classification + checklist semantics into the user
# prompt's "Регламент" block, so the system prompt MUST NOT inject hardcoded
# outbound-call structure (groups, classification enum, brush-off rules,
# meeting-closing playbook) — those silently override the template's intent
# and pull the evaluation back to outbound framing.
SYSTEM_PROMPT_V3_TEMPLATE = """Ты — эксперт по оценке качества работы менеджеров в телефонных/видео переговорах.
Ты оцениваешь разговор по детальному регламенту. Регламент находится в user-промпте в блоке «Регламент» — он определяет этапы, критерии, правила классификации и общие требования. Следуй ему.

## Основные правила
- Все оценки (overall_score, criteria[].score) — целые числа 0-10
- key_moments[].time — число секунд от начала (float)
- key_moments[].type — одно из: positive, negative, neutral
- conversation_outcome.result — одно из: sale, appointment, complaint_resolved, escalation, lost, info_provided, other

## Критерии оценки (каждый 0-10):
{criteria_instructions}

## Классификация (call_classification.type):
Используй категорию, описанную в блоке «Регламент». Если регламент задаёт собственный набор значений (например prep_only / partial_presentation / closed_with_visit) — выбирай ТОЛЬКО из них и не подмешивай категории других скриптов.

## Чек-лист скрипта (protocol_checklist):
Сформируй protocol_checklist строго из ЭТАПОВ Регламента (пронумерованные блоки 1, 2, 3, …). Для каждого этапа создай ОДНУ группу:
- id: латинский snake_case-идентификатор этапа (например preparation, connection_check, presentation_quality)
- name: русское название этапа из заголовка Регламента
- items: конкретные подпункты этого этапа из текста Регламента; status — completed | attempted | not_applicable | not_reached; comment — что именно произошло.

ЗАПРЕЩЕНО использовать чужие категории чек-листа (greeting / initiative / needs_discovery / proposal / meeting / agreements и подобные) — это структура другого скрипта. protocol_checklist должен ТОЧНО отражать пронумерованные этапы текущего Регламента.

## Оценка при незавершённом разговоре:
- Если до пункта Регламента не дошли — status="not_reached"; такие пункты НЕ должны снижать overall_score и criteria[].score.
- Если брокер пытался выполнить пункт, но клиент не дал — status="attempted"; оценивай попытку, а не результат.
- Если пункт не применим к данному разговору (например, follow-up без повторного приветствия) — status="not_applicable" с пояснением.

## Возражения (objections):
Отдельно фиксируй КАЖДОЕ возражение клиента:
- text: что именно сказал клиент
- category: already_contacted | no_time | not_interested | too_expensive | has_broker | just_looking | send_info | other
- broker_response: как брокер отработал
- handling_quality: 0-10
- resolved: было ли снято

## Информация от клиента (client_info):
Извлекай ТОЛЬКО то, что сообщил КЛИЕНТ (не менеджер). Если информация не была озвучена — ставь null.

## Общие проверки (general_checks):
Заполняй на основе общих требований к коммуникации, описанных в Регламенте (имя клиента, активное слушание, вежливость, отсутствие слов неуверенности и т.п.). НЕ выдумывай outbound-специфичные пункты вроде «meeting_proposed_2_times», если их нет в Регламенте.

## brief_summary:
1-2 предложения для CRM-примечания: суть разговора и ключевой результат — в фрейминге Регламента (визит к застройщику, презентация ЖК, и т.п.), а не outbound-фрейминге «закрыли на встречу».

## client_info_summary:
2-3 предложения: что конкретно узнали или подтвердили в разговоре с клиентом.

## speaker_roles:
ОБЯЗАТЕЛЬНО определи роль каждого спикера (manager/client/other). Ищи имена в тексте.

## summary и detailed_summary:
Используй реальные имена. summary: 2-4 предложения. detailed_summary: 5-15 предложений. Фрейминг — из Регламента.

{custom_instructions}"""


SYSTEM_PROMPT_V4_TEMPLATE = SYSTEM_PROMPT_V3_TEMPLATE + CONTEXT_AWARE_INSTRUCTIONS

USER_PROMPT = """## Регламент
{protocol}

## Транскрипт
{transcript}

## Тональность
{sentiment_summary}

Проанализируй разговор и дай оценку."""

# Legacy prompt for fallback
ASSESSMENT_PROMPT = """Ты — эксперт по оценке качества работы менеджеров в телефонных/видео переговорах.

Проанализируй следующий транскрипт разговора и дай оценку.

## Регламент
{protocol}

## Транскрипт разговора (с указанием спикеров)
{transcript}

## Данные по тональности
{sentiment_summary}

## Задание
Проанализируй разговор и верни JSON (строго без markdown, только JSON):
{{
  "overall_score": <число 0-10>,
  "criteria": [
    {{"name": "protocol_adherence", "score": <0-10>, "comment": "<комментарий>"}},
    {{"name": "politeness", "score": <0-10>, "comment": "<комментарий>"}},
    {{"name": "listening", "score": <0-10>, "comment": "<комментарий>"}},
    {{"name": "clarity", "score": <0-10>, "comment": "<комментарий>"}},
    {{"name": "empathy", "score": <0-10>, "comment": "<комментарий>"}}
  ],
  "protocol_adherence": {{
    "steps_completed": ["список выполненных шагов регламента"],
    "steps_missed": ["список пропущенных шагов"]
  }},
  "communication_quality": {{
    "score": <число 0-10>,
    "politeness": <число 0-10>,
    "listening": <число 0-10>,
    "clarity": <число 0-10>,
    "empathy": <число 0-10>
  }},
  "conversation_outcome": {{
    "result": "<sale|appointment|complaint_resolved|escalation|lost|info_provided|other>",
    "description": "<краткое описание результата>"
  }},
  "key_moments": [
    {{"time": <секунды от начала>, "description": "<описание важного момента>", "type": "<positive|negative|neutral>"}}
  ],
  "improvement_suggestions": ["список конкретных рекомендаций для менеджера"],
  "summary": "<краткое резюме разговора в 2-4 предложениях>",
  "detailed_summary": "<подробный пересказ по блокам: потребность клиента, предложенные объекты/решения, работа с возражениями, ключевые аргументы, итог и договорённости. 5-15 предложений>",
  "speaker_roles": {{
    "<SPEAKER_ID>": {{"role": "<manager|client>", "name": "<имя если упомянуто, иначе null>"}},
    ...для каждого спикера в транскрипте
  }}
}}

{custom_instructions}
"""


def format_transcript_for_llm(transcript: list[dict], max_chars: int = 30000) -> str:
    """Форматирует транскрипт для отправки в LLM. Обрезает по целым сегментам."""
    lines = []
    total_len = 0
    for s in transcript:
        speaker = s.get("speaker", "?")
        time_str = f"[{s['start']:.0f}s-{s['end']:.0f}s]"
        line = f"{speaker} {time_str}: {s['text']}"
        line_len = len(line) + 1  # +1 for newline
        if total_len + line_len > max_chars:
            lines.append("\n[... транскрипт обрезан для экономии ...]")
            break
        lines.append(line)
        total_len += line_len

    return "\n".join(lines)


def _convert_speaker_roles(roles_list: list[dict]) -> dict:
    """Convert speaker_roles array back to dict keyed by speaker_id for backward compat."""
    result = {}
    for item in roles_list:
        sid = item.get("speaker_id", "")
        result[sid] = {"role": item.get("role", ""), "name": item.get("name")}
    return result


def _build_criteria_instructions(criteria_config: list[dict] | None) -> str:
    """Build criteria instructions string for LLM prompt.

    Format leads with the human-readable name so the LLM uses it verbatim as
    the `name` field in `criteria[].name` (instead of picking up the latin id).
    """
    criteria = criteria_config or DEFAULT_CRITERIA
    lines = []
    for c in criteria:
        cid = c.get("id", c.get("name", ""))
        desc = c.get("description", "")
        name = c.get("name", cid) or cid
        head = name if not cid or cid == name else f"{name} [id: {cid}]"
        if desc:
            lines.append(f"  - {head}: {desc}")
        else:
            lines.append(f"  - {head}")
    return "\n".join(lines) + (
        "\n\nВ выходном JSON `criteria[].name` пиши именно человекочитаемое имя "
        "(первое поле до квадратных скобок), а не латинский id."
    )


def assess_quality(
    transcript: list[dict],
    sentiment_results: list[dict],
    protocol: str | None = None,
    custom_prompt: str | None = None,
    criteria_config: list[dict] | None = None,
    use_extended_schema: bool = False,
    prior_context: dict | None = None,
    template_driven: bool = False,
) -> dict:
    """
    Оценивает качество разговора через OpenAI GPT-5.4 API.

    Args:
        transcript: Транскрипт с метками спикеров
        sentiment_results: Результаты sentiment analysis
        protocol: Пользовательский регламент (или дефолтный)
        custom_prompt: Дополнительные инструкции для оценки
        criteria_config: Список критериев [{id, name, description}] или None для дефолтных
        use_extended_schema: Использовать расширенную V3/V4 схему с чек-листом,
                            классификацией, возражениями, client_info
        prior_context: Контекст прошлых взаимодействий для V4 оценки

    Returns:
        JSON с оценками и рекомендациями
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not set, skipping quality assessment")
        return {
            "overall_score": None,
            "error": "OPENAI_API_KEY not configured",
        }

    # Форматируем данные
    transcript_text = format_transcript_for_llm(transcript)

    # Сводка по тональности
    sentiment_summary = {}
    for r in sentiment_results:
        speaker = r.get("speaker", "UNKNOWN")
        if speaker not in sentiment_summary:
            sentiment_summary[speaker] = {"positive": 0, "negative": 0, "neutral": 0}
        s = r.get("sentiment", "neutral")
        if s in sentiment_summary[speaker]:
            sentiment_summary[speaker][s] += 1

    sentiment_json = json.dumps(sentiment_summary, ensure_ascii=False, indent=2)
    custom_instructions = custom_prompt.strip() if custom_prompt else ""
    criteria_instructions = _build_criteria_instructions(criteria_config)

    client = OpenAI(api_key=api_key)

    # Try structured output first, fallback to legacy
    try:
        result = _assess_with_structured_output(
            client, transcript_text, sentiment_json, protocol,
            custom_instructions, criteria_instructions,
            use_extended_schema=use_extended_schema,
            prior_context=prior_context,
            template_driven=template_driven,
        )
    except Exception as e:
        logger.warning(f"Structured output failed ({e}), falling back to legacy prompt")
        result = _assess_with_legacy_prompt(
            client, transcript_text, sentiment_json, protocol, custom_instructions
        )

    logger.info(f"Quality assessment complete. Overall score: {result.get('overall_score')}")
    return result


def _assess_with_structured_output(
    client, transcript_text, sentiment_json, protocol, custom_instructions,
    criteria_instructions="", use_extended_schema=False,
    prior_context=None, template_driven=False,
):
    """Assess using structured output (JSON schema) via GPT-5.4 Responses API."""
    if not criteria_instructions:
        criteria_instructions = _build_criteria_instructions(None)

    if use_extended_schema:
        # V4 is used for all extended evaluations. When the session is driven
        # by an evaluation template, swap in the template-driven prompt that
        # doesn't carry the hardcoded outbound checklist/classification/playbook
        # blocks (those would otherwise dominate the template's intent).
        v4_template = SYSTEM_PROMPT_V4_TEMPLATE if template_driven else SYSTEM_PROMPT_V4
        system = v4_template.format(
            custom_instructions=custom_instructions,
            criteria_instructions=criteria_instructions,
        )
        schema = QUALITY_JSON_SCHEMA_V4
        schema_name = "quality_assessment_v4"
        version = 4
    else:
        system = SYSTEM_PROMPT.format(
            custom_instructions=custom_instructions,
            criteria_instructions=criteria_instructions,
        )
        schema = QUALITY_JSON_SCHEMA
        schema_name = "quality_assessment"
        version = 2

    user_parts = [
        USER_PROMPT.format(
            protocol=protocol or DEFAULT_PROTOCOL,
            transcript=transcript_text,
            sentiment_summary=sentiment_json,
        )
    ]
    if prior_context is not None:
        user_parts.append("\n## Prior context\n" + json.dumps(prior_context, ensure_ascii=False, indent=2))
    user = "\n".join(user_parts)

    logger.info(f"Sending transcript to GPT-5.4 for quality assessment (structured output, v{version})...")
    response = client.responses.create(
        model="gpt-5.4",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema["schema"],
            }
        },
    )

    response_text = response.output_text
    result = json.loads(response_text)

    # Convert speaker_roles from array to dict for backward compat
    if isinstance(result.get("speaker_roles"), list):
        result["speaker_roles"] = _convert_speaker_roles(result["speaker_roles"])

    result["score_version"] = version
    return result


def _assess_with_legacy_prompt(
    client, transcript_text, sentiment_json, protocol, custom_instructions
):
    """Fallback: legacy single-prompt approach via Chat Completions."""
    prompt = ASSESSMENT_PROMPT.format(
        protocol=protocol or DEFAULT_PROTOCOL,
        transcript=transcript_text,
        sentiment_summary=sentiment_json,
        custom_instructions=custom_instructions,
    )

    logger.info("Sending transcript to GPT-5.4 for quality assessment (legacy)...")
    response = client.chat.completions.create(
        model="gpt-5.4",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.choices[0].message.content.strip()

    # Strip markdown if present
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse LLM response as JSON: {response_text[:200]}")
        result = {
            "overall_score": None,
            "error": "Failed to parse LLM response",
            "raw_response": response_text[:1000],
        }

    result["score_version"] = 2
    return result
