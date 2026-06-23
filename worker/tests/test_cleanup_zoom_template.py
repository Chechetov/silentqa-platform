"""B3 — ops-чистка Zoom-ЖК шаблона из не-complexes тенантов.

Скрипт `scripts.cleanup_zoom_template`:
- удаляет «Zoom-встреча брокера (презентация ЖК)» только у тенантов БЕЗ модуля
  complexes и только если шаблон не используется (ни сессией, ни извлечением);
- DRY-RUN по умолчанию, реальное удаление лишь с apply=True;
- идемпотентен (повторный прогон → шаблона уже нет).
Тесты — без БД: монкипатчим iter_active_tenants/tenant_connect фейками.
"""
from scripts import cleanup_zoom_template as cz
from tenancy.context import require_tenant_schema


def _make_conn(tpl_row, usage_row):
    """Фейковый conn: возвращает tpl_row на lookup по имени, usage_row на счётчик
    использования; пишет DELETE/commit в state для проверок."""
    state = {"last": "", "deleted": [], "committed": 0}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            state["last"] = sql
            if sql.strip().upper().startswith("DELETE"):
                state["deleted"].append(params)

        def fetchone(self):
            if "WHERE name" in state["last"]:
                return tpl_row
            return usage_row

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            state["committed"] += 1

        def close(self):
            pass

    return Conn(), state


def _make_stateful_conn(tpl_row, usage_row):
    """Как _make_conn, но после DELETE name-lookup начинает возвращать None —
    моделирует прод-идемпотентность (повторный прогон → шаблона уже нет)."""
    state = {"last": "", "deleted": [], "committed": 0, "present": True}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            state["last"] = sql
            if sql.strip().upper().startswith("DELETE"):
                state["deleted"].append(params)
                state["present"] = False

        def fetchone(self):
            if "WHERE name" in state["last"]:
                return tpl_row if state["present"] else None
            return usage_row

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            state["committed"] += 1

        def close(self):
            pass

    return Conn(), state


def _wire(monkeypatch, tenants, conns):
    monkeypatch.setattr(cz, "iter_active_tenants", lambda: tenants)

    def fake_connect():
        return conns[require_tenant_schema()][0]

    monkeypatch.setattr(cz, "tenant_connect", fake_connect)


def test_complexes_on_defaults_off():
    assert cz._complexes_on(None) is False
    assert cz._complexes_on({}) is False
    assert cz._complexes_on({"complexes": False}) is False
    assert cz._complexes_on({"knowledge_base": True}) is False  # другой модуль не считается
    assert cz._complexes_on({"complexes": True}) is True


def test_dry_run_does_not_delete(monkeypatch):
    tenants = [
        {"slug": "realestate", "schema_name": "t_realestate", "modules": {"complexes": True}},
        {"slug": "fulldent", "schema_name": "t_fulldent", "modules": {}},
        {"slug": "chechetov", "schema_name": "t_chechetov", "modules": {"knowledge_base": True}},
    ]
    conns = {
        "t_fulldent": _make_conn(("uuid-f",), (0, 0)),    # шаблон есть, не используется
        "t_chechetov": _make_conn(None, None),            # шаблона нет
    }
    _wire(monkeypatch, tenants, conns)

    counts = cz.cleanup(apply=False)

    assert counts["kept_complexes"] == 1   # realestate пропущен (complexes on) — до connect не дошли
    assert counts["deleted"] == 1          # fulldent: удалил бы (dry)
    assert counts["not_found"] == 1        # chechetov: шаблона нет
    assert counts["skipped_inuse"] == 0
    # DRY-RUN: реального удаления/коммита не было
    assert conns["t_fulldent"][1]["deleted"] == []
    assert conns["t_fulldent"][1]["committed"] == 0


def test_apply_deletes_and_commits_unused(monkeypatch):
    tenants = [{"slug": "fulldent", "schema_name": "t_fulldent", "modules": {}}]
    conns = {"t_fulldent": _make_conn(("uuid-f",), (0, 0))}
    _wire(monkeypatch, tenants, conns)

    counts = cz.cleanup(apply=True)

    assert counts["deleted"] == 1
    assert conns["t_fulldent"][1]["deleted"] == [("uuid-f",)]  # реальный DELETE по id
    assert conns["t_fulldent"][1]["committed"] == 1


def test_apply_skips_template_in_use(monkeypatch):
    """Даже с --apply: шаблон, на который ссылаются сессии/извлечения, НЕ удаляется."""
    tenants = [{"slug": "fulldent", "schema_name": "t_fulldent", "modules": {}}]
    conns = {"t_fulldent": _make_conn(("uuid-f",), (0, 3))}  # 3 сессии ссылаются
    _wire(monkeypatch, tenants, conns)

    counts = cz.cleanup(apply=True)

    assert counts["skipped_inuse"] == 1
    assert counts["deleted"] == 0
    assert conns["t_fulldent"][1]["deleted"] == []
    assert conns["t_fulldent"][1]["committed"] == 0


def test_apply_skips_template_in_use_via_extractions(monkeypatch):
    """Зеркало delete-guard: на шаблон ссылается ИЗВЛЕЧЕНИЕ (complex_extractions),
    а не сессия → тоже не удаляем. Пинит ОБЕ половины OR независимо."""
    tenants = [{"slug": "fulldent", "schema_name": "t_fulldent", "modules": {}}]
    conns = {"t_fulldent": _make_conn(("uuid-f",), (2, 0))}  # 2 извлечения, 0 сессий
    _wire(monkeypatch, tenants, conns)

    counts = cz.cleanup(apply=True)

    assert counts["skipped_inuse"] == 1
    assert counts["deleted"] == 0
    assert conns["t_fulldent"][1]["deleted"] == []


def test_idempotent_second_apply_is_noop(monkeypatch):
    """Повторный --apply: первый прогон удаляет, второй видит not_found и ничего
    не трогает (один DELETE суммарно)."""
    tenants = [{"slug": "fulldent", "schema_name": "t_fulldent", "modules": {}}]
    conns = {"t_fulldent": _make_stateful_conn(("uuid-f",), (0, 0))}
    _wire(monkeypatch, tenants, conns)

    c1 = cz.cleanup(apply=True)
    c2 = cz.cleanup(apply=True)

    assert c1["deleted"] == 1
    assert c2["deleted"] == 0 and c2["not_found"] == 1
    assert conns["t_fulldent"][1]["deleted"] == [("uuid-f",)]  # ровно один DELETE
    assert conns["t_fulldent"][1]["committed"] == 1


def test_complexes_tenant_never_touched(monkeypatch):
    """complexes-тенант (incl. realestate) пропускается ДО любого обращения к БД."""
    tenants = [{"slug": "realestate", "schema_name": "t_realestate", "modules": {"complexes": True}}]

    def boom():
        raise AssertionError("tenant_connect не должен вызываться для complexes-тенанта")

    monkeypatch.setattr(cz, "iter_active_tenants", lambda: tenants)
    monkeypatch.setattr(cz, "tenant_connect", boom)

    counts = cz.cleanup(apply=True)

    assert counts == {"deleted": 0, "skipped_inuse": 0, "not_found": 0, "kept_complexes": 1}
