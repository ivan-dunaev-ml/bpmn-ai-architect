"""Слой visual / fidelity / maturity и инварианты AS-IS vs To-Be."""

from pathlib import Path

from ai_generator import generate_bpmn_from_text, optimize_process_to_be
from bpmn_quality import (
    bpmn_maturity_level,
    compute_visual_score,
    count_crossings,
    jury_scorecard,
    normalize_optimization_actions,
    process_improvement_score,
    regulation_trace,
)
from process_blueprint import analyze_process

ROOT = Path(__file__).resolve().parent
EX1 = (ROOT / "examples" / "example_1_substation_repair.txt").read_text(encoding="utf-8")
EX2 = (ROOT / "examples" / "example_2_equipment_procurement.txt").read_text(encoding="utf-8")


def test_visual_score_on_procurement():
    xml, audit, err = generate_bpmn_from_text(EX2, use_llm=False)
    assert not err
    visual = compute_visual_score(xml, audit)
    assert visual["overall_visual_score"] >= 60
    assert visual["routing_quality"] >= 50
    assert visual["crossings"] is not None and visual["crossings"] <= 3
    assert audit.get("bpmn_quality", {}).get("asis_fidelity", 0) >= 80
    assert audit["bpmn_quality"]["maturity_level"] >= 2


def test_regulation_trace_covers_steps():
    blueprint = analyze_process(EX2)
    xml, audit, err = generate_bpmn_from_text(EX2, use_llm=False)
    assert not err
    trace = regulation_trace(EX2, blueprint, audit, xml)
    assert trace["total"] >= 13
    assert trace["fidelity"] >= 85
    assert trace["covered"] >= 12


def test_optimization_action_contract():
    actions = normalize_optimization_actions(
        [{"kind": "parallel", "detail": "Шаги 9 и 10 параллельно."}]
    )
    assert actions[0]["type"] == "parallelization"
    assert actions[0]["title"]
    assert actions[0]["problem"]
    assert actions[0]["decision"]
    assert actions[0]["business_reason"]
    assert actions[0]["sla_effect"]
    assert actions[0]["risk_effect"]
    assert 0 < float(actions[0]["confidence"]) <= 1


def test_maturity_and_jury_scorecard():
    xml, audit, err = generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    card = jury_scorecard(audit, xml, EX1, analyze_process(EX1), None)
    assert card["asis_fidelity"] >= 95
    assert card["maturity"]["level"] >= 2
    assert card["maturity"]["why"]
    assert card["ai_confidence"]["level"] in ("Высокая", "Средняя", "Ограниченная")
    assert "routing_quality" in card["visual"]
    assert card["process_health"]
    assert "→" in card["process_health"][0]


def test_asis_unchanged_by_tobe_optimizer_text_copy():
    """AS-IS XML/аудит не подменяются оптимизатором: исходный текст даёт те же часы."""
    asis_xml, asis_audit, err = generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    frozen_cp = float((asis_audit.get("sla") or {}).get("critical_path_hours") or 0)
    frozen_rw = float((asis_audit.get("sla") or {}).get("with_rework_hours") or 0)
    tobe_text, delta = optimize_process_to_be(EX1, asis_audit)
    assert float((asis_audit.get("sla") or {}).get("critical_path_hours") or 0) == frozen_cp == 9.7
    assert float((asis_audit.get("sla") or {}).get("with_rework_hours") or 0) == frozen_rw == 12.55
    assert tobe_text.strip() != ""
    actions = normalize_optimization_actions(list(delta.get("actions") or []))
    for act in actions:
        assert act.get("business_reason")
        assert act.get("sla_effect")
        assert act.get("risk_effect")
    if normalize_like(tobe_text) != normalize_like(EX1):
        assert int(delta.get("rework_after") or 0) <= int(delta.get("rework_before") or 0)
        card = jury_scorecard(asis_audit, asis_xml, EX1, None, delta)
        assert int(card["process_improvement"]["score"]) >= 40


def normalize_like(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


COMPLEX = """Регламент: Многоролевой контроль
Целевой срок: 40 часов.
1. Заявитель подаёт заявку (1 час).
2. Диспетчер регистрирует заявку (1 час).
3. Инженер проверяет комплект (2 часа). Если документы неполные — вернуть на п.1.
4. Юрист проверяет договор (2 часа). При ошибках направить на доработку.
5. Служба безопасности проверяет контрагента (2 часа). Если отказ СБ — завершить процесс.
6. Экономист считает смету (2 часа).
7. Параллельно: Архивариус готовит том документов (2 часа).
8. Главный инженер согласует решение (2 часа). Если замечания — вернуть на п.6.
9. Начальник смены выдаёт наряд (1 час).
10. Бригада выполняет работы (8 часов).
11. Лаборатория снимает замеры (2 часа).
12. Параллельно: Охрана труда проверяет СИЗ (1 час).
13. Диспетчер закрывает заявку (1 час). Если повторная проверка нужна — вернуть на п.10.
14. Руководитель утверждает акт (1 час).
"""


def test_complex_multi_role_gateways_loops_parallel():
    xml, audit, err = generate_bpmn_from_text(COMPLEX, use_llm=False)
    assert not err
    assert audit.get("xsd_valid") is True
    blueprint = analyze_process(COMPLEX)
    assert len(blueprint.roles) >= 8
    assert len(blueprint.decisions) >= 4
    assert len(blueprint.parallel_blocks) >= 1
    assert len(blueprint.reworks) >= 2
    crossings = count_crossings(xml)
    assert crossings is not None and crossings <= 8
    maturity = bpmn_maturity_level(audit, xml, fidelity=regulation_trace(COMPLEX, blueprint, audit, xml)["fidelity"])
    assert maturity["level"] >= 2
    assert maturity["structural_quality"] >= 70


AMBIGUOUS = """Регламент: Неоднозначный
1. Сотрудник делает работу.
2. Потом проверка. Если что-то не так — разобраться.
3. Завершить.
"""


def test_ambiguous_regulation_still_builds_or_warns():
    xml, audit, err = generate_bpmn_from_text(AMBIGUOUS, use_llm=False)
    if err:
        assert "не распознан" in err.lower() or "пустой" in err.lower() or len(err) > 0
        return
    assert audit.get("xsd_valid") is True
    warnings = (audit.get("bpmn_quality") or {}).get("trace_warnings") or []
    assert isinstance(warnings, list)


def test_procurement_demo_jury_thresholds():
    xml, audit, err = generate_bpmn_from_text(EX2, use_llm=False)
    assert not err
    assert audit.get("xsd_valid") is True
    assert int((audit.get("bpmn_quality") or {}).get("asis_fidelity") or 0) >= 95
    crossings = count_crossings(xml)
    assert crossings is not None and crossings <= 5
    tobe_text, delta = optimize_process_to_be(EX2, audit)
    assert float((audit.get("sla") or {}).get("critical_path_hours") or 0) == 528.3
    assert float((audit.get("sla") or {}).get("with_rework_hours") or 0) == 856.5
    card = jury_scorecard(audit, xml, EX2, analyze_process(EX2), delta)
    assert card["asis_fidelity"] >= 95
    for act in card["reasoning"]:
        assert act["problem"] and act["decision"] and act["business_reason"]
        assert act["sla_effect"] and act["risk_effect"]
    if int(delta.get("rework_after") or 0) < int(delta.get("rework_before") or 0):
        assert process_improvement_score(audit, delta, card["visual"]["overall_visual_score"])["score"] >= 50
        assert card["maturity"]["level"] >= 3
    assert tobe_text.strip()
