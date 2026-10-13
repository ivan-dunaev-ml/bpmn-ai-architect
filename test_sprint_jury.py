"""Спринт к чекпоинту: события отказа, артефакты по флажку, селектор движка, Guardrails, To-Be. Без сети."""

import os
import re
import tempfile
from pathlib import Path

import pytest

import ai_generator as gen
import llm_providers
from bpmn_artifacts import with_data_artifacts
from validate_bpmn import validate, xsd_errors_xml

ROOT = Path(__file__).resolve().parent
EX1 = (ROOT / "examples" / "example_1_substation_repair.txt").read_text(encoding="utf-8")
EX2 = (ROOT / "examples" / "example_2_equipment_procurement.txt").read_text(encoding="utf-8")

SUBSTATION = """Регламент: Модернизация подстанции 110 кВ
Целевой срок: 300 часов

1. Главный инженер формирует программу модернизации (16 часов).
2. Служба безопасности проверяет подрядчика (40 часов). Если подрядчик неблагонадёжен — завершить процесс.
3. Отдел закупок заказывает оборудование (80 часов).
4. Технический комитет проверяет оборудование при приёмке (24 часа). Если обнаружен брак — завершить процесс.
5. Ремонтная бригада монтирует новое оборудование (48 часов).
6. Главный инженер подписывает акт приёмки (4 часа).
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("BPMN_AI_MODE", "emulator")


def _validate_xml(xml: str) -> list:
    with tempfile.NamedTemporaryFile("w", suffix=".bpmn", delete=False, encoding="utf-8") as fh:
        fh.write(xml)
    try:
        return validate(fh.name)
    finally:
        os.unlink(fh.name)


def _diagonals(xml: str) -> int:
    count = 0
    for edge in re.findall(r"<bpmndi:BPMNEdge.*?</bpmndi:BPMNEdge>", xml, re.S):
        pts = [(float(x), float(y)) for x, y in re.findall(r'<di:waypoint x="([\d.]+)" y="([\d.]+)"', edge)]
        count += sum(1 for (a, b), (c, d) in zip(pts, pts[1:]) if abs(a - c) > 0.5 and abs(b - d) > 0.5)
    return count


def test_rejection_branch_ends_in_terminate_event():
    xml, audit, err = gen.generate_bpmn_from_text(SUBSTATION, use_llm=False)
    assert not err, err
    assert xml.count("<bpmn:terminateEventDefinition") == 2  # отказ СБ и брак
    assert 'id="Event_RootEnd"' in xml  # успешный финал — по-прежнему ROOT_END_TASK_ID
    assert xsd_errors_xml(xml) == []
    assert _validate_xml(xml) == []
    assert _diagonals(xml) == 0
    # Событие останова правее шлюза, в его дорожке.
    for end_id in re.findall(r'<bpmn:endEvent id="([^"]+)"[^>]*>(?:(?!</bpmn:endEvent>).)*terminateEventDefinition', xml, re.S):
        gate = re.search(rf'sourceRef="([^"]+)" targetRef="{end_id}"', xml).group(1)
        gx = float(re.search(rf'bpmnElement="{gate}"[^>]*>\s*<dc:Bounds x="([\d.]+)"', xml).group(1))
        ex = float(re.search(rf'bpmnElement="{end_id}"[^>]*>\s*<dc:Bounds x="([\d.]+)"', xml).group(1))
        assert ex > gx


def test_final_step_termination_stays_root_end():
    # «Если согласовано — завершить процесс» на последнем шаге — это успешный финал, не отказ.
    xml, _audit, err = gen.generate_bpmn_from_text(
        "Регламент: Финал\n1. Инициатор готовит заявку (1 час).\n"
        "2. Руководитель согласовывает заявку (1 час). Если согласовано — завершить процесс, иначе вернуть на п.1.\n",
        use_llm=False,
    )
    assert not err
    assert "terminateEventDefinition" not in xml


def test_artifacts_off_by_default_and_clean_when_on():
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    assert "dataObjectReference" not in xml  # основной вид чистый
    layered = with_data_artifacts(xml, EX1, audit)
    assert "<bpmn:dataObjectReference" in layered and "<bpmn:dataStoreReference" in layered
    assert layered.count("<bpmn:dataObjectReference") + layered.count("<bpmn:dataStoreReference") <= 6
    assert xsd_errors_xml(layered) == []
    assert _validate_xml(layered) == []  # потоки не пересекают объекты, у связей нет диагоналей
    assert _diagonals(layered) == 0
    assert with_data_artifacts("<не xml>", EX1, audit) == "<не xml>"  # сбой слоя не ломает схему


def test_session_engine_choice_does_not_leak():
    assert llm_providers.mode() == "emulator"  # BPMN_AI_MODE главнее
    os.environ.pop("BPMN_AI_MODE")
    try:
        with llm_providers.session_mode("yandex"):
            assert llm_providers.mode() == "yandex"
        assert llm_providers.mode() == "auto"
    finally:
        os.environ["BPMN_AI_MODE"] = "emulator"


def test_document_is_not_a_lane_and_verbs_are_perfective():
    parsed = gen.parse_regulation(gen.normalize_regulation(
        "Регламент: Договор\n1. Юридическая служба согласовывает договор (8 часов).\n"
        "2. Руководитель подписывает договор (2 часа).\n3. Договор вступает в силу после регистрации (1 час).\n"
        "4. Руководитель утверждает реестр (1 час).\n"
    ))
    assert "Договор" not in parsed.roles
    assert parsed.steps[0].title == "Согласовать договор"
    assert parsed.steps[3].title.startswith("Утвердить")


def test_edo_fallback_builds_tobe_when_moves_fail():
    text = (
        "Регламент: Согласование сметы\n1. Финансовый департамент готовит смету (8 часов).\n"
        "2. Финансовый департамент согласовывает смету с подразделениями (80 часов).\n"
    )
    xml, audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not err
    _opt, delta = gen.optimize_process_to_be(text, audit)
    assert delta["tobe_xml"]
    assert delta["sla_after_hours"] < delta["sla_before_hours"]
    edo = [a for a in delta["actions"] if a.get("type") == "edo_approval" or a.get("kind") == "edo_approval"]
    assert edo and "допущение" in edo[0]["detail"] and "80 ч" in edo[0]["detail"] and "24 ч" in edo[0]["detail"]


def test_rejection_reason_shows_minutes_not_equal_hours():
    reason = gen._tobe_known_block({"cp": 380.3, "rw": 678.5, "loops": 2, "q": 100}, {"cp": 380.38, "rw": 678.6, "loops": 2, "q": 100})
    assert "380 ч → 380 ч" not in reason and "мин" in reason


def test_ui_helpers_dedupe_and_hide_tracebacks():
    import app

    delta = {"rejected": [{"reason": "Голый путь To-Be длиннее: 102 ч → 198 ч.", "detail": "Шаги 8 и 9 параллельно"}] * 7}
    note = app._rejected_move_note(delta)
    assert note.count("Голый путь") == 1
    safe = app.jury_safe_line(
        "openai, попытка 1: результат отклонён — Ошибка выполнения кода: TypeError: "
        "add_user_task() takes 3 positional arguments but 4 were given"
    )
    assert "TypeError" not in safe and "неверными аргументами" in safe
    assert app.ENGINE_CHOICES[app.ENGINE_OFFLINE] == ("emulator", False)
    assert app.ENGINE_CHOICES[app.ENGINE_YANDEX][0] == "yandex"


def test_prompt_knows_terminate_and_lane_rule():
    prompt = gen.build_prompt(EX2)
    assert "add_terminate_end_event" in prompt
    assert "Документы, договоры" in prompt


def test_vanya_223fz_conditional_actions_and_named_return():
    """Стресс-тест Вани (223-ФЗ): «Если X — сделать Y» и «возврат на повторную переторжку» не теряются."""
    from process_blueprint import analyze_process

    text = (ROOT / "stress_tests" / "10_vanya_223fz_turbines.txt").read_text(encoding="utf-8")
    blueprint = analyze_process(text)
    actions = {d.num: (d.yes_action, d.yes_action_stop) for d in blueprint.decisions if d.yes_action}
    assert actions[4] == ("Отклонить заявку и опубликовать протокол отказа", True)
    assert actions[5][1] is False  # требование обеспечения — мера, процесс продолжается
    assert actions[10] == ("Наложить вето и прекратить торги", True)
    assert actions[11][1] is True  # «не подписал» — отказная ветка
    assert [(r.check_num, r.target_num) for r in blueprint.reworks] == [(8, 7)]
    xml, audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not err
    assert len(audit["rework_loops"]) == 1
    assert xml.count("terminateEventDefinition") == 3
    assert xsd_errors_xml(xml) == [] and _validate_xml(xml) == [] and _diagonals(xml) == 0
    _opt, delta = gen.optimize_process_to_be(text, audit)
    assert delta["sla_after_hours"] <= delta["sla_before_hours"]  # параллель не удваивает путь
