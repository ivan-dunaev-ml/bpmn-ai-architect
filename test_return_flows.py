"""Тип возврата решает форму цикла: откат, исправление или завершение."""

from pathlib import Path

from ai_generator import generate_bpmn_from_text
from process_blueprint import analyze_process, validate_correction, validate_rollback
from test_process_blueprint import _normalize_linear


def _work_titles(diagram):
    kinds = {"task", "userTask", "scriptTask", "manualTask", "serviceTask"}
    return [node.name for node in diagram.nodes.values() if node.kind in kinds]


def test_rollback_detection_and_no_new_task():
    text = (
        "Регламент: Откат на пункт\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет документы (1 час). Если документы неполные — вернуть на п.1.\n"
    )
    diagram, blueprint, _before = _normalize_linear(text)
    assert len(blueprint.reworks) == 1
    item = blueprint.reworks[0]
    assert item.type == "rollback"
    assert item.target_num == 1
    assert item.fix_task == ""
    assert validate_rollback(diagram, blueprint) == []
    assert "Устранить" not in " ".join(_work_titles(diagram))
    assert "Исправить" not in " ".join(_work_titles(diagram))


def test_correction_detection_creates_fix_task():
    text = (
        "Регламент: Доработка\n"
        "1. Исполнитель готовит документ (1 час).\n"
        "2. Руководитель проверяет документ (1 час). "
        "При ошибках направить на доработку. После исправления повторно проверить.\n"
    )
    diagram, blueprint, before = _normalize_linear(text)
    assert len(blueprint.reworks) == 1
    item = blueprint.reworks[0]
    assert item.type == "correction"
    assert item.fix_task
    assert validate_correction(diagram, blueprint) == []
    titles = _work_titles(diagram)
    assert item.fix_task in titles
    assert len(titles) == len(before) + 1
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert len(audit["rework_loops"]) == 1


def test_termination_is_not_a_cycle():
    text = (
        "Регламент: Отказ\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет договор (1 час). Если договор не подписан — отказ заявителя. "
        "Завершить процесс.\n"
        "3. Центр выдаёт документы (1 час).\n"
    )
    blueprint = analyze_process(text)
    assert blueprint.reworks == []
    assert any(item.kind == "end" for item in blueprint.decisions)
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert audit["rework_loops"] == []


def test_equipment_handoff_is_not_a_return():
    text = (
        "Регламент: Склад\n"
        "1. Бригада выполняет ремонт (1 час).\n"
        "2. Бригада вернуть оборудование на склад (30 минут).\n"
        "3. Диспетчер закрывает наряд (15 минут).\n"
    )
    blueprint = analyze_process(text)
    assert blueprint.reworks == []
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert audit["rework_loops"] == []


def test_grid_connection_rollbacks_do_not_invent_tasks():
    text = Path("examples/example_3_grid_connection.txt").read_text()
    blueprint = analyze_process(text)
    assert [item.type for item in blueprint.reworks] == ["rollback", "rollback"]
    assert [item.target_num for item in blueprint.reworks] == [1, 11]
    assert all(item.fix_task == "" for item in blueprint.reworks)
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert len(audit["rework_loops"]) == 2
    assert audit["sla"]["critical_path_hours"] == 400.3
    assert audit["sla"]["with_rework_hours"] == 584.4
    joined = " ".join(loop["from"] + " " + loop["to"] for loop in audit["rework_loops"])
    assert "Устранить" not in joined
    assert "заявк" in joined.lower()
