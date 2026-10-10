"""Семантический чертёж процесса между текстом регламента и генерацией BPMN.

Возврат — не один тип. Чертёж различает три потока:

- rollback: назад на уже существующий шаг, новая задача не создаётся;
- correction: отдельная задача исправления и ребро на проверку;
- termination: ветка завершения, цикла нет.

Модель получает этот чертёж готовым. После генерации нормализатор
достраивает только недостающую часть своего типа и не меняет
уже существующие задачи, роли и сроки.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FlowType(str, Enum):
    """Тип потока. Сравнение со строкой «rollback» остаётся рабочим для уже записанных чертежей."""

    NORMAL = "normal"
    ROLLBACK = "rollback"
    CORRECTION = "correction"
    TERMINATION = "termination"
    PARALLEL = "parallel"


class TaskType(str, Enum):
    """Вид задачи BPMN. Компилятор не угадывает его заново."""

    USER = "user"
    SCRIPT = "script"


@dataclass
class BlueprintTask:
    num: int
    role: str
    title: str
    hours: Optional[float] = None
    stage: Optional[str] = None
    system: bool = False
    back_num: Optional[int] = None
    id: str = ""
    task_type: TaskType = TaskType.USER


@dataclass
class BlueprintDecision:
    num: int
    question: str
    yes_label: str
    no_label: str
    kind: str  # rework | end | branch
    role: str = ""
    flow: Optional[FlowType] = None
    yes_num: Optional[int] = None
    no_num: Optional[int] = None
    yes_end: bool = False
    no_end: bool = False
    else_end: bool = False
    id: str = ""
    gateway_hours: Optional[float] = None

    @property
    def yes_target(self) -> Optional[int]:
        if self.yes_end:
            return None
        return self.yes_num

    @property
    def no_target(self) -> Optional[int]:
        if self.no_end or self.else_end or self.flow == FlowType.TERMINATION:
            return None
        return self.no_num

    @property
    def flow_type(self) -> FlowType:
        return self.flow or FlowType.NORMAL


@dataclass
class BlueprintParallel:
    nums: List[int]
    roles: List[str]
    id: str = ""
    task_ids: List[str] = field(default_factory=list)
    split_name: str = "Параллельные работы"
    join_name: str = "Работы завершены"
    flow_type: FlowType = FlowType.PARALLEL


@dataclass
class BlueprintRework:
    """Один возврат процесса. Тип — FlowType, не свободная строка."""

    type: FlowType
    source_phrase: str
    check_task: str
    target_task: str
    fix_task: str
    condition: str
    gateway_name: str
    check_num: Optional[int] = None
    target_num: Optional[int] = None
    success_label: str = "Дальше"


@dataclass
class ProcessBlueprint:
    title: str
    sla_hours: Optional[float]
    pool: str = ""
    month_durations_ignored: bool = False
    roles: List[str] = field(default_factory=list)
    tasks: List[BlueprintTask] = field(default_factory=list)
    decisions: List[BlueprintDecision] = field(default_factory=list)
    parallel_blocks: List[BlueprintParallel] = field(default_factory=list)
    reworks: List[BlueprintRework] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        decisions = []
        for item in self.decisions:
            row = asdict(item)
            row["yes_target"] = item.yes_target
            row["no_target"] = item.no_target
            row["flow_type"] = item.flow_type.value
            decisions.append(row)
        return {
            "Process": self.title,
            "SLA": self.sla_hours,
            "Pool": self.pool or self.title,
            "Roles": self.roles,
            "Tasks": [asdict(item) for item in self.tasks],
            "Decisions": decisions,
            "ParallelBlocks": [asdict(item) for item in self.parallel_blocks],
            "Reworks": [asdict(item) for item in self.reworks],
        }


# Словарь намерения. Тип выбирает структура шага: номер существующего пункта
# важнее глагола исправления, завершение процесса важнее обоих.
_CORRECTION_MARKS = (
    "устранить замечан",
    "исправить ошиб",
    "направить на доработку",
    "на доработку",
    "повторно представить",
)
# «Повторно проверить» само по себе — продолжение уже открытого исправления, не второй возврат.
_CORRECTION_ECHO = (
    "повторная проверка",
    "повторно проверить",
)
# Повтор той же работы без номера пункта — исправление, не откат на чужой шаг.
_REPEAT_ACTIVITY = (
    ("повторн", "диагностик"),
    ("повторн", "согласован"),
)
_ROLLBACK_MARKS = (
    "вернуть к шаг",
    "вернуть к этап",
    "перейти обратно",
    "повторить предыдущ",
)


def _clause_text(step: Any) -> str:
    """Исходная фраза шага. Нормализованная подпись ветки («На доработку») тип не меняет."""
    decision = getattr(step, "decision", None)
    source = (getattr(decision, "source", "") or "").strip()
    if source:
        return source
    parts = (
        getattr(step, "title", "") or "",
        getattr(decision, "back_clause", "") or "",
    )
    return " ".join(part for part in parts if part)


def _mentioned_refs(step: Any) -> set:
    """Номера пунктов, которые написаны в шаге. Выведенный «предыдущий шаг» сюда не входит."""
    from ai_generator import _REF_RE

    return {int(match.group(1)) for match in _REF_RE.finditer(_clause_text(step))}


def _backward_target(step: Any) -> Optional[int]:
    """Откат только на пункт, который регламент назвал. Ссылка «на предыдущий» — ещё не тип."""
    decision = getattr(step, "decision", None)
    num = getattr(step, "num", None)
    mentioned = _mentioned_refs(step)
    if decision is not None and decision.no_ref is not None and num is not None and decision.no_ref < num:
        if decision.no_ref in mentioned:
            return decision.no_ref
    if decision is not None and decision.yes_ref is not None and num is not None and decision.yes_ref < num:
        if decision.yes_ref in mentioned:
            return decision.yes_ref
    back_ref = getattr(step, "back_ref", None)
    if back_ref is not None and num is not None and back_ref < num:
        return back_ref
    text = _clause_text(step)
    low = text.lower()
    from ai_generator import _EXPLICIT_RETURN_RE

    if num is not None and (_EXPLICIT_RETURN_RE.search(text) or "перейти обратно" in low):
        backward = [ref for ref in mentioned if ref < num]
        if backward:
            return max(backward)
    return None


def clause_intent(text: str) -> Optional[FlowType]:
    """Намерение фразы. Передача вещи сюда не входит."""
    from ai_generator import (
        _END_KW,
        _EXPLICIT_RETURN_RE,
        _NAZAD_RETURN_RE,
        _REF_RE,
        _REWORK_CLAUSE_RE,
        _is_physical_handoff,
    )

    src = text or ""
    if _is_physical_handoff(src):
        return None
    low = src.lower()
    # Приоритет: явный пункт, затем «повторить», затем «доработать», затем «завершить».
    numbered = bool(
        _REF_RE.search(src)
        and (_EXPLICIT_RETURN_RE.search(src) or "перейти обратно" in low)
    )
    if numbered:
        return FlowType.ROLLBACK
    if any(left in low and right in low for left, right in _REPEAT_ACTIVITY):
        return FlowType.CORRECTION
    if any(mark in low for mark in _CORRECTION_ECHO) and re.search(r"если|при\s|ошиб|замечан", low):
        return FlowType.CORRECTION
    if any(mark in low for mark in _ROLLBACK_MARKS):
        return FlowType.ROLLBACK
    if any(mark in low for mark in _CORRECTION_MARKS):
        return FlowType.CORRECTION
    if _NAZAD_RETURN_RE.search(src):
        return FlowType.ROLLBACK
    if _REWORK_CLAUSE_RE.search(src):
        if "доработ" in low or "устранить" in low or "исправить" in low:
            return FlowType.CORRECTION
        return FlowType.ROLLBACK
    if _END_KW.search(src) or "аннулир" in low or "отказ заявителя" in low or re.search(r"\bотказать\b", low):
        return FlowType.TERMINATION
    return None


def classify_flow(step: Any) -> Optional[FlowType]:
    """Тип потока по уже разобранному шагу, а не по новому выражению поверх парсера.

    Ссылка на существующий пункт — rollback.
    Глагол исправления без пункта — correction.
    Ветка завершения без возврата — termination.
    """
    from ai_generator import _is_physical_handoff

    if _is_physical_handoff(_clause_text(step)):
        return None
    decision = getattr(step, "decision", None)
    if _backward_target(step) is not None:
        return FlowType.ROLLBACK
    intent = clause_intent(_clause_text(step))
    if intent in (FlowType.ROLLBACK, FlowType.CORRECTION):
        return intent
    if decision is not None and (decision.no_end or decision.yes_end):
        return FlowType.TERMINATION
    return intent


def _step_is_rework(step: Any) -> bool:
    return classify_flow(step) in (FlowType.ROLLBACK, FlowType.CORRECTION)


def _decision_kind(step: Any) -> str:
    kind = classify_flow(step)
    if kind in (FlowType.ROLLBACK, FlowType.CORRECTION):
        return "rework"
    if kind == FlowType.TERMINATION:
        return "end"
    decision = step.decision
    if decision is not None and (decision.yes_end or decision.no_end):
        return "end"
    return "branch"


def correction_fix_name(step: Any) -> str:
    """Имя задачи исправления. У rollback такой задачи нет."""
    low = _clause_text(step).lower()
    if "ошиб" in low:
        return "Исправить ошибки"
    return "Устранить замечания"


def _fix_task_name(condition: str) -> str:
    """Имя синтетической задачи исправления: инфинитив и объект, без чужого шага."""
    label = re.sub(r"\s+", " ", (condition or "").strip())
    low = label.lower()
    if "не соглас" in low or "отклон" in low:
        return "Исправить замечания"
    if "ошиб" in low:
        return "Исправить ошибки"
    if not label or low in ("иначе", "нет", "на доработку"):
        label = "замечания"
    if low.startswith(("устранить ", "исправить ")):
        return label[:80]
    body = label[:1].lower() + label[1:]
    return f"Устранить {body}"[:80]


def _condition(step: Any) -> str:
    decision = getattr(step, "decision", None)
    label = (getattr(decision, "no_label", "") or "").strip()
    low = label.lower()
    if "не соглас" in low or "отклон" in low:
        return "Не согласовано"
    if "ошиб" in low:
        return "Ошибки"
    if label and label.lower() not in ("иначе", "нет"):
        return label[:40]
    return "Замечания"


def analyze_process(regulation_text: str) -> ProcessBlueprint:
    """Чертёж As-Is: возвраты уже отделены от завершения процесса и от передачи вещей."""
    from ai_generator import parse_regulation

    parsed = parse_regulation(regulation_text or "")
    steps = list(parsed.steps)
    by_num = {step.num: step for step in steps}
    blueprint = ProcessBlueprint(
        title=parsed.title,
        sla_hours=parsed.sla_hours,
        pool=parsed.title,
        month_durations_ignored=bool(getattr(parsed, "month_durations_ignored", False)),
        roles=list(parsed.roles),
        tasks=[
            BlueprintTask(
                num=step.num,
                role=step.role,
                title=step.title,
                hours=step.hours,
                stage=step.stage,
                system=bool(step.system),
                back_num=step.back_ref,
                id=f"T{step.num}",
                task_type=TaskType.SCRIPT if step.system else TaskType.USER,
            )
            for step in steps
            if step.title
        ],
    )
    blueprint.parallel_blocks = _parallel_blocks(steps)

    for step in steps:
        if step.decision is None and not _step_is_rework(step):
            continue
        flow = classify_flow(step)
        if step.decision is not None:
            question = (step.decision.yes_label or "Условие").rstrip("?") + "?"
            decision = step.decision
            blueprint.decisions.append(
                BlueprintDecision(
                    num=step.num,
                    question=question[:80],
                    yes_label=decision.yes_label,
                    no_label=decision.no_label,
                    kind=_decision_kind(step),
                    role=step.role or "",
                    flow=flow if flow in (FlowType.ROLLBACK, FlowType.CORRECTION, FlowType.TERMINATION) else FlowType.NORMAL,
                    yes_num=decision.yes_ref,
                    no_num=decision.no_ref,
                    yes_end=bool(decision.yes_end),
                    no_end=bool(decision.no_end),
                    else_end=bool(
                        decision.has_else
                        and not decision.no_end
                        and decision.no_ref is None
                        and not decision.no_back
                    ),
                    id=f"D{step.num}",
                )
            )
        elif flow == FlowType.ROLLBACK:
            blueprint.decisions.append(
                BlueprintDecision(
                    num=step.num,
                    question="Результат принят?",
                    yes_label="Дальше",
                    no_label="Возврат",
                    kind="rework",
                    role=step.role or "",
                    flow=FlowType.ROLLBACK,
                    no_num=_backward_target(step),
                    id=f"D{step.num}",
                    gateway_hours=0.0,
                )
            )
        if flow not in (FlowType.ROLLBACK, FlowType.CORRECTION):
            continue
        decision = step.decision
        target_num = _backward_target(step)
        if flow == FlowType.ROLLBACK and target_num is None:
            earlier = [item for item in steps if item.num < step.num]
            if earlier:
                target_num = earlier[-1].num
        target = by_num.get(target_num) if target_num is not None else None
        phrase = (decision.back_clause if decision is not None else "") or step.title
        condition = _condition(step)
        question = ""
        success = "Дальше"
        if decision is not None:
            question = ((decision.yes_label or "Условие").rstrip("?") + "?")[:80]
            success = (decision.yes_label or "Дальше")[:40]
        check_task = step.title or "Проверить"
        if flow == FlowType.ROLLBACK:
            target_task = (target.title if target is not None else "") or ""
            fix_task = ""
        else:
            target_task = check_task
            fix_task = correction_fix_name(step)
        blueprint.reworks.append(
            BlueprintRework(
                type=flow,
                source_phrase=re.sub(r"\s+", " ", phrase).strip()[:160],
                check_num=step.num,
                check_task=check_task,
                fix_task=fix_task,
                target_num=target_num if flow == FlowType.ROLLBACK else step.num,
                target_task=target_task,
                condition=condition,
                gateway_name=question or "Результат принят?",
                success_label=success,
            )
        )
    return blueprint


def _parallel_blocks(steps: List[Any]) -> List[BlueprintParallel]:
    """«Параллельно» присоединяет шаг к предыдущему. Развилка в блок не входит.

    Правило живёт в чертеже. Генератор потом не собирает AND заново из текста.
    """
    blocks: List[tuple] = []
    for step in steps:
        if getattr(step, "decision", None) is not None:
            blocks.append(("decision", [step]))
        elif step.parallel:
            if step.fork_parallel or not blocks or blocks[-1][0] not in ("step", "parallel"):
                blocks.append(("parallel", [step]))
            elif blocks[-1][0] == "parallel":
                blocks[-1][1].append(step)
            else:
                blocks[-1] = ("parallel", blocks[-1][1] + [step])
        else:
            blocks.append(("step", [step]))
    result: List[BlueprintParallel] = []
    for kind, group in blocks:
        if kind != "parallel" or len(group) < 2:
            continue
        nums = [item.num for item in group]
        result.append(
            BlueprintParallel(
                nums=nums,
                roles=list(dict.fromkeys(item.role for item in group if item.role)),
                id=f"P{len(result) + 1}",
                task_ids=[f"T{num}" for num in nums],
                flow_type=FlowType.PARALLEL,
            )
        )
    return result


@dataclass
class _EmitUnit:
    kind: str
    tasks: List[BlueprintTask]
    name: str = ""
    decision_num: Optional[int] = None

    @property
    def role(self) -> str:
        return self.tasks[0].role if self.tasks else ""


def _layout_units(blueprint: ProcessBlueprint) -> List[_EmitUnit]:
    """Порядок блоков схемы строго из чертежа: шаг, развилка, AND, подпроцесс этапа."""
    decisions = {item.num: item for item in blueprint.decisions}
    block_of = {}
    for block in blueprint.parallel_blocks:
        for num in block.nums:
            block_of[num] = block
    pinned = set()
    for decision in blueprint.decisions:
        if decision.yes_num is not None:
            pinned.add(decision.yes_num)
        if decision.no_num is not None and decision.flow == FlowType.ROLLBACK:
            pinned.add(decision.no_num)
    for item in blueprint.reworks:
        if item.type == FlowType.ROLLBACK and item.target_num is not None:
            pinned.add(item.target_num)
    for task in blueprint.tasks:
        if task.back_num is not None:
            pinned.add(task.back_num)

    units: List[_EmitUnit] = []
    consumed = set()
    task_by = {task.num: task for task in blueprint.tasks}
    for num in sorted(set(task_by) | set(decisions)):
        if num in consumed:
            continue
        block = block_of.get(num)
        if block is not None and num == block.nums[0]:
            members = [item for item in blueprint.tasks if item.num in set(block.nums)]
            members.sort(key=lambda item: block.nums.index(item.num))
            units.append(_EmitUnit("parallel", members))
            consumed.update(item.num for item in members)
            continue
        task = task_by.get(num)
        if num in decisions:
            units.append(_EmitUnit("decision", [task] if task is not None else [], decision_num=num))
        elif task is not None:
            units.append(_EmitUnit("step", [task]))
        consumed.add(num)

    stage_marks: Dict[str, int] = {}
    for task in blueprint.tasks:
        if task.stage:
            stage_marks[task.stage] = stage_marks.get(task.stage, 0) + 1

    staged: List[_EmitUnit] = []
    index = 0
    while index < len(units):
        unit = units[index]
        stage = unit.tasks[0].stage if unit.kind == "step" and unit.tasks else None
        if not stage:
            staged.append(unit)
            index += 1
            continue
        # Этап назван у одного шага («(этап «Диагностика»)») — отсюда он начинается и продолжается
        # следующими шагами той же роли без своего этапа: до развилки, параллели, смены роли или нового этапа.
        # Этап, отмеченный у нескольких шагов, перечислен явно — чужие шаги в него не добавляются.
        carry = stage_marks.get(stage, 0) == 1
        nxt = index + 1
        while (
            nxt < len(units)
            and units[nxt].kind == "step"
            and units[nxt].tasks
            and (units[nxt].tasks[0].stage == stage or (carry and units[nxt].tasks[0].stage is None))
            and units[nxt].role == unit.role
            and units[nxt].tasks[0].num not in pinned
        ):
            nxt += 1
        run = units[index:nxt]
        # Возврат на первый шаг этапа = повтор этапа целиком: ребро ведёт на подпроцесс, шаг остаётся внутри.
        # Цель возврата в середине этапа в подпроцесс не попадает — внутрь подпроцесса ребро не проходит
        # (такие шаги уже отрезаны условием цикла выше).
        if len(run) >= 2:
            staged.append(_EmitUnit("subprocess", [task for item in run for task in item.tasks], name=stage))
        else:
            staged.extend(run)
        index = nxt
    return staged


def _unit_index(units: List[_EmitUnit]) -> Dict[int, int]:
    mapping: Dict[int, int] = {}
    for index, unit in enumerate(units):
        for task in unit.tasks:
            mapping[task.num] = index
    return mapping


def _branch_targets(
    index: int,
    decision: BlueprintDecision,
    rework: Optional[BlueprintRework],
    n_units: int,
    num_to_unit: Dict[int, int],
) -> tuple:
    """Куда ведут ветки развилки. Откат и исправление не подменяются завершением."""
    nxt = index + 1 if index + 1 < n_units else "END"

    def resolve(num: Optional[int]) -> Any:
        if num is None or num not in num_to_unit:
            return nxt
        target = num_to_unit[num]
        return nxt if target == index else target

    if decision.yes_end:
        yes = "END"
    elif decision.yes_num is not None and decision.yes_num > decision.num:
        yes = resolve(decision.yes_num)
    else:
        yes = nxt
    flow = decision.flow
    if flow == FlowType.ROLLBACK and rework is not None:
        no = resolve(rework.target_num)
    elif flow == FlowType.CORRECTION:
        no = "FIX"
    elif (flow == FlowType.TERMINATION and not decision.yes_end) or decision.no_end:
        no = "END"
    elif decision.no_num is not None and not (decision.no_num < decision.num):
        no = resolve(decision.no_num)
    elif decision.else_end:
        no = "END"
    else:
        no = nxt
    return yes, no


def _flatten_units(blueprint: ProcessBlueprint, units: List[_EmitUnit]) -> List[_EmitUnit]:
    """Шлюз с одной и той же целью обеих веток остаётся обычным шагом."""
    decisions = {item.num: item for item in blueprint.decisions}
    reworks = {item.check_num: item for item in blueprint.reworks}
    mapping = _unit_index(units)
    flat: List[_EmitUnit] = []
    for index, unit in enumerate(units):
        if unit.kind != "decision":
            flat.append(unit)
            continue
        num = unit.decision_num if unit.decision_num is not None else (unit.tasks[0].num if unit.tasks else None)
        decision = decisions.get(num) if num is not None else None
        if decision is None:
            flat.append(unit)
            continue
        yes, no = _branch_targets(index, decision, reworks.get(decision.num), len(units), mapping)
        if yes == no:
            if unit.tasks:
                flat.append(_EmitUnit("step", unit.tasks))
            continue
        flat.append(unit)
    return flat


def compile_blueprint(blueprint: ProcessBlueprint) -> tuple:
    """Код DIAGRAM только из полей чертежа. Исходный регламент сюда не передаётся."""
    units = _flatten_units(blueprint, _layout_units(blueprint))
    decisions = {item.num: item for item in blueprint.decisions}
    reworks = {item.check_num: item for item in blueprint.reworks}
    num_to_unit = _unit_index(units)
    lane_var = {role: f"lane_{index}" for index, role in enumerate(blueprint.roles)}

    def lane(role: str) -> str:
        return lane_var.get(role) or "lane_0"

    code: List[str] = [
        f"# Сгенерировано семантическим эмулятором: {blueprint.title}",
        "pool_id, lanes = DIAGRAM.add_pool(ROOT_PROCESS_ID, "
        + json.dumps(list(blueprint.roles), ensure_ascii=False)
        + ")",
    ]
    code += [f"lane_{index} = lanes[{index}]" for index in range(len(blueprint.roles))]

    def emit_task(var: str, task: BlueprintTask, parent: str) -> None:
        func = "add_script_task" if task.task_type == TaskType.SCRIPT else "add_user_task"
        code.append(f"{var} = DIAGRAM.{func}({task.title!r}, {parent})")
        if task.hours is not None:
            code.append(f"DIAGRAM.set_sla({var}, {round(float(task.hours), 3)})")

    entry: List[str] = []
    exits: List[str] = []
    gateways: Dict[int, str] = {}
    for index, unit in enumerate(units):
        if unit.kind == "step":
            var = f"n{index}"
            emit_task(var, unit.tasks[0], lane(unit.role))
            entry.append(var)
            exits.append(var)
        elif unit.kind == "decision":
            num = unit.decision_num if unit.decision_num is not None else unit.tasks[0].num
            decision = decisions[num]
            gw = f"g{index}"
            gate_lane = lane(decision.role or unit.role)
            if unit.tasks:
                var = f"n{index}"
                emit_task(var, unit.tasks[0], lane(unit.role))
                code.append(f"{gw} = DIAGRAM.add_exclusive_gateway({decision.question!r}, {gate_lane})")
                if decision.gateway_hours is not None:
                    code.append(f"DIAGRAM.set_sla({gw}, {decision.gateway_hours})")
                code.append(f"DIAGRAM.add_link({var}, {gw})")
                entry.append(var)
            else:
                code.append(f"{gw} = DIAGRAM.add_exclusive_gateway({decision.question!r}, {gate_lane})")
                if decision.gateway_hours is not None:
                    code.append(f"DIAGRAM.set_sla({gw}, {decision.gateway_hours})")
                entry.append(gw)
            exits.append(gw)
            gateways[index] = gw
        elif unit.kind == "parallel":
            split, join = f"ps{index}", f"pj{index}"
            block = next(
                (
                    item for item in blueprint.parallel_blocks
                    if tuple(item.nums) == tuple(task.num for task in unit.tasks)
                ),
                None,
            )
            split_name = block.split_name if block is not None else "Параллельные работы"
            join_name = block.join_name if block is not None else "Работы завершены"
            code.append(f"{split} = DIAGRAM.add_parallel_gateway({split_name!r}, {lane(unit.role)})")
            code.append(f"{join} = DIAGRAM.add_parallel_gateway({join_name!r}, {lane(unit.role)})")
            for offset, task in enumerate(unit.tasks):
                var = f"n{index}_{offset}"
                emit_task(var, task, lane(task.role))
                code.append(f"DIAGRAM.add_link({split}, {var})")
                code.append(f"DIAGRAM.add_link({var}, {join})")
            entry.append(split)
            exits.append(join)
        else:
            sub = f"sp{index}"
            code.append(f"{sub} = DIAGRAM.create_subprocess({unit.name!r}, {lane(unit.role)})")
            prev = None
            for offset, task in enumerate(unit.tasks):
                var = f"n{index}_{offset}"
                emit_task(var, task, sub)
                if prev:
                    code.append(f"DIAGRAM.add_link({prev}, {var})")
                prev = var
            entry.append(sub)
            exits.append(sub)

    end_expr = "ROOT_END_TASK_ID"
    if not entry:
        code.append("DIAGRAM.add_link(ROOT_START_TASK_ID, ROOT_END_TASK_ID)")
    else:
        code.append(f"DIAGRAM.add_link(ROOT_START_TASK_ID, {entry[0]})")

    def as_expr(target: Any) -> str:
        if target == "END":
            return end_expr
        return entry[target]

    for index, unit in enumerate(units):
        nxt = entry[index + 1] if index + 1 < len(units) else end_expr
        if unit.kind != "decision":
            code.append(f"DIAGRAM.add_link({exits[index]}, {nxt})")
            continue
        decision = decisions[unit.decision_num if unit.decision_num is not None else unit.tasks[0].num]
        rework = reworks.get(decision.num)
        gw = gateways[index]
        yes, no = _branch_targets(index, decision, rework, len(units), num_to_unit)
        if decision.flow == FlowType.CORRECTION and rework is not None:
            fix = f"fix{index}"
            code.append(
                f"{fix} = DIAGRAM.add_user_task({rework.fix_task!r}, {lane(decision.role or unit.role)})"
            )
            code.append(f"DIAGRAM.add_link({gw}, {fix}, {decision.no_label!r})")
            code.append(f"DIAGRAM.add_link({fix}, {entry[index]})")
            code.append(f"DIAGRAM.add_link({gw}, {as_expr(yes)}, {decision.yes_label!r})")
            continue
        code.append(f"DIAGRAM.add_link({gw}, {as_expr(yes)}, {decision.yes_label!r})")
        if no != yes:
            code.append(f"DIAGRAM.add_link({gw}, {as_expr(no)}, {decision.no_label!r})")

    info = {
        "title": blueprint.title,
        "steps": len(blueprint.tasks),
        "roles": list(blueprint.roles),
        "blocks": len(units),
        "subprocesses": sum(1 for unit in units if unit.kind == "subprocess"),
        "decisions": sum(1 for unit in units if unit.kind == "decision"),
        "parallel_groups": sum(1 for unit in units if unit.kind == "parallel"),
        "sla_hours": blueprint.sla_hours,
    }
    return "\n".join(code) + "\n", info


def blueprint_prompt_block(regulation_text: str) -> str:
    """Фрагмент промпта: модель строит циклы из чертежа, а не ищет их в тексте."""
    blueprint = analyze_process(regulation_text)
    body = json.dumps(blueprint.as_dict(), ensure_ascii=False, indent=2)
    return (
        "==================================================\n"
        "PROCESS BLUEPRINT\n"
        "==================================================\n\n"
        "Семантический разбор уже выполнен кодом. Не ищи возвраты в тексте заново.\n"
        "Roles, Tasks, Decisions, ParallelBlocks и Reworks ниже — обязательный чертёж.\n"
        "type=rollback: не создавай задачу. Проверка → XOR → условие → существующая target_task.\n"
        "type=correction: создай fix_task и DIAGRAM.add_link(fix_task, check_task).\n"
        "Решение kind=end завершает процесс и циклом не является.\n\n"
        + body
    )


def _work_nodes(diagram: Any) -> List[Any]:
    kinds = {"task", "userTask", "scriptTask", "manualTask", "serviceTask", "sendTask", "receiveTask", "businessRuleTask"}
    return [node for node in diagram.nodes.values() if node.kind in kinds]


def _find_node(diagram: Any, title: str) -> Any:
    from ai_generator import _sla_title_score

    wanted = (title or "").strip()
    exact = [node for node in _work_nodes(diagram) if (node.name or "").strip() == wanted]
    if len(exact) == 1:
        return exact[0]
    best, score = None, 0
    pool = exact or _work_nodes(diagram)
    for node in pool:
        got = _sla_title_score(title or "", node.name or "")
        if got > score:
            best, score = node, got
    return best if score >= 2 else None


def _reaches(diagram: Any, start: str, goal: str) -> bool:
    seen = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == goal and current != start:
            return True
        for link in diagram.links:
            if link.source_id == current:
                stack.append(link.target_id)
    return False


def _back_edge_count(diagram: Any) -> int:
    count = 0
    for link in diagram.links:
        if link.source_id != link.target_id and _reaches(diagram, link.target_id, link.source_id):
            count += 1
    return count


def _back_links(diagram: Any) -> List[Any]:
    from ai_generator import _back_link_list

    return _back_link_list(diagram)


def _lands_on_target(diagram: Any, hit_id: str, target: Any, check: Any) -> bool:
    """Обратное ребро попало в шаг N, в его подпроцесс или в блок, из которого шаг N ещё достижим.

    Возврат на шлюз «Параллельные работы», из которого открывается п.11, — это возврат на п.11.
    Возврат на более ранний шаг, из которого до проверки ещё надо пройти чужие пункты, сюда не входит:
    обход останавливается на самой проверке.
    """
    if hit_id == target.id:
        return True
    node = diagram.nodes.get(hit_id)
    if node is not None and node.kind == "subProcess":
        from ai_generator import _owner_holds

        if _owner_holds(diagram, target.id, hit_id):
            return True
    if check is None:
        return False
    seen = set()
    stack = [hit_id]
    while stack:
        current = stack.pop()
        if current in seen or current == check.id:
            continue
        seen.add(current)
        if current == target.id:
            return True
        for link in diagram.links:
            if link.source_id == current and link.target_id not in seen:
                stack.append(link.target_id)
    return False


def _conditioned_return(diagram: Any, check: Any, source_id: str) -> bool:
    """От проверки до источника обратного ребра есть XOR с подписанной веткой."""
    seen = set()
    stack = [check.id]
    gates = []
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = diagram.nodes.get(current)
        if node is not None and node.kind == "exclusiveGateway":
            gates.append(node)
        for link in diagram.links:
            if link.source_id == current:
                stack.append(link.target_id)
    if source_id not in seen:
        return False
    for gate in gates:
        outs = [link for link in diagram.links if link.source_id == gate.id and (link.condition_name or "").strip()]
        if gate.id == source_id and outs:
            return True
        for link in outs:
            if link.target_id == source_id or _reaches(diagram, link.target_id, source_id):
                return True
    return False


def rollback_cycle_holds(diagram: Any, item: BlueprintRework) -> bool:
    """Допустимый цикл rollback: проверка, XOR, ветка условия и путь на существующую target_task.

    Новая задача исправления в этот цикл не входит. Прямое ребро на проверку его не заменяет.
    """
    check = _find_node(diagram, item.check_task)
    target = _find_node(diagram, item.target_task)
    if check is None or target is None:
        return False
    for link in _back_links(diagram):
        if not _lands_on_target(diagram, link.target_id, target, check):
            continue
        if link.source_id != check.id and not _reaches(diagram, check.id, link.source_id):
            continue
        if _conditioned_return(diagram, check, link.source_id):
            return True
    return False


def correction_cycle_holds(diagram: Any, item: BlueprintRework) -> bool:
    """Допустимый цикл correction: проверка, XOR, задача исправления и возврат на ту же проверку.

    Откат на другой пункт этот цикл не закрывает.
    """
    check = _find_node(diagram, item.check_task)
    fix = _find_node(diagram, item.fix_task) if item.fix_task else None
    if check is None or fix is None or fix.id == check.id:
        return False
    if not any(link.source_id == fix.id and link.target_id == check.id for link in diagram.links):
        return False
    gate = _exclusive_after(diagram, check)
    if gate is None:
        return _conditioned_return(diagram, check, fix.id)
    labeled = [
        link for link in diagram.links
        if link.source_id == gate.id and (link.condition_name or "").strip()
    ]
    if not labeled:
        return False
    return any(link.target_id == fix.id for link in diagram.links if link.source_id == gate.id) or _reaches(
        diagram, gate.id, fix.id
    )


def validate_rollback(diagram: Any, blueprint: ProcessBlueprint) -> List[str]:
    gaps: List[str] = []
    for item in blueprint.reworks:
        if item.type != FlowType.ROLLBACK or rollback_cycle_holds(diagram, item):
            continue
        where = item.target_task or f"п.{item.target_num}"
        gaps.append(f"«{item.source_phrase}»: нет отката на «{where}»")
    return gaps


def validate_correction(diagram: Any, blueprint: ProcessBlueprint) -> List[str]:
    gaps: List[str] = []
    for item in blueprint.reworks:
        if item.type != FlowType.CORRECTION or correction_cycle_holds(diagram, item):
            continue
        gaps.append(f"«{item.source_phrase}»: нет исправления «{item.fix_task}» с возвратом на проверку")
    return gaps


def validate_reworks(diagram: Any, blueprint: ProcessBlueprint) -> List[str]:
    """Проверка по типу: rollback ищет путь к шагу, correction — ребро исправления на проверку."""
    return validate_rollback(diagram, blueprint) + validate_correction(diagram, blueprint)


def _terminal_decisions(blueprint: ProcessBlueprint) -> List[BlueprintDecision]:
    """Ветки завершения. Откат и исправление эту ветку не занимают."""
    taken = {
        item.check_num
        for item in blueprint.reworks
        if item.type in (FlowType.ROLLBACK, FlowType.CORRECTION)
    }
    found = []
    for decision in blueprint.decisions:
        if decision.num in taken:
            continue
        if (
            decision.flow == FlowType.TERMINATION
            or decision.kind == "end"
            or decision.no_end
            or decision.yes_end
            or decision.else_end
        ):
            found.append(decision)
    return found


def _success_nums(diagram: Any, blueprint: ProcessBlueprint) -> List[int]:
    """Номера шагов по положительным веткам. Откат, исправление и завершение сюда не входят."""
    title_num = {task.title: task.num for task in blueprint.tasks}
    yes_labels = {(decision.yes_label or "").strip() for decision in blueprint.decisions}
    start = diagram.root_start_id if diagram.root_start_id in diagram.nodes else None
    if start is None:
        return []
    nums: List[int] = []
    current = start
    seen = set()
    while current and current not in seen:
        seen.add(current)
        node = diagram.nodes.get(current)
        if node is None:
            break
        if node.name in title_num:
            nums.append(title_num[node.name])
        if node.kind == "subProcess":
            # Шаги этапа идут внутри подпроцесса: от его старта до конца по единственному выходу.
            inner = getattr(node, "inner_start_id", None)
            if not any(link.source_id == inner for link in diagram.links):
                # До лечения графа старт подпроцесса ещё не связан: первый шаг — без входящих рёбер.
                targets = {link.target_id for link in diagram.links}
                heads = sorted(
                    (item for item in diagram.nodes.values()
                     if item.owner_id == node.id and item.kind not in ("startEvent", "endEvent") and item.id not in targets),
                    key=lambda item: item.order,
                )
                inner = heads[0].id if heads else None
            guard = 0
            while inner and guard < 60:
                guard += 1
                inner_node = diagram.nodes.get(inner)
                if inner_node is None or inner_node.kind == "endEvent":
                    break
                if inner_node.name in title_num:
                    nums.append(title_num[inner_node.name])
                inner_outs = [link for link in diagram.links if link.source_id == inner]
                inner = inner_outs[0].target_id if len(inner_outs) == 1 else None
        outs = [link for link in diagram.links if link.source_id == current]
        if not outs:
            break
        if node.kind == "exclusiveGateway" and len(outs) > 1:
            continuing = [
                link for link in outs
                if diagram.nodes.get(link.target_id) is None
                or diagram.nodes[link.target_id].kind != "endEvent"
            ]
            labeled = [link for link in continuing if (link.condition_name or "").strip() in yes_labels]
            if labeled:
                chosen = labeled[0]
            elif continuing:
                chosen = continuing[0]
            else:
                chosen = outs[0]
            current = chosen.target_id
            continue
        if node.kind == "parallelGateway" and len(outs) > 1:
            join_id = None
            for link in outs:
                branch = link.target_id
                guard = 0
                while branch and guard < 30:
                    guard += 1
                    branch_node = diagram.nodes.get(branch)
                    if branch_node is None:
                        break
                    if branch_node.kind == "parallelGateway":
                        join_id = branch
                        break
                    if branch_node.name in title_num:
                        nums.append(title_num[branch_node.name])
                    branch_outs = [item for item in diagram.links if item.source_id == branch]
                    if len(branch_outs) != 1:
                        break
                    branch = branch_outs[0].target_id
            if join_id is None:
                break
            join_outs = [link for link in diagram.links if link.source_id == join_id]
            current = join_outs[0].target_id if join_outs else None
            continue
        if len(outs) == 1:
            current = outs[0].target_id
            continue
        break
    return nums


def execution_gaps(diagram: Any, blueprint: ProcessBlueprint) -> List[str]:
    """Расхождение BPMN с чертежом. Пустой список — схема исполнила чертёж."""
    gaps: List[str] = []
    work = _work_nodes(diagram)
    corrections = [item for item in blueprint.reworks if item.type == FlowType.CORRECTION]
    titles = {task.title for task in blueprint.tasks}
    fix_names = {item.fix_task for item in corrections if item.fix_task}
    if len(work) != len(blueprint.tasks) + len(corrections):
        gaps.append(
            f"задач {len(work)}, в чертеже {len(blueprint.tasks)} и исправлений {len(corrections)}"
        )
    for node in work:
        if node.name not in titles and node.name not in fix_names:
            gaps.append(f"Лишняя задача «{node.name}»")
    by_title: Dict[str, List[Any]] = {}
    for node in work:
        by_title.setdefault(node.name, []).append(node)
    for task in blueprint.tasks:
        nodes = by_title.get(task.title) or []
        if not nodes:
            gaps.append(f"Потеряна задача №{task.num}")
            continue
        node = nodes[0]
        lane = diagram.lanes.get(node.lane_id or "")
        role = lane.name if lane is not None else ""
        if role != (task.role or ""):
            gaps.append(f"шаг {task.num}: роль «{role}» вместо «{task.role}»")
        if task.hours is not None and node.sla_hours is not None:
            if abs(float(node.sla_hours) - float(task.hours)) > 0.01:
                gaps.append(f"шаг {task.num}: срок {node.sla_hours} вместо {task.hours}")
    for item in blueprint.reworks:
        if item.type == FlowType.ROLLBACK:
            if item.fix_task:
                gaps.append(f"rollback на шаге {item.check_num} содержит задачу исправления")
            if not rollback_cycle_holds(diagram, item):
                gaps.append(f"rollback на п.{item.target_num} не ведёт на существующий шаг")
        elif item.type == FlowType.CORRECTION and not correction_cycle_holds(diagram, item):
            gaps.append(f"correction «{item.fix_task}» не возвращается на проверку")
    end_ids = {node.id for node in diagram.nodes.values() if node.kind == "endEvent"}
    back_ids = {link.id for link in _back_links(diagram)}
    terminal = _terminal_decisions(blueprint)
    terminal_labels = set()
    for decision in terminal:
        if decision.yes_end:
            terminal_labels.add((decision.yes_label or "").strip())
        no_ends = decision.no_end or decision.else_end or (
            decision.flow == FlowType.TERMINATION and not decision.yes_end
        )
        if no_ends:
            terminal_labels.add((decision.no_label or "").strip())
    xor_to_end = [
        link for link in diagram.links
        if link.target_id in end_ids
        and (link.condition_name or "").strip() in terminal_labels
        and diagram.nodes.get(link.source_id) is not None
        and diagram.nodes[link.source_id].kind == "exclusiveGateway"
    ]
    if len(xor_to_end) != len(terminal):
        gaps.append(f"веток завершения {len(xor_to_end)}, в чертеже {len(terminal)}")
    if any(link.id in back_ids for link in xor_to_end):
        gaps.append("termination записан циклом")
    splits = [
        node for node in diagram.nodes.values()
        if node.kind == "parallelGateway"
        and sum(1 for link in diagram.links if link.source_id == node.id) >= 2
    ]
    if len(splits) != len(blueprint.parallel_blocks):
        gaps.append(f"параллельных блоков {len(splits)}, в чертеже {len(blueprint.parallel_blocks)}")
    by_num = {task.num: task for task in blueprint.tasks}
    for block in blueprint.parallel_blocks:
        members = [_find_node(diagram, by_num[num].title) for num in block.nums if num in by_num]
        if any(node is None for node in members) or not members:
            gaps.append(f"параллельный блок {block.nums} не собран")
            continue
        held = False
        for split in splits:
            outs = {link.target_id for link in diagram.links if link.source_id == split.id}
            if not all(node.id in outs for node in members):
                continue
            joins = set()
            for node in members:
                for link in diagram.links:
                    if link.source_id != node.id:
                        continue
                    target = diagram.nodes.get(link.target_id)
                    if target is not None and target.kind == "parallelGateway":
                        joins.add(target.id)
            held = len(joins) == 1
            if held:
                break
        if not held:
            gaps.append(f"параллельный блок {block.nums} не является AND split/join")
    expected = [task.num for task in blueprint.tasks]
    actual = _success_nums(diagram, blueprint)
    if actual != expected:
        gaps.append(f"порядок шагов {actual} вместо {expected}")
    return gaps


def xml_contract_gaps(xml: str, blueprint: ProcessBlueprint) -> List[str]:
    """Каркас BPMN 2.0, который открывают bpmn.io и Camunda Modeler."""
    gaps: List[str] = []
    body = xml or ""
    if "http://www.omg.org/spec/BPMN/20100524/MODEL" not in body:
        gaps.append("нет пространства имён BPMN 2.0")
    if "http://www.omg.org/spec/BPMN/20100524/DI" not in body:
        gaps.append("нет пространства имён BPMNDI")
    for token, label in (
        ("bpmn:collaboration", "Collaboration"),
        ("bpmn:participant", "Participant"),
        ("bpmn:laneSet", "LaneSet"),
        ("bpmn:sequenceFlow", "SequenceFlow"),
        ("bpmndi:BPMNShape", "BPMNShape"),
        ("bpmndi:BPMNEdge", "BPMNEdge"),
    ):
        if token not in body:
            gaps.append(f"нет {label}")
    if "bpmn:userTask" not in body and "bpmn:scriptTask" not in body and "bpmn:task" not in body:
        gaps.append("нет задач")
    if blueprint.decisions and "bpmn:exclusiveGateway" not in body:
        gaps.append("нет ExclusiveGateway")
    if blueprint.parallel_blocks and "bpmn:parallelGateway" not in body:
        gaps.append("нет ParallelGateway")
    return gaps


def _exclusive_after(diagram: Any, check: Any) -> Any:
    outs = [link for link in diagram.links if link.source_id == check.id]
    if len(outs) != 1:
        return None
    nxt = diagram.nodes.get(outs[0].target_id)
    if nxt is not None and nxt.kind == "exclusiveGateway":
        return nxt
    return None


def _split_check(diagram: Any, check: Any, item: BlueprintRework) -> Any:
    """Вставляет XOR на единственный выход проверки. Саму задачу, роль и срок не меняет."""
    outs = [link for link in diagram.links if link.source_id == check.id]
    if len(outs) != 1:
        return None
    parent = check.lane_id or check.parent_id or check.owner_id
    gate_id = diagram.add_exclusive_gateway(item.gateway_name or "Результат принят?", parent)
    gate = diagram.nodes.get(gate_id)
    if gate is None:
        return None
    gate.sla_hours = 0.0
    forward = outs[0]
    saved_source, saved_label = forward.source_id, forward.condition_name
    forward.source_id = gate_id
    if not (forward.condition_name or "").strip():
        forward.condition_name = item.success_label or "Дальше"
    if not diagram.add_link(check.id, gate_id):
        forward.source_id, forward.condition_name = saved_source, saved_label
        diagram.nodes.pop(gate_id, None)
        return None
    return gate


def _reuse_fix(diagram: Any, gate: Any, item: BlueprintRework) -> Any:
    """Задача, которая уже стоит на ветке ошибки этого шлюза. Новую вместо неё не создаём."""
    wanted = (item.condition or "").strip().lower()
    for link in diagram.links:
        if link.source_id != gate.id:
            continue
        label = (link.condition_name or "").strip().lower()
        if wanted and wanted not in label and label not in wanted:
            continue
        node = diagram.nodes.get(link.target_id)
        if node is None or node.kind in (
            "exclusiveGateway", "parallelGateway", "inclusiveGateway", "startEvent", "endEvent",
        ):
            continue
        return node
    return None


def _place_fix(diagram: Any, item: BlueprintRework, check: Any, target: Any) -> Any:
    parent = target.lane_id or check.lane_id or target.parent_id
    owner = diagram.nodes.get(target.owner_id)
    if owner is not None and owner.kind == "subProcess":
        parent = owner.id
    fix_id = diagram.add_user_task(item.fix_task or "Устранить замечания", parent)
    fix = diagram.nodes.get(fix_id)
    if fix is not None and fix.sla_hours is None:
        fix.sla_hours = 0.0
    return fix


def _ensure_rollback(diagram: Any, item: BlueprintRework, check: Any, target: Any) -> int:
    """XOR и ветка на уже существующий шаг. Задачу исправления не создаёт."""
    gate = _exclusive_after(diagram, check)
    if gate is None:
        gate = _split_check(diagram, check, item)
    if gate is None:
        return 0
    if any(link.source_id == gate.id and link.target_id == target.id for link in diagram.links):
        for link in diagram.links:
            if link.source_id == gate.id and link.target_id == target.id and not (link.condition_name or "").strip():
                link.condition_name = item.condition or "Замечания"
        return 0
    linked = diagram.add_link(gate.id, target.id, item.condition or "Замечания")
    return 1 if linked else 0


def _ensure_correction(diagram: Any, item: BlueprintRework, check: Any) -> int:
    """Недостающие XOR, задачу исправления и ребро fix → check."""
    gate = _exclusive_after(diagram, check)
    if gate is None:
        gate = _split_check(diagram, check, item)
    if gate is None:
        return 0
    fix = _find_node(diagram, item.fix_task) if item.fix_task else None
    if fix is None or fix.id in (check.id, gate.id):
        fix = _reuse_fix(diagram, gate, item)
    created = False
    if fix is None or fix.id in (check.id, gate.id):
        fix = _place_fix(diagram, item, check, check)
        created = fix is not None
    if fix is None or fix.id == check.id:
        return 0
    branch = [link for link in diagram.links if link.source_id == gate.id and link.target_id == fix.id]
    if branch:
        if not (branch[0].condition_name or "").strip():
            branch[0].condition_name = item.condition or "Замечания"
    elif not diagram.add_link(gate.id, fix.id, item.condition or "Замечания"):
        return 0
    if any(link.source_id == fix.id and link.target_id == check.id for link in diagram.links):
        return 1 if created else 0
    linked = diagram.add_link(fix.id, check.id)
    return 1 if linked else 0


def _freeze_nodes(diagram: Any) -> Dict[str, tuple]:
    return {
        node.id: (node.name, node.lane_id, node.sla_hours, getattr(node, "sla_source", None))
        for node in diagram.nodes.values()
    }


def _restore_nodes(diagram: Any, frozen: Dict[str, tuple]) -> None:
    for node_id, signature in frozen.items():
        node = diagram.nodes.get(node_id)
        if node is None:
            continue
        node.name, node.lane_id, node.sla_hours, source = signature
        if source is not None:
            node.sla_source = source


def normalize_rollbacks(diagram: Any, blueprint: ProcessBlueprint) -> int:
    """Достраивает откат по чертежу. Текст не читает, задачу исправления не создаёт."""
    items = [item for item in blueprint.reworks if item.type == FlowType.ROLLBACK]
    if not items:
        return 0
    before = set(diagram.nodes)
    frozen = _freeze_nodes(diagram)
    added = 0
    for item in items:
        if rollback_cycle_holds(diagram, item):
            continue
        check = _find_node(diagram, item.check_task)
        target = _find_node(diagram, item.target_task)
        if check is None or target is None or check.id == target.id:
            continue
        added += _ensure_rollback(diagram, item, check, target)
    _restore_nodes(diagram, frozen)
    for node_id in list(diagram.nodes):
        if node_id in before:
            continue
        node = diagram.nodes[node_id]
        if node.kind not in ("exclusiveGateway", "inclusiveGateway"):
            diagram.nodes.pop(node_id, None)
            diagram.links[:] = [
                link for link in diagram.links
                if link.source_id != node_id and link.target_id != node_id
            ]
    return added


def normalize_corrections(diagram: Any, blueprint: ProcessBlueprint) -> int:
    """Достраивает исправление по чертежу. Текст не читает. Чужие задачи, роли и сроки не меняет."""
    items = [item for item in blueprint.reworks if item.type == FlowType.CORRECTION]
    if not items:
        return 0
    frozen = _freeze_nodes(diagram)
    added = 0
    for item in items:
        if correction_cycle_holds(diagram, item):
            continue
        check = _find_node(diagram, item.check_task)
        if check is None:
            continue
        added += _ensure_correction(diagram, item, check)
    _restore_nodes(diagram, frozen)
    return added


def apply_blueprint(diagram: Any, blueprint: ProcessBlueprint) -> int:
    """Единственный вход нормализатора. На входе уже готовый чертёж, не регламент."""
    return normalize_rollbacks(diagram, blueprint) + normalize_corrections(diagram, blueprint)


def normalize_reworks(diagram: Any, regulation_text: str) -> int:
    """Граница совместимости: текст разбирает analyze_process, нормализатор видит только чертёж."""
    if not (regulation_text or "").strip():
        return 0
    return apply_blueprint(diagram, analyze_process(regulation_text))


def blueprint_invariants(blueprint: ProcessBlueprint) -> List[str]:
    """Нарушения модели чертежа. Пустой список — чертёж допустим."""
    gaps: List[str] = []
    nums = [task.num for task in blueprint.tasks]
    if nums != sorted(nums):
        gaps.append("порядок шагов нарушен")
    seen = set()
    for task in blueprint.tasks:
        if not (task.role or "").strip():
            gaps.append(f"шаг {task.num} без роли")
        key = (task.num, task.title)
        if key in seen:
            gaps.append(f"дубликат шага {task.num}")
        seen.add(key)
    rework_checks = {item.check_num for item in blueprint.reworks}
    for decision in blueprint.decisions:
        if decision.kind == "end" and decision.num in rework_checks:
            gaps.append(f"завершение на шаге {decision.num} записано как цикл")
    for item in blueprint.reworks:
        if item.type == FlowType.TERMINATION:
            gaps.append("termination попал в список возвратов")
        if item.type == FlowType.ROLLBACK:
            if item.fix_task:
                gaps.append(f"rollback на шаге {item.check_num} содержит задачу исправления")
            if item.target_num is None or item.check_num is None or item.target_num >= item.check_num:
                gaps.append(f"rollback на шаге {item.check_num} смотрит не на предыдущий шаг")
            if not item.target_task:
                gaps.append(f"rollback на шаге {item.check_num} без целевой задачи")
        if item.type == FlowType.CORRECTION and not item.fix_task:
            gaps.append(f"correction на шаге {item.check_num} без задачи исправления")
    return gaps


def repair_missing_rework_edges(diagram: Any, regulation_text: str) -> int:
    """Совместимое имя: нормализует цикл, а не только дорисовывает fix → check."""
    return normalize_reworks(diagram, regulation_text)
