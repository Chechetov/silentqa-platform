"""Add extraction_templates, complex_extractions, complexes tables

Revision ID: 004
Revises: 003
Create Date: 2026-04-27
"""
import json
import uuid
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRESENTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name":      {"type": ["string", "null"], "description": "Название ЖК"},
        "developer": {"type": ["string", "null"], "description": "Название застройщика"},
        "class":     {"type": ["string", "null"], "description": "эконом / комфорт / комфорт+ / бизнес / премиум / элит"},
        "location": {
            "type": "object",
            "properties": {
                "district":     {"type": ["string", "null"]},
                "address":      {"type": ["string", "null"]},
                "parks":        {"type": "array", "items": {"type": "string"}},
                "embankments":  {"type": "array", "items": {"type": "string"}},
                "transport":    {"type": "array", "items": {"type": "string"}, "description": "Метро/автобусы/дороги, время до точек"},
                "malls":        {"type": "array", "items": {"type": "string"}},
                "venues":       {"type": "array", "items": {"type": "string"}, "description": "Театры, рестораны и т.п."},
                "future_plans": {"type": ["string", "null"], "description": "Что будет в районе в будущем"}
            }
        },
        "architecture": {
            "type": "object",
            "properties": {
                "style":        {"type": ["string", "null"], "description": "модернизм/неоклассика/ар-деко/бионический и т.п."},
                "materials":    {"type": "array", "items": {"type": "string"}, "description": "натуральный камень, клинкер, медные панели и т.п."},
                "phases":       {"type": ["integer", "null"], "description": "Количество очередей"},
                "buildings":    {"type": ["integer", "null"], "description": "Количество корпусов"},
                "floors":       {"type": "array", "items": {"type": "string"}, "description": "Этажности корпусов с пояснениями"},
                "layouts_note": {"type": ["string", "null"], "description": "Описание выбора планировок"}
            }
        },
        "amenities": {
            "type": "object",
            "properties": {
                "lobby":            {"type": ["boolean", "null"]},
                "concierge":        {"type": ["boolean", "null"]},
                "meeting_rooms":    {"type": ["boolean", "null"]},
                "coworking":        {"type": ["boolean", "null"]},
                "guest_entrance":   {"type": ["boolean", "null"]},
                "observation_deck": {"type": ["boolean", "null"]},
                "fitness":          {"type": ["boolean", "null"]},
                "parking":          {"type": ["string", "null"]},
                "engineering":      {"type": "array", "items": {"type": "string"}, "description": "VRV, фанкойл, очистка воздуха/воды и т.п."},
                "yard": {
                    "type": "object",
                    "properties": {
                        "area_ha": {"type": ["number", "null"]},
                        "zones":   {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        },
        "delivery": {
            "type": "object",
            "properties": {
                "overall_year":    {"type": ["integer", "null"]},
                "overall_quarter": {"type": ["integer", "null"]},
                "by_phase": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase":   {"type": ["string", "null"]},
                            "year":    {"type": ["integer", "null"]},
                            "quarter": {"type": ["integer", "null"]}
                        }
                    }
                }
            }
        },
        "finishes": {
            "type": "object",
            "properties": {
                "options":       {"type": "array", "items": {"type": "string"}, "description": "без / white box / чистовая / дизайнерская"},
                "design_styles": {"type": "array", "items": {"type": "string"}}
            }
        },
        "pricing": {
            "type": "object",
            "properties": {
                "cash":        {"type": ["string", "null"], "description": "Цена при 100% оплате"},
                "mortgage":    {"type": ["string", "null"], "description": "Условия по ипотеке"},
                "installment": {"type": ["string", "null"], "description": "Условия по рассрочке"},
                "min_price":   {"type": ["number", "null"]},
                "currency":    {"type": ["string", "null"]}
            }
        },
        "additional_info": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Любые факты, не вошедшие в стандартные поля выше"
        }
    },
    "required": ["name", "developer", "additional_info"]
}

PRESENTATION_PROMPT = """Ты извлекаешь структурированные факты о жилом или многофункциональном комплексе из транскрипта презентации. Презентует обычно представитель застройщика (девелопера) или брокер.

ВАЖНО — постарайся обязательно вытащить:
- name: НАЗВАНИЕ комплекса (обычно произносится в начале — «ЖК ...», «комплекс ...», «проект ...» — может быть на английском или русском). Если оно проскальзывает в любой части записи, бери его.
- developer: НАЗВАНИЕ компании-застройщика (компания, которая строит и продаёт). Часто говорят «мы (как девелопер)», «наша компания», «реализуем проект», и где-то рядом — название. Также упоминаются юр. формы (ООО, ГК, Group) и частые контексты: основатели, акционеры, портфель проектов. Если название упоминается ХОТЯ БЫ ОДИН раз — обязательно зафиксируй.
- class: эконом / комфорт / комфорт+ / бизнес / премиум / элит — часто проговаривают в начале как «бизнес-класс», «премиум».

Для остальных полей: заполняй только тем, что прямо упомянуто. Если факт не упомянут — null (для скаляров) или [] (для массивов). НЕ выдумывай и не додумывай.

В additional_info собери всё значимое, что не попало в стандартные поля: сделки, скидки, особенности, имена менеджеров, конкретные цифры площадей или цен, и т.п."""


def _strictify(schema):
    """Make the schema OpenAI-strict-compliant: every object gets
    additionalProperties=false and required = list of all its property keys.
    Recurses through `properties` and `items`.
    """
    if not isinstance(schema, dict):
        return schema
    s = dict(schema)
    if s.get("type") == "object" or "properties" in s:
        s["additionalProperties"] = False
        props = {k: _strictify(v) for k, v in s.get("properties", {}).items()}
        s["properties"] = props
        s["required"] = list(props.keys())
    if "items" in s:
        s["items"] = _strictify(s["items"])
    return s


PRESENTATION_SCHEMA = _strictify(PRESENTATION_SCHEMA)


def upgrade() -> None:
    op.execute("""CREATE TABLE extraction_templates (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(200) NOT NULL UNIQUE,
        description TEXT,
        kind VARCHAR(20) NOT NULL,
        prompt TEXT NOT NULL,
        json_schema JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    op.execute("""CREATE TABLE complexes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name VARCHAR(300) NOT NULL,
        name_normalized VARCHAR(300) NOT NULL,
        developer VARCHAR(200),
        developer_normalized VARCHAR(200) NOT NULL DEFAULT '',
        class VARCHAR(50),
        district VARCHAR(200),
        aggregated_data JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE UNIQUE INDEX idx_complexes_norm ON complexes(name_normalized, developer_normalized)")
    op.execute("CREATE INDEX idx_complexes_developer ON complexes(developer)")
    op.execute("CREATE INDEX idx_complexes_class ON complexes(class)")

    op.execute("""CREATE TABLE complex_extractions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        template_id UUID NOT NULL REFERENCES extraction_templates(id) ON DELETE RESTRICT,
        complex_id UUID REFERENCES complexes(id) ON DELETE SET NULL,
        raw_data JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX idx_extractions_session ON complex_extractions(session_id)")
    op.execute("CREATE INDEX idx_extractions_complex ON complex_extractions(complex_id)")

    # Seed «Презентация ЖК»
    op.execute(f"""INSERT INTO extraction_templates (name, description, kind, prompt, json_schema)
        VALUES (
            'Презентация ЖК',
            'Извлечение структурированных данных из презентации жилого комплекса (застройщик или брокер презентует ЖК).',
            'extraction',
            $${PRESENTATION_PROMPT}$$,
            $${json.dumps(PRESENTATION_SCHEMA, ensure_ascii=False)}$$::jsonb
        )""")


def downgrade() -> None:
    op.drop_table("complex_extractions")
    op.drop_index("idx_complexes_class")
    op.drop_index("idx_complexes_developer")
    op.drop_index("idx_complexes_norm")
    op.drop_table("complexes")
    op.drop_table("extraction_templates")
