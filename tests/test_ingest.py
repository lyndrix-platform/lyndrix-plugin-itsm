"""Shared ingest helpers: generated texts, formatting, service lookups for dedupe."""

from __future__ import annotations

import json


def test_texts_locale_fallbacks(tmp_path):
    from app.logic.texts import Texts

    (tmp_path / "itsm.en.json").write_text(
        json.dumps({"auto": {"a": "A {{x}}", "only_en": "EN"}})
    )
    (tmp_path / "itsm.de.json").write_text(json.dumps({"auto": {"a": "Ä {{ x }}"}}))
    texts = Texts("de", locales_dir=tmp_path)
    assert texts("a", x=1) == "Ä 1"
    assert texts("only_en") == "EN"  # missing in de → English
    assert texts("nope", x=1) == "nope"  # unknown → key
    assert texts("a") == "Ä {{ x }}"  # a placeholder without a value stays visible
    assert texts.set_locale("fr") is False and texts.locale == "de"
    assert texts.set_locale("en") is True and texts("a", x=2) == "A 2"


def test_shipped_locales_have_every_auto_text_in_both_languages():
    from app.logic.texts import LOCALES_DIR, Texts

    de = json.loads((LOCALES_DIR / "itsm.de.json").read_text(encoding="utf-8"))["auto"]
    en = json.loads((LOCALES_DIR / "itsm.en.json").read_text(encoding="utf-8"))["auto"]
    assert set(de) == set(en) and de
    assert Texts("en")("task_title", name="x") == "Define a maintenance window: x"


def test_formatting_helpers():
    from app.logic.ingest import fmt_duration, fmt_ts, iso_utc, parse_iso

    assert fmt_duration(45) == "45 s" and fmt_duration(600) == "10 min"
    assert fmt_duration(5400) == "1 h 30 min" and fmt_duration(90000) == "1 d 01 h"
    assert fmt_duration("12.0") == "12 s" and fmt_duration(None) == "?"
    assert fmt_ts("2026-10-18T01:23:45Z") == "2026-10-18 01:23 UTC"
    assert fmt_ts("2026-10-18T03:23:45+02:00") == "2026-10-18 01:23 UTC"
    assert fmt_ts("yesterday") == "yesterday" and fmt_ts(None) == "—"
    assert parse_iso("nope") is None and parse_iso("") is None
    assert iso_utc(parse_iso("2026-10-18T03:00:00+02:00")) == "2026-10-18T01:00:00Z"


def test_open_states_and_find_open_for_ci(svc):
    ci = "11111111-2222-3333-4444-555555555555"
    assert svc.open_state_names("incident") == ["new", "in_progress", "on_hold"]
    assert svc.open_state_names("task") == ["open", "in_progress"]
    link = [{"entity_uuid": ci, "role": "affected"}]
    assert svc.find_open_for_ci(ci, record_type="incident") is None
    manual = svc.create_record(
        {"record_type": "incident", "title": "manual", "ci_links": link}, author="m"
    )
    auto = svc.create_record(
        {
            "record_type": "incident",
            "title": "auto",
            "ci_links": link,
            "source": "monitoring",
            "external_id": f"{ci}:x",
        },
        author="monitoring",
    )
    # Most recent open record wins; the source filter picks the pushed-in one.
    assert svc.find_open_for_ci(ci, record_type="incident")["uuid"] == auto["uuid"]
    assert (
        svc.find_open_for_ci(ci, record_type="incident", source="monitoring")["uuid"]
        == auto["uuid"]
    )
    assert svc.find_open_for_ci(ci, record_type="incident", source="bmc") is None
    assert svc.find_open_for_ci(ci, record_type="task") is None
    # 'related' links do not count as "open incident for this CI".
    svc.link_ci(manual["uuid"], "99999999-9999-9999-9999-999999999999", "related")
    assert (
        svc.find_open_for_ci(
            "99999999-9999-9999-9999-999999999999", record_type="incident"
        )
        is None
    )
    # resolved is not open (closed_at is still NULL there!).
    svc.transition(
        auto["uuid"],
        "resolved",
        fields={"resolution_code": "fixed", "resolution_notes": "x"},
    )
    assert svc.find_open_for_ci(ci, record_type="incident", source="monitoring") is None
    assert svc.find_open_for_ci(ci, record_type="incident")["uuid"] == manual["uuid"]
    assert svc.find_by_external("monitoring", f"{ci}:x")["uuid"] == auto["uuid"]
    assert svc.find_by_external("monitoring", "unknown") is None


def test_duplicate_record_is_an_itsm_error(svc):
    import pytest

    from app.logic.service import DuplicateRecord, ItsmError

    svc.create_record(
        {"record_type": "incident", "title": "a", "source": "x", "external_id": "1"}
    )
    with pytest.raises(DuplicateRecord) as exc:
        svc.create_record(
            {"record_type": "incident", "title": "b", "source": "x", "external_id": "1"}
        )
    assert isinstance(exc.value, ItsmError)
