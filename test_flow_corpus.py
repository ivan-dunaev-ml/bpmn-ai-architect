"""Корпус регламентов: тип потока и инварианты чертежа, без разбора текста в нормализаторе."""

import pytest

from process_blueprint import FlowType, analyze_process, blueprint_invariants


def _linear(title: str, steps: int) -> str:
    lines = [f"Регламент: {title}"]
    for num in range(1, steps + 1):
        lines.append(f"{num}. Исполнитель выполняет шаг {num} ({num} минут).")
    return "\n".join(lines)


def _rollback(title: str, target: int, check: int) -> str:
    lines = [f"Регламент: {title}"]
    for num in range(1, check):
        lines.append(f"{num}. Исполнитель готовит материал {num} (1 час).")
    lines.append(
        f"{check}. Руководитель проверяет материал {check} (1 час). "
        f"Если замечания — вернуть на п.{target}."
    )
    return "\n".join(lines)


CASES = []

for index in range(1, 9):
    CASES.append(("A", f"linear-{index}", _linear(f"Линейный {index}", 3 + index), ()))

for index, target in enumerate((1, 1, 1, 2, 2, 3, 5, 11), start=1):
    check = max(target + 1, 12 if target == 11 else target + 2)
    CASES.append(("B", f"rollback-{index}", _rollback(f"Откат {index}", target, check), (FlowType.ROLLBACK,)))

for index, phrase in enumerate((
    "При ошибках направить на доработку.",
    "При замечаниях устранить замечания.",
    "Если найдены ошибки — исправить ошибки.",
    "При отказе в комплекте направить на доработку.",
    "При сомнении — повторная диагностика.",
    "Если есть замечания — повторное согласование.",
), start=1):
    text = (
        f"Регламент: Исправление {index}\n"
        "1. Исполнитель готовит документ (1 час).\n"
        f"2. Руководитель проверяет документ (1 час). {phrase}\n"
    )
    CASES.append(("C", f"correction-{index}", text, (FlowType.CORRECTION,)))

for index, phrase in enumerate((
    "иначе «отказ заявителя» — завершить процесс.",
    "иначе аннулировать заявку и завершить процесс.",
    "иначе отказ заявителя.",
    "Если договор не подписан — завершить процесс.",
    "иначе «аннулировать заявку» — завершить процесс.",
    "иначе прекратить процесс.",
), start=1):
    text = (
        f"Регламент: Завершение {index}\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        f"2. Центр проверяет заявку (1 час). Если документы полные — перейти к п.3, {phrase}\n"
        "3. Центр выдаёт договор (1 час).\n"
    )
    CASES.append(("D", f"termination-{index}", text, ()))

for index in range(1, 5):
    text = (
        f"Регламент: Параллель {index}\n"
        "1. Диспетчер открывает наряд (1 час).\n"
        f"2. Параллельно: Бригада ремонтирует линию {index} (2 часа).\n"
        f"3. Параллельно: Лаборатория берёт пробу {index} (1 час).\n"
        "4. Диспетчер закрывает наряд (30 минут).\n"
    )
    CASES.append(("E", f"parallel-{index}", text, ()))

for index in range(1, 5):
    text = (
        f"Регламент: Вложенный шлюз {index}\n"
        "1. Диспетчер открывает наряд (1 час).\n"
        "2. Параллельно: Бригада ремонтирует линию (2 часа).\n"
        "3. Параллельно: Лаборатория берёт пробу (1 час).\n"
        f"4. Инженер проверяет результат {index} (1 час). Если замечания — вернуть на п.1.\n"
    )
    CASES.append(("F", f"nested-{index}", text, (FlowType.ROLLBACK,)))

for index in range(1, 5):
    text = (
        f"Регламент: Два отката {index}\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет документы (1 час). Если комплект неполный — вернуть на п.1.\n"
        f"3. Служба готовит условия {index} (1 час).\n"
        "4. Инженер проводит осмотр (1 час). Если есть замечания — вернуть на п.3.\n"
    )
    CASES.append(("G", f"multi-{index}", text, (FlowType.ROLLBACK, FlowType.ROLLBACK)))

for index in range(1, 5):
    text = (
        f"Регламент: Смешанный {index}\n"
        "1. Заявитель подаёт заявку (1 час).\n"
        "2. Центр проверяет документы (1 час). Если документы неполные — вернуть на п.1.\n"
        f"3. Юрист проверяет договор {index} (1 час). При ошибках направить на доработку.\n"
    )
    CASES.append(("H", f"mixed-{index}", text, (FlowType.ROLLBACK, FlowType.CORRECTION)))

for index, hours in enumerate(("15 минут", "2 часа", "1 рабочий день", "30 минут"), start=1):
    text = (
        f"Регламент: Срок {index}\n"
        f"1. Исполнитель готовит пакет ({hours}).\n"
        "2. Руководитель проверяет пакет (1 час). Если замечания — вернуть на п.1.\n"
    )
    CASES.append(("I", f"sla-{index}", text, (FlowType.ROLLBACK,)))

CASES.append(("J", "large-100", _rollback("Сто шагов", 1, 100), (FlowType.ROLLBACK,)))
CASES.append(("J", "large-120", _rollback("Сто двадцать шагов", 11, 120), (FlowType.ROLLBACK,)))


assert len(CASES) >= 50


@pytest.mark.parametrize(
    ("category", "name", "text", "types"),
    CASES,
    ids=[f"{category}-{name}" for category, name, _text, _types in CASES],
)
def test_blueprint_corpus(category, name, text, types):
    blueprint = analyze_process(text)
    assert blueprint_invariants(blueprint) == [], (category, name, blueprint_invariants(blueprint))
    assert tuple(item.type for item in blueprint.reworks) == types
    assert all(task.role for task in blueprint.tasks)
    if category == "E":
        assert blueprint.parallel_blocks
    if category == "J":
        assert len(blueprint.tasks) >= 100
    if category == "D":
        assert any(item.kind == "end" for item in blueprint.decisions)
        assert blueprint.reworks == []
    if category == "I":
        assert blueprint.tasks[0].hours
