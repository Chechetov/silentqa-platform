"""One-shot, idempotent: seed realestate's config word_boost (53 terms) into the
realestate tenant's KB 'Термины' category. Run once, post-deploy. Skips if the
category already has entries. The config word_boost remains a fallback.

  python -m scripts.seed_realestate_kb   (cwd: worker/)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root → tenancy

from tenancy.context import reset_tenant_schema, set_tenant_schema  # noqa: E402
from tenancy.db import tenant_connect  # noqa: E402

COMPANIES = Path(os.getenv("COMPANIES_PATH", Path(__file__).resolve().parents[2] / "companies"))


def main() -> None:
    cfg = json.loads((COMPANIES / "realestate.json").read_text())
    word_boost = (cfg.get("asr") or {}).get("word_boost") or []
    if not word_boost:
        print("no word_boost in realestate.json; nothing to seed")
        return

    token = set_tenant_schema("t_realestate")
    try:
        conn = tenant_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO kb_categories (name, slug, feeds_asr, feeds_llm) "
                            "VALUES ('Термины','terms',true,true) ON CONFLICT (slug) DO NOTHING")
                cur.execute("SELECT id FROM kb_categories WHERE slug='terms'")
                cat_id = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM kb_entries WHERE category_id=%s", (cat_id,))
                if cur.fetchone()[0] > 0:
                    print("'Термины' already populated; skipping (idempotent)")
                    return
                for term in word_boost:
                    cur.execute(
                        "INSERT INTO kb_entries (category_id, term) VALUES (%s, %s) "
                        "ON CONFLICT (category_id, term) DO NOTHING", (cat_id, term))
            conn.commit()
            print(f"seeded {len(word_boost)} realestate terms into KB 'Термины'")
        finally:
            conn.close()
    finally:
        reset_tenant_schema(token)


if __name__ == "__main__":
    main()
