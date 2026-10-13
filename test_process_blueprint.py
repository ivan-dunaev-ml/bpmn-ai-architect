"""Чертёж процесса: возврат становится циклом, отказ и передача оборудования — нет."""

from ai_generator import (
    ROOT_END_TASK_ID,
    ROOT_PROCESS_ID,
    ROOT_START_TASK_ID,
    build_prompt,
    execute_generated_code,
    generate_bpmn_from_text,
)
from bpmn_framework import BPMNDiagramBuilder
from process_blueprint import analyze_process, normalize_reworks, validate_reworks


def _loops(text: str) -> int:
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    return len(audit["rework_loops"])


def test_return_to_step_is_rework():
    text = (
        "Регламент: Вернуть на пункт\n"
        "1. Исполнитель готовит заявку (10 минут).\n"
        "2. Руководитель проверяет заявку (15 минут). Если замечания — вернуть на п.1.\n"
    )
    blueprint = analyze_process(text)
    assert len(blueprint.reworks) == 1
    assert _loops(text) == 1


def test_send_to_rework_is_rework():
    text = (
        "Регламент: Направить на доработку\n"
        "1. Исполнитель готовит заявку (10 минут).\n"
        "2. Руководитель проверяет заявку (15 минут). При замечаниях направить на доработку.\n"
    )
    blueprint = analyze_process(text)
    assert len(blueprint.reworks) == 1
    assert blueprint.reworks[0].condition == "Замечания"
    assert _loops(text) == 1


def test_fix_remarks_is_rework():
    text = (
        "Регламент: Устранить замечания\n"
        "1. Исполнитель готовит заявку (10 минут).\n"
        "2. Руководитель проверяет заявку (15 минут). При замечаниях устранить замечания.\n"
    )
    blueprint = analyze_process(text)
    assert len(blueprint.reworks) == 1
    assert _loops(text) == 1


def test_applicant_refusal_is_not_rework():
    text = (
        "Регламент: Отказ заявителя\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет заявку (1 час). Если документы полные — перейти к п.3, "
        "иначе «отказ заявителя» — завершить процесс.\n"
        "3. Центр выдаёт договор (1 час).\n"
    )
    blueprint = analyze_process(text)
    assert blueprint.reworks == []
    assert any(item.kind == "end" for item in blueprint.decisions)
    assert _loops(text) == 0


def test_return_equipment_is_not_rework():
    text = (
        "Регламент: Вернуть оборудование\n"
        "1. Бригада выполняет ремонт линии (2 часа).\n"
        "2. Бригада вернуть оборудование на склад (30 минут).\n"
        "3. Диспетчер закрывает наряд (15 минут).\n"
    )
    blueprint = analyze_process(text)
    assert blueprint.reworks == []
    assert _loops(text) == 0


def test_blueprint_goes_to_the_prompt():
    text = (
        "Регламент: Вернуть на пункт\n"
        "1. Исполнитель готовит заявку (10 минут).\n"
        "2. Руководитель проверяет заявку (15 минут). Если замечания — вернуть на п.1.\n"
    )
    prompt = build_prompt(text)
    assert "PROCESS BLUEPRINT" in prompt
    assert '"Reworks"' in prompt
    assert "Не ищи возвраты" in prompt
    assert "секрет" not in prompt or "вернуть на п.1" in prompt


def _linear_code(text: str) -> str:
    blueprint = analyze_process(text)
    titles = ",\n".join(
        f'    "{task.title}"' for task in blueprint.tasks
    )
    return f"""
titles = [
{titles}
]
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Исполнитель"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
prev = ROOT_START_TASK_ID
for title in titles:
    node = DIAGRAM.add_user_task(title, lanes[0])
    DIAGRAM.set_sla(node, 8)
    DIAGRAM.add_link(prev, node)
    prev = node
DIAGRAM.add_link(prev, ROOT_END_TASK_ID)
"""


def _normalize_linear(text: str):
    blueprint = analyze_process(text)
    diagram = BPMNDiagramBuilder(process_id=ROOT_PROCESS_ID)
    _pool, lanes = diagram.add_pool(ROOT_PROCESS_ID, ["Исполнитель"])
    diagram.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
    diagram.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
    previous = ROOT_START_TASK_ID
    originals = []
    for task in blueprint.tasks:
        node_id = diagram.add_user_task(task.title, lanes[0])
        diagram.set_sla(node_id, 8)
        diagram.add_link(previous, node_id)
        previous = node_id
        originals.append(node_id)
    diagram.add_link(previous, ROOT_END_TASK_ID)
    before = {
        node_id: (
            diagram.nodes[node_id].name,
            diagram.nodes[node_id].lane_id,
            diagram.nodes[node_id].sla_hours,
            diagram.nodes[node_id].sla_source,
        )
        for node_id in originals
    }
    normalize_reworks(diagram, text)
    return diagram, blueprint, before


def test_return_to_point_1_becomes_a_cycle():
    text = (
        "Регламент: Вернуть на п.1\n"
        "1. Исполнитель готовит заявку (1 час).\n"
        "2. Руководитель проверяет заявку (1 час). Если замечания — вернуть на п.1.\n"
        "3. Руководитель выдаёт договор (1 час).\n"
    )
    diagram, blueprint, before = _normalize_linear(text)
    assert blueprint.reworks[0].target_num == 1
    assert validate_reworks(diagram, blueprint) == []
    for node_id, signature in before.items():
        node = diagram.nodes[node_id]
        assert (node.name, node.lane_id, node.sla_hours, node.sla_source) == signature
    _, audit, err = execute_generated_code(_linear_code(text), regulation_text=text)
    assert not err, err
    assert len(audit["rework_loops"]) == 1
    assert "заяв" in audit["rework_loops"][0]["to"].lower()


def test_return_to_point_11_becomes_a_cycle():
    steps = [f"{num}. Исполнитель выполняет работу {num} (1 час)." for num in range(1, 11)]
    text = "\n".join([
        "Регламент: Вернуть на п.11",
        *steps,
        "11. Заявитель выполняет мероприятия на участке (1 час).",
        "12. Служба проводит осмотр устройств (1 час). "
        "Если замечаний нет — перейти к п.13, иначе «замечания к ТУ» — вернуть на п.11.",
        "13. Служба подключает объект (1 час).",
    ])
    blueprint = analyze_process(text)
    assert len(blueprint.reworks) == 1
    assert blueprint.reworks[0].target_num == 11
    assert blueprint.reworks[0].check_num == 12
    diagram, _blueprint, before = _normalize_linear(text)
    assert validate_reworks(diagram, blueprint) == []
    for node_id, signature in before.items():
        node = diagram.nodes[node_id]
        assert (node.name, node.lane_id, node.sla_hours, node.sla_source) == signature
    gates = [node for node in diagram.nodes.values() if node.kind == "exclusiveGateway"]
    assert gates
    assert any((link.condition_name or "") for link in diagram.links if link.source_id == gates[-1].id)
    _, audit, err = execute_generated_code(_linear_code(text), regulation_text=text)
    assert not err, err
    assert len(audit["rework_loops"]) == 1


def test_refusal_and_equipment_are_not_normalized_into_a_cycle():
    refusal = (
        "Регламент: Отказ заявителя\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет заявку (1 час). Если документы полные — перейти к п.3, "
        "иначе «отказ заявителя» — завершить процесс.\n"
        "3. Центр выдаёт договор (1 час).\n"
    )
    equipment = (
        "Регламент: Вернуть оборудование\n"
        "1. Бригада выполняет ремонт линии (2 часа).\n"
        "2. Бригада вернуть оборудование на склад (30 минут).\n"
        "3. Диспетчер закрывает наряд (15 минут).\n"
    )
    for text in (refusal, equipment):
        diagram, blueprint, before = _normalize_linear(text)
        assert blueprint.reworks == []
        assert normalize_reworks(diagram, text) == 0
        assert len(diagram.nodes) == len(before) + 2
        _, audit, err = execute_generated_code(_linear_code(text), regulation_text=text)
        assert not err, err
        assert audit["rework_loops"] == []


def test_repair_adds_only_the_back_edge():
    text = (
        "Регламент: Вернуть на пункт\n"
        "1. Исполнитель готовит заявку (10 минут).\n"
        "2. Руководитель проверяет заявку (15 минут). Если замечания — вернуть на п.1.\n"
    )
    code = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Исполнитель", "Руководитель"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Готовить заявку", lanes[0])
b = DIAGRAM.add_user_task("Проверить заявку", lanes[1])
g = DIAGRAM.add_exclusive_gateway("Заявка принята?", lanes[1])
c = DIAGRAM.add_user_task("Исправить заявку", lanes[0])
d = DIAGRAM.add_user_task("Выдать договор", lanes[1])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, g)
DIAGRAM.add_link(g, d, "Согласовано")
DIAGRAM.add_link(g, c, "Замечания")
DIAGRAM.add_link(c, d)
DIAGRAM.add_link(d, ROOT_END_TASK_ID)
"""
    _, audit, err = execute_generated_code(code, regulation_text=text)
    assert not err, err
    assert len(audit["rework_loops"]) == 1
