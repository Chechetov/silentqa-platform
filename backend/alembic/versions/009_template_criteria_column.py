"""Add criteria JSONB column to extraction_templates and backfill seeded templates.

When an evaluation template overrides the scenario, the LLM should also score
on template-specific criterion names, not the generic DEFAULT_CRITERIA fallback.
This migration adds a criteria column and populates it for both seeded templates.

Revision ID: 009
Revises: 008
Create Date: 2026-05-07
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Outbound call evaluation — criteria mirror companies/realestate.json
# scenario `outbound_residential` (8 items).
OUTBOUND_CRITERIA = [
    {"id": "greeting_quality", "name": "Качество приветствия",
     "description": "Приветствие по имени, представление, упоминание ЖК, базовая информация по проекту"},
    {"id": "initiative", "name": "Инициативность",
     "description": "Перехват инициативы, ведение разговора, удержание клиента в звонке"},
    {"id": "needs_discovery", "name": "Снятие запроса",
     "description": "Глубина выяснения потребностей: цель, локации, формат, сроки, бюджет, доп. вопросы"},
    {"id": "proposal_quality", "name": "Качество предложения",
     "description": "Конкретные ЖК, релевантность запросу, аргументация почему подходят"},
    {"id": "meeting_effort", "name": "Работа на встречу",
     "description": "Аргументация пользы встречи, мин. 2 попытки, объяснение выгоды работы с брокером"},
    {"id": "objection_handling", "name": "Отработка возражений",
     "description": "Реакция на возражения клиента, качество аргументов, попытка продолжить разговор"},
    {"id": "communication", "name": "Коммуникация",
     "description": "Вежливость, активное слушание, позитивный настрой, не перебивает, имя клиента 3+ раз"},
    {"id": "script_adherence", "name": "Следование скрипту",
     "description": "Последовательность этапов, отсутствие слов неуверенности, фиксация договорённостей"},
]


# Zoom-meeting evaluation — one criterion per step of the broker's Zoom playbook
# (11 items, mirrors the protocol seeded by migration 008).
ZOOM_CRITERIA = [
    {"id": "preparation", "name": "Подготовка",
     "description": "Брокер до начала встречи проверил соединение, звук, видео, демонстрацию экрана; маркер «связь проверена»"},
    {"id": "greeting", "name": "Приветствие",
     "description": "Тёплое приветствие по имени с упоминанием встречи («рад/рада вас видеть на встрече»)"},
    {"id": "connection_check", "name": "Проверка связи с клиентом",
     "description": "«Хорошо ли видно/слышно?», «Демонстрация экрана видна?» — обязательно перед началом содержательной части"},
    {"id": "agenda", "name": "Старт встречи",
     "description": "Согласована длительность встречи; ответы на стартовые вопросы клиента; рассказан маршрут (как пройдёт встреча)"},
    {"id": "needs_recap", "name": "Резюмирование запроса",
     "description": "Подтверждены параметры: локация, бюджет, площадь, спальни, этаж, форма оплаты, ключевые критерии клиента"},
    {"id": "presentation_quality", "name": "Качество презентации",
     "description": "Карта/локация/план развития/окружение/панорама/транспорт; застройщик; архитектура и наполнение дома; инженерия; планировки со сценарием жизни — глубина и полнота"},
    {"id": "purchase_terms", "name": "Условия покупки",
     "description": "Озвучены конкретные условия: ипотека от X%, рассрочка до X месяцев, скидка X% при 100% оплате"},
    {"id": "feedback_loop", "name": "Обратная связь по каждому ЖК",
     "description": "После КАЖДОГО показанного ЖК: «что понравилось», «насколько подходит», «переходим?» — не только в конце встречи"},
    {"id": "objection_handling", "name": "Работа с возражениями",
     "description": "Возражения клиента зафиксированы и отработаны, а не проигнорированы"},
    {"id": "closing", "name": "Подведение итогов и договорённости",
     "description": "Собраны впечатления клиента; есть договор о визите в офис застройщика ИЛИ конкретная дата/время следующего контакта"},
    {"id": "communication", "name": "Коммуникация",
     "description": "Вежливость, активное слушание, ясность, имя клиента используется регулярно"},
]


def upgrade() -> None:
    op.add_column(
        "extraction_templates",
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        "UPDATE extraction_templates SET criteria = :c::jsonb "
        "WHERE name = 'Звонок брокера (исходящий по жилой)'"
        .replace(":c", "$$" + json.dumps(OUTBOUND_CRITERIA, ensure_ascii=False) + "$$")
    )
    op.execute(
        "UPDATE extraction_templates SET criteria = :c::jsonb "
        "WHERE name = 'Zoom-встреча брокера (презентация ЖК)'"
        .replace(":c", "$$" + json.dumps(ZOOM_CRITERIA, ensure_ascii=False) + "$$")
    )


def downgrade() -> None:
    op.drop_column("extraction_templates", "criteria")
