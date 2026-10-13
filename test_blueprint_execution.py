"""Генератор исполняет чертёж закупки и не читает регламент второй раз."""

from pathlib import Path

from ai_generator import (
    ROOT_END_TASK_ID,
    ROOT_PROCESS_ID,
    ROOT_START_TASK_ID,
    bpmn_presentation_quality,
    emulate_generation,
    execute_generated_code,
    generate_bpmn_from_text,
    process_problem_lines,
)
from bpmn_framework import BPMNDiagramBuilder
from process_blueprint import (
    FlowType,
    analyze_process,
    compile_blueprint,
    execution_gaps,
    xml_contract_gaps,
)

PROCUREMENT = (
    Path(__file__).resolve().parent / "examples" / "example_2_equipment_procurement.txt"
).read_text(encoding="utf-8")


def _diagram(code: str) -> BPMNDiagramBuilder:
    diagram = BPMNDiagramBuilder(
        process_name="Закупка",
        process_id=ROOT_PROCESS_ID,
        root_start_id=ROOT_START_TASK_ID,
        root_end_id=ROOT_END_TASK_ID,
    )
    namespace = {
        "DIAGRAM": diagram,
        "ROOT_PROCESS_ID": ROOT_PROCESS_ID,
        "ROOT_START_TASK_ID": ROOT_START_TASK_ID,
        "ROOT_END_TASK_ID": ROOT_END_TASK_ID,
    }
    exec(code, namespace)
    return diagram


def test_procurement_blueprint_is_executed():
    blueprint = analyze_process(PROCUREMENT)
    assert len(blueprint.tasks) == 13
    assert [item.type for item in blueprint.reworks] == [FlowType.ROLLBACK, FlowType.ROLLBACK]
    assert all(item.fix_task == "" for item in blueprint.reworks)
    assert [item.target_num for item in blueprint.reworks] == [2, 4]
    assert sum(1 for item in blueprint.decisions if item.flow == FlowType.TERMINATION) == 1
    assert len(blueprint.parallel_blocks) == 1
    assert blueprint.parallel_blocks[0].nums == [9, 10]

    code, _info = compile_blueprint(blueprint)
    same_code, _same_info = emulate_generation(PROCUREMENT)
    assert code == same_code

    gaps = execution_gaps(_diagram(code), blueprint)
    assert gaps == [], gaps

    _xml, audit, err = execute_generated_code(
        code, regulation_text=PROCUREMENT, blueprint=blueprint,
    )
    assert not err, err
    assert audit["stats"]["work_items"] == 13
    assert len(audit["rework_loops"]) == 2
    assert audit["sla"]["critical_path_hours"] == 528.3
    assert audit["sla"]["with_rework_hours"] == 856.5
    assert audit.get("xsd_valid") is True
    assert xml_contract_gaps(_xml, blueprint) == []
    assert all(task.id == f"T{task.num}" for task in blueprint.tasks)
    assert blueprint.parallel_blocks[0].flow_type == FlowType.PARALLEL


def test_flow_priority_explicit_reference_first():
    rollback = analyze_process(
        "Регламент: Явная ссылка\n"
        "1. Исполнитель готовит пакет (1 час).\n"
        "2. Руководитель проверяет пакет (1 час). "
        "Если ошибка — повторная диагностика, вернуть на п.1, иначе завершить процесс.\n"
    )
    assert [item.type for item in rollback.reworks] == [FlowType.ROLLBACK]
    assert rollback.reworks[0].target_num == 1
    assert rollback.reworks[0].fix_task == ""
    assert not any(item.flow == FlowType.TERMINATION for item in rollback.decisions)

    repeat = analyze_process(
        "Регламент: Повторить\n"
        "1. Исполнитель готовит пакет (1 час).\n"
        "2. Руководитель проверяет пакет (1 час). "
        "Если сомнение — повторная диагностика, иначе завершить процесс.\n"
    )
    assert [item.type for item in repeat.reworks] == [FlowType.CORRECTION]
    assert not any(item.flow == FlowType.TERMINATION for item in repeat.decisions)

    rework = analyze_process(
        "Регламент: Доработать\n"
        "1. Исполнитель готовит пакет (1 час).\n"
        "2. Руководитель проверяет пакет (1 час). "
        "При ошибках направить на доработку, иначе завершить процесс.\n"
    )
    assert [item.type for item in rework.reworks] == [FlowType.CORRECTION]
    assert rework.reworks[0].fix_task == "Исправить ошибки"
    assert not any(item.flow == FlowType.TERMINATION for item in rework.decisions)


def _assert_contract(text: str, tasks: int, rollbacks: int, corrections: int, terminations: int, parallels: int) -> None:
    blueprint = analyze_process(text)
    assert len(blueprint.tasks) == tasks
    assert sum(item.type == FlowType.ROLLBACK for item in blueprint.reworks) == rollbacks
    assert sum(item.type == FlowType.CORRECTION for item in blueprint.reworks) == corrections
    assert sum(item.flow_type == FlowType.TERMINATION for item in blueprint.decisions) == terminations
    assert len(blueprint.parallel_blocks) == parallels
    assert all(block.flow_type == FlowType.PARALLEL for block in blueprint.parallel_blocks)
    code, _info = compile_blueprint(blueprint)
    gaps = execution_gaps(_diagram(code), blueprint)
    assert gaps == [], gaps
    xml, audit, err = execute_generated_code(code, regulation_text=text, blueprint=blueprint)
    assert not err, err
    assert audit["stats"]["work_items"] == tasks + corrections
    assert len(audit["rework_loops"]) == rollbacks + corrections
    assert audit.get("xsd_valid") is True
    assert xml_contract_gaps(xml, blueprint) == []


def test_contract_linear():
    _assert_contract(
        "Регламент: Линейный\n"
        "1. Диспетчер принимает заявку (10 минут).\n"
        "2. Мастер выполняет ремонт (1 час).\n"
        "3. Диспетчер закрывает заявку (15 минут).\n",
        tasks=3, rollbacks=0, corrections=0, terminations=0, parallels=0,
    )


def test_contract_one_rollback():
    _assert_contract(
        "Регламент: Один откат\n"
        "1. Исполнитель готовит пакет (1 час).\n"
        "2. Руководитель проверяет пакет (1 час). Если замечания — вернуть на п.1.\n",
        tasks=2, rollbacks=1, corrections=0, terminations=0, parallels=0,
    )


def test_contract_two_rollbacks():
    _assert_contract(
        "Регламент: Два отката\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет документы (1 час). Если комплект неполный — вернуть на п.1.\n"
        "3. Служба готовит условия (1 час).\n"
        "4. Инженер проводит осмотр (1 час). Если есть замечания — вернуть на п.3.\n",
        tasks=4, rollbacks=2, corrections=0, terminations=0, parallels=0,
    )


def test_contract_correction():
    _assert_contract(
        "Регламент: Доработка\n"
        "1. Исполнитель готовит документ (1 час).\n"
        "2. Руководитель проверяет документ (1 час). При ошибках направить на доработку.\n",
        tasks=2, rollbacks=0, corrections=1, terminations=0, parallels=0,
    )


def test_contract_termination():
    _assert_contract(
        "Регламент: Отказ\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет заявку (1 час). Если документы полные — перейти к п.3, "
        "иначе отказать и завершить процесс.\n"
        "3. Центр выдаёт договор (1 час).\n",
        tasks=3, rollbacks=0, corrections=0, terminations=1, parallels=0,
    )


def test_contract_parallel():
    _assert_contract(
        "Регламент: Параллель\n"
        "1. Диспетчер открывает наряд (1 час).\n"
        "2. Параллельно: Бригада ремонтирует линию (2 часа).\n"
        "3. Параллельно: Лаборатория берёт пробу (1 час).\n"
        "4. Диспетчер закрывает наряд (30 минут).\n",
        tasks=4, rollbacks=0, corrections=0, terminations=0, parallels=1,
    )


def test_contract_mixed():
    _assert_contract(
        "Регламент: Смешанный\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет документы (1 час). Если документы неполные — вернуть на п.1.\n"
        "3. Юрист проверяет договор (1 час). При ошибках направить на доработку.\n"
        "4. Служба готовит акт (1 час).\n"
        "5. Параллельно: Архив сканирует договор (1 час).\n"
        "6. Центр подтверждает комплект (1 час). Если заявитель отказался — отказать и завершить процесс.\n"
        "7. Центр передаёт документы в архив (1 час).\n",
        tasks=7, rollbacks=1, corrections=1, terminations=1, parallels=1,
    )


def test_contract_large_process():
    lines = ["Регламент: Сто шагов"]
    for num in range(1, 100):
        lines.append(f"{num}. Исполнитель выполняет шаг {num} (1 час).")
    lines.append(
        "100. Руководитель проверяет итог (1 час). Если замечания — вернуть на п.1."
    )
    _assert_contract("\n".join(lines), tasks=100, rollbacks=1, corrections=0, terminations=0, parallels=0)


def test_explicit_stage_reference_beats_other_words():
    blueprint = analyze_process(
        "Регламент: Обратно к этапу\n"
        "1. Исполнитель готовит пакет (1 час).\n"
        "2. Руководитель уточняет комплект (1 час).\n"
        "3. Служба публикует извещение (1 час).\n"
        "4. Комитет проверяет итог (1 час). Если ошибка — повторная диагностика, "
        "перейти обратно к этапу 2, иначе завершить процесс.\n"
    )
    assert [item.type for item in blueprint.reworks] == [FlowType.ROLLBACK]
    assert blueprint.reworks[0].target_num == 2
    assert blueprint.reworks[0].fix_task == ""
    assert not any(item.flow_type == FlowType.TERMINATION for item in blueprint.decisions)


def test_presentation_quality_on_procurement():
    xml, audit, err = generate_bpmn_from_text(PROCUREMENT, use_llm=False)
    assert not err
    score = bpmn_presentation_quality(audit, xml)
    assert score["structure"] >= 90
    assert score["readability"] >= 80
    assert score["logic"] >= 90
    assert score["optimization"] is None
    assert score["crossings"] is not None and score["crossings"] <= 2
    assert process_problem_lines(audit)
    improved = bpmn_presentation_quality(
        audit,
        xml,
        {
            "sla_before_hours": 528.3,
            "sla_after_hours": 488.3,
            "rework_before": 2,
            "rework_after": 0,
            "quality_before": 62,
            "quality_after": 100,
            "actions": [{"kind": "loops", "detail": "Цикл входного контроля заменён эскалацией."}],
        },
    )
    assert improved["optimization"] >= 80
    same = bpmn_presentation_quality(
        audit,
        xml,
        {
            "sla_before_hours": 10,
            "sla_after_hours": 10,
            "rework_before": 0,
            "rework_after": 0,
            "quality_before": 100,
            "quality_after": 100,
            "actions": [{"kind": "stable", "detail": "Существенных узких мест не найдено."}],
        },
    )
    assert same["optimization"] == 25
