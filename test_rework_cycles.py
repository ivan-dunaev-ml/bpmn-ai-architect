"""Возврат из регламента становится обратным ребром, процесс без возврата остаётся линейным."""

from ai_generator import (
    build_prompt,
    build_rework_repair_prompt,
    execute_generated_code,
    generate_bpmn_from_text,
)


_LINEAR = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Диспетчер"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Принять заявку", lanes[0])
b = DIAGRAM.add_user_task("Проверить комплект", lanes[0])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, b)
DIAGRAM.add_link(b, ROOT_END_TASK_ID)
"""

_WITH_RETURN = (
    "Регламент: Один возврат\n"
    "1. Диспетчер готовит пакет (10 минут).\n"
    "2. Начальник смены согласовывает пакет (20 минут). Если не согласовано — назад.\n"
    "3. Служба закрывает заявку (15 минут).\n"
)

_NO_RETURN = (
    "Регламент: Без возврата\n"
    "1. Диспетчер принимает заявку (10 минут).\n"
    "2. Начальник смены проверяет комплект (20 минут).\n"
    "3. Служба закрывает заявку (15 минут).\n"
)


def test_missing_rework_edge_is_explained():
    _, audit, err = execute_generated_code(_LINEAR, "Дыра возврата", regulation_text=_WITH_RETURN)
    assert audit == {}
    assert "REWORK VALIDATION FAILED" in err
    assert "Схема не готова" in err and "явных возврата" in err
    assert "Найдено:" in err and "Фраза:" in err and "назад" in err
    assert "Ожидается:" in err
    assert "нет обратного ребра" in err


def test_remark_return_is_one_cycle():
    text = (
        "Проверить заявку. "
        "При замечаниях вернуть исполнителю на доработку. "
        "После исправления повторно проверить."
    )
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert len(audit["rework_loops"]) == 1


def test_process_without_return_stays_linear():
    _, audit, err = generate_bpmn_from_text(_NO_RETURN, use_llm=False)
    assert not err
    assert audit["rework_loops"] == []
    assert audit["sla"]["critical_path_hours"] == audit["sla"]["with_rework_hours"]


def test_two_returns_are_two_cycles():
    text = (
        "Регламент: Два возврата\n"
        "1. Диспетчер принимает заявку (10 минут).\n"
        "2. Начальник смены проверяет комплект (20 минут). "
        "Если комплект полный — перейти к п.3, иначе «замечания» — вернуть на п.1.\n"
        "3. Служба готовит ответ (15 минут). "
        "Если согласовано — завершить процесс, иначе «на доработку» — вернуть на п.2.\n"
    )
    _, audit, err = generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert len(audit["rework_loops"]) == 2


def test_linear_fix_is_not_a_cycle():
    code = """
pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, ["Исполнитель"])
DIAGRAM.add_start_event("Старт", lanes[0], node_id=ROOT_START_TASK_ID)
DIAGRAM.add_end_event("Финал", lanes[0], node_id=ROOT_END_TASK_ID)
a = DIAGRAM.add_user_task("Проверить заявку", lanes[0])
g = DIAGRAM.add_exclusive_gateway("Заявка верна?", lanes[0])
b = DIAGRAM.add_user_task("Исправить заявку", lanes[0])
c = DIAGRAM.add_user_task("Продолжить оформление", lanes[0])
DIAGRAM.add_link(ROOT_START_TASK_ID, a)
DIAGRAM.add_link(a, g)
DIAGRAM.add_link(g, b, "Замечания")
DIAGRAM.add_link(b, c)
DIAGRAM.add_link(c, ROOT_END_TASK_ID)
DIAGRAM.add_link(g, ROOT_END_TASK_ID, "Согласовано")
"""
    plain = (
        "Регламент: Без возврата\n"
        "1. Исполнитель принимает заявку (10 минут).\n"
        "2. Исполнитель оформляет ответ (15 минут).\n"
    )
    _, audit, err = execute_generated_code(code, regulation_text=plain)
    assert not err, err
    assert audit["rework_loops"] == []


def test_rework_repair_prompt_asks_only_for_back_edges():
    prompt = build_prompt(_WITH_RETURN)
    assert "REWORK BPMN COMPLIANCE" in prompt
    assert "Возврат в регламенте = обязательный BPMN цикл." in prompt
    assert "_complete_return_edges" not in prompt
    repair = build_rework_repair_prompt(_WITH_RETURN, _LINEAR, ["REWORK VALIDATION FAILED"])
    assert "Не переписывай процесс." in repair
    assert "названия задач" in repair and "SLA" in repair
    assert "DIAGRAM.add_link" in repair
    assert "REWORK ISSUES:" in repair
    assert '"missing_link"' in repair
