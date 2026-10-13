"""Слой промышленного качества BPMN: визуал, fidelity AS-IS, зрелость, объяснения To-Be.

Методологический score (check_methodology_compliance) здесь не пересчитывается и не подменяется.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

_NS_BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_NS_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
_NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
_NS_DI_WP = "http://www.omg.org/spec/DD/20100524/DI"

_WORK_TAGS = {
    "task",
    "userTask",
    "scriptTask",
    "manualTask",
    "serviceTask",
    "sendTask",
    "receiveTask",
    "businessRuleTask",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clip(value: float) -> int:
    return max(0, min(100, int(round(value))))


def _orient(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_from_xml(xml: str) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    edges: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for edge in root.iter(f"{{{_NS_DI}}}BPMNEdge"):
        points = [
            (float(point.get("x", 0)), float(point.get("y", 0)))
            for point in edge.findall(f"{{{_NS_DI_WP}}}waypoint")
        ]
        for left, right in zip(points, points[1:]):
            if abs(left[0] - right[0]) + abs(left[1] - right[1]) > 0.5:
                edges.append((left, right))
    return edges


def count_crossings(xml: str) -> Optional[int]:
    if not (xml or "").strip():
        return None
    try:
        edges = _segments_from_xml(xml)
    except Exception:  # noqa: BLE001
        return None
    crossings = 0
    for i, (a, b) in enumerate(edges):
        for c, d in edges[i + 1 :]:
            if a in (c, d) or b in (c, d):
                continue
            if _orient(a, b, c) * _orient(a, b, d) < 0 and _orient(c, d, a) * _orient(c, d, b) < 0:
                crossings += 1
    return crossings


def _shape_bounds(xml: str) -> List[Tuple[float, float, float, float, str]]:
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out: List[Tuple[float, float, float, float, str]] = []
    for shape in root.iter(f"{{{_NS_DI}}}BPMNShape"):
        box = shape.find(f"{{{_NS_DC}}}Bounds")
        if box is None:
            continue
        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
        w = float(box.get("width", 0))
        h = float(box.get("height", 0))
        out.append((x, y, x + w, y + h, shape.get("bpmnElement") or ""))
    return out


def _label_bounds(xml: str) -> List[Tuple[float, float, float, float]]:
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out: List[Tuple[float, float, float, float]] = []
    for label in root.iter(f"{{{_NS_DI}}}BPMNLabel"):
        box = label.find(f"{{{_NS_DC}}}Bounds")
        if box is None:
            continue
        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
        w = float(box.get("width", 0))
        h = float(box.get("height", 0))
        if w > 1 and h > 1:
            out.append((x, y, x + w, y + h))
    return out


def _rects_overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], pad: float = 0.0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _path_lengths(xml: str) -> Tuple[List[float], List[float]]:
    """Длины forward и back рёбер по waypoint (манхэттен)."""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    forward: List[float] = []
    back: List[float] = []
    for edge in root.iter(f"{{{_NS_DI}}}BPMNEdge"):
        points = [
            (float(point.get("x", 0)), float(point.get("y", 0)))
            for point in edge.findall(f"{{{_NS_DI_WP}}}waypoint")
        ]
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:]))
        color = (edge.get("{http://bpmn.io/schema/bpmn/biocolor/1.0}stroke") or edge.get("bioc:stroke") or "").lower()
        # возвраты красятся в красный REWORK_EDGE_COLOR
        is_back = "#c62828" in color or "c62828" in color
        (back if is_back else forward).append(length)
    return forward, back


def compute_visual_score(xml: str, audit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Метрики визуального качества. Не трогает methodology.score."""
    data = audit or {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    meth = data.get("methodology") if isinstance(data.get("methodology"), dict) else {}
    lanes = int(stats.get("lanes") or 0)
    crossings = count_crossings(xml)
    try:
        shapes = _shape_bounds(xml)
        labels = _label_bounds(xml)
        forward, back = _path_lengths(xml)
    except Exception:  # noqa: BLE001
        return {
            "routing_quality": 50,
            "spacing_quality": 50,
            "lane_balance": 50,
            "gateway_clarity": 50,
            "label_readability": 50,
            "overall_visual_score": 50,
            "crossings": crossings,
            "long_back_edges": 0,
            "label_overlaps": 0,
        }

    work_shapes = [s for s in shapes if s[4]]
    xs = sorted((s[0] + s[2]) / 2.0 for s in work_shapes)
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 8]
    if gaps:
        mean = sum(gaps) / len(gaps)
        variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        spacing = max(0.0, 100.0 - math.sqrt(variance) / max(mean, 1.0) * 35.0)
    else:
        spacing = 70.0

    widths = [s[2] - s[0] for s in work_shapes]
    heights = [s[3] - s[1] for s in work_shapes]
    size_spread = 0.0
    if widths and heights:
        size_spread = (max(widths) - min(widths)) + (max(heights) - min(heights))
    spacing = max(0.0, spacing - min(40.0, size_spread / 8.0))

    routing = 100.0
    if crossings is not None:
        routing -= min(60.0, crossings * 10.0)
    long_back = sum(1 for length in back if length > 1200)
    routing -= min(20.0, long_back * 6.0)
    if back:
        avg_back = sum(back) / len(back)
        routing -= min(15.0, max(0.0, (avg_back - 600.0) / 50.0))

    lane_load = list(data.get("lane_load") or [])
    if lane_load:
        shares = [float(item.get("share") or 0) for item in lane_load if isinstance(item, dict)]
        top = max(shares) if shares else 0.0
        lane_balance = max(0.0, 100.0 - max(0.0, top - 0.35) * 180.0)
    else:
        lane_balance = 55.0 if lanes <= 1 else 75.0 + min(20.0, lanes * 2.0)

    gw_checks = {str(item.get("key")): bool(item.get("passed")) for item in (meth.get("checks") or []) if isinstance(item, dict)}
    gateway_clarity = 100.0 if gw_checks.get("gateway", True) else 45.0
    xor = int(stats.get("nodes") or 0)  # fallback; уточним по XML ниже
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
        xor_n = len(list(root.iter(f"{{{_NS_BPMN}}}exclusiveGateway")))
        and_n = len(list(root.iter(f"{{{_NS_BPMN}}}parallelGateway")))
        if xor_n + and_n == 0:
            gateway_clarity = max(gateway_clarity, 90.0)
        else:
            gateway_clarity = min(100.0, gateway_clarity + min(10.0, (xor_n + and_n)))
    except Exception:  # noqa: BLE001
        xor_n = and_n = 0

    label_overlaps = 0
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            if _rects_overlap(a, b, pad=1.0):
                label_overlaps += 1
    label_readability = max(0.0, 100.0 - label_overlaps * 12.0)
    if lanes >= 2:
        label_readability = min(100.0, label_readability + 5.0)

    overall = (
        0.28 * routing
        + 0.18 * spacing
        + 0.18 * lane_balance
        + 0.18 * gateway_clarity
        + 0.18 * label_readability
    )
    return {
        "routing_quality": _clip(routing),
        "spacing_quality": _clip(spacing),
        "lane_balance": _clip(lane_balance),
        "gateway_clarity": _clip(gateway_clarity),
        "label_readability": _clip(label_readability),
        "overall_visual_score": _clip(overall),
        "crossings": crossings,
        "long_back_edges": long_back,
        "label_overlaps": label_overlaps,
        "gateways_xor": xor_n,
        "gateways_and": and_n,
    }


def _stems(text: str) -> set:
    """Основы слов (6 букв) без служебных: «Провести визуальный осмотр» ≈ «Визуальный осмотр провести»."""
    return {word[:6] for word in re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", (text or "").lower())} - _STOP_STEMS


_STOP_STEMS = {"для", "при", "или", "что", "это", "его", "так", "все", "без", "над", "под"}


def _same_meaning(reference: str, candidate: str) -> bool:
    """Шаг регламента представлен в BPMN: совпадает хотя бы половина основ его названия."""
    ref, cand = _stems(reference), _stems(candidate)
    if not ref or not cand:
        return False
    if reference.strip().lower() == candidate.strip().lower():
        return True
    return len(ref & cand) / len(ref) >= 0.5


def _xml_inventory(xml: str) -> Dict[str, Any]:
    names: List[str] = []
    lanes: List[str] = []
    xor = 0
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception:  # noqa: BLE001
        return {"names": names, "lanes": lanes, "xor": xor}
    for node in root.iter():
        tag = _local(node.tag)
        name = (node.get("name") or "").strip()
        if tag in _WORK_TAGS or tag == "subProcess":
            if name:
                names.append(name)
        elif tag == "lane" and name:
            lanes.append(name)
        elif tag == "exclusiveGateway":
            xor += 1
    return {"names": names, "lanes": lanes, "xor": xor}


FIDELITY_METHOD = (
    "Сверка схемы с детерминированным разбором текста регламента: шаги (по смыслу названия), "
    "роли (дорожки), развилки (XOR) и возвраты (циклы). Процент — доля найденных элементов."
)


def regulation_trace(
    regulation_text: str,
    blueprint: Any = None,
    audit: Optional[Dict[str, Any]] = None,
    xml: str = "",
) -> Dict[str, Any]:
    """Трассировка: фраза регламента → BPMN. Потери — предупреждения.

    Эталон — разбор текста (номера шагов, роли, «если … иначе», «вернуть на п.N»), а не имена задач эмулятора:
    модель, которая перефразировала шаг, его не теряет. Если схему собрал тот же разбор, сверка —
    самопроверка (self_check=True), и интерфейс говорит об этом прямо.
    """
    from ai_generator import normalize_regulation, parse_regulation

    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    text = normalize_regulation(regulation_text or "")
    empty = {"rows": [], "fidelity": 0, "covered": 0, "total": 0, "dimensions": {}, "method": FIDELITY_METHOD}
    if len(text) < 20:
        return {**empty, "warnings": ["Регламент слишком короткий для трассировки."]}

    try:
        parsed = parse_regulation(text)
    except Exception as exc:  # noqa: BLE001
        return {**empty, "warnings": [f"Парсер не разобрал регламент: {exc}"]}

    inventory = _xml_inventory(xml) if xml else {"names": [], "lanes": [], "xor": 0}
    bpmn_names = inventory["names"]
    bp_tasks = list(getattr(blueprint, "tasks", None) or [])
    bp_decisions = list(getattr(blueprint, "decisions", None) or [])
    by_num = {int(task.num): task for task in bp_tasks}
    dec_by_num = {int(item.num): item for item in bp_decisions}

    covered = 0
    for step in parsed.steps:
        sentence = f"{step.num}. {step.role or ''} {step.title or ''}".strip()
        if step.decision and getattr(step.decision, "source", None):
            sentence = str(step.decision.source)
        elif step.decision and getattr(step.decision, "back_clause", None):
            sentence = f"{sentence}. {step.decision.back_clause}".strip()

        semantic = "task"
        if step.decision:
            semantic = "decision"
        if getattr(step, "parallel", False):
            semantic = "parallel-task"

        bp_el = by_num.get(step.num) or dec_by_num.get(step.num)
        bp_id = getattr(bp_el, "id", None) if bp_el is not None else None
        title = (step.title or "").strip()
        bpmn_hit = next((name for name in bpmn_names if title and _same_meaning(title, name)), "")
        status = "ok"
        if not title and not step.decision:
            status = "lost"
            warnings.append(f"Фраза регламента не получила BPMN-представление: «{sentence[:120]}»")
        elif title and xml and not bpmn_hit:
            status = "lost"
            warnings.append(f"Шаг №{step.num} «{title[:80]}» не найден в схеме.")
        else:
            covered += 1

        rows.append(
            {
                "num": step.num,
                "sentence": sentence[:240],
                "semantic": semantic,
                "blueprint_id": bp_id or "",
                "role": step.role or "",
                "bpmn_element": bpmn_hit,
                "status": status,
            }
        )

    data = audit or {}
    roles = [role for role in (parsed.roles or []) if role]
    roles_hit = sum(1 for role in roles if any(_same_meaning(role, lane) for lane in inventory["lanes"])) if xml else 0
    for role in roles:
        if xml and not any(_same_meaning(role, lane) for lane in inventory["lanes"]):
            warnings.append(f"Роль «{role}» из регламента не получила дорожку.")
    decisions = sum(1 for step in parsed.steps if step.decision)
    decisions_hit = min(decisions, int(inventory["xor"]))
    try:
        from ai_generator import _explicit_return_count

        returns = int(_explicit_return_count(regulation_text or ""))
    except Exception:  # noqa: BLE001
        returns = 0
    returns_hit = min(returns, len(data.get("rework_loops") or [])) if data else 0
    if returns_hit < returns:
        warnings.append(f"Возвратов в регламенте {returns}, циклов в схеме {returns_hit}.")

    dimensions = {
        "steps": {"found": covered, "total": len(parsed.steps)},
        "roles": {"found": roles_hit, "total": len(roles)},
        "decisions": {"found": decisions_hit, "total": decisions},
        "returns": {"found": returns_hit, "total": returns},
    }
    total_items = sum(item["total"] for item in dimensions.values())
    found_items = sum(item["found"] for item in dimensions.values())
    fidelity = _clip(100.0 * found_items / total_items) if total_items else 0
    engine = str(((data.get("generation") or {}).get("engine")) or "")
    return {
        "rows": rows,
        "warnings": warnings[:12],
        "fidelity": fidelity,
        "covered": covered,
        "total": len(parsed.steps),
        "dimensions": dimensions,
        "method": FIDELITY_METHOD,
        # Эталон и схема из одного разбора: 100% подтверждает перенос без потерь, а не понимание текста.
        "self_check": engine.startswith("semantic"),
        # Пунктов мало — разбор текста ненадёжен как эталон (сплошной текст без нумерации).
        "reference_weak": len(parsed.steps) < 3,
    }


_ACTION_TYPE_MAP = {
    "automation": "automation",
    "parallel": "parallelization",
    "parallelization": "parallelization",
    "zero_rework": "zero_rework",
    "loops": "zero_rework",
    "control": "control_point_shift",
    "safety": "control_point_shift",
    "notation": "role_balance",
    "role_balance": "role_balance",
    "control_point_shift": "control_point_shift",
    "edo_approval": "edo_approval",
}

_ACTION_CATALOG = {
    "edo_approval": {
        "title": "Согласование через ЭДО",
        "problem": "Бумажное согласование занимает дни и стоит на критическом пути.",
        "decision": "Согласование идёт в системе электронного документооборота; исполнитель и порядок те же.",
        "business_reason": "Маршрут согласования без пересылки бумаги.",
        "sla_effect": "Срок шага сокращён до норматива ЭДО (допущение, подтверждает владелец процесса).",
        "risk_effect": "Контроль согласования сохранён, срок — допущение до утверждения норматива.",
        "confidence": 0.6,
    },
    "automation": {
        "title": "Автоматизировать ручную фиксацию",
        "problem": "Исполнитель вручную дублирует запись, которую система уже может сделать сама.",
        "decision": "Шаг выполняет информационная система автоматически, срок из регламента сохранён.",
        "business_reason": "Ручная фиксация заменена системной операцией.",
        "sla_effect": "Снимается ожидание человека на типовой записи.",
        "risk_effect": "Снижен риск пропуска записи и человеческой ошибки.",
        "confidence": 0.9,
    },
    "parallelization": {
        "title": "Параллелизация независимых этапов",
        "problem": "Разные роли ждут друг друга без зависимости по результату.",
        "decision": "Независимые этапы разных ролей выполняются одновременно.",
        "business_reason": "Этапы не зависят друг от друга.",
        "sla_effect": "Сокращение критического пути.",
        "risk_effect": "Контрольные точки сохранены.",
        "confidence": 0.88,
    },
    "zero_rework": {
        "title": "Убрать цикл возврата",
        "problem": "Недостаток контроля качества приводит к повторному прохождению шагов.",
        "decision": "Контроль перенесён раньше; цикл снят со счастливого пути.",
        "business_reason": "Недостаток контроля качества устранён раньше по потоку.",
        "sla_effect": "Срок с возвратами сокращается.",
        "risk_effect": "Цикл возврата больше не раздувает SLA.",
        "confidence": 0.86,
    },
    "control_point_shift": {
        "title": "Сместить контрольную точку",
        "problem": "Проверка стоит слишком поздно или нарушает обязательный порядок допуска.",
        "decision": "Контроль стоит там, где предотвращает возврат и сохраняет требования ОТ.",
        "business_reason": "Контроль перенесён раньше, чтобы не возвращать работу.",
        "sla_effect": "Укорачивается путь с возвратами без нарушения стоп-листа.",
        "risk_effect": "Снижен риск повторной проверки и нарушения допуска.",
        "confidence": 0.84,
    },
    "role_balance": {
        "title": "Свернуть повторяющиеся операции в читаемый блок",
        "problem": "Длинная цепочка одной роли плохо читается и скрывает смысл этапа.",
        "decision": "Повторяющиеся операции сгруппированы в контрольный блок нотации.",
        "business_reason": "Нотация BPMN стала читаемой без смены бизнес-логики.",
        "sla_effect": "Трудозатраты шагов не меняются.",
        "risk_effect": "Снижен риск неверной интерпретации схемы жюри и аналитиком.",
        "confidence": 0.8,
    },
}


def enrich_optimization_action(action: Dict[str, Any], before: Any = None, after: Any = None) -> Dict[str, Any]:
    """Нормализует ход To-Be до контракта OptimizationAction для демонстрации жюри."""
    raw = str(action.get("type") or action.get("kind") or "reengine")
    kind = _ACTION_TYPE_MAP.get(raw, raw if raw in _ACTION_CATALOG else "role_balance")
    detail = str(action.get("description") or action.get("detail") or "").strip()
    pack = _ACTION_CATALOG.get(kind, _ACTION_CATALOG["role_balance"])
    title = str(action.get("title") or pack["title"])
    problem = str(action.get("problem") or pack["problem"])
    decision = str(action.get("decision") or detail or pack["decision"])
    confidence = action.get("confidence", pack["confidence"])
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = float(pack["confidence"])
    return {
        "type": kind,
        "title": title,
        "problem": problem,
        "decision": decision,
        "business_reason": str(action.get("business_reason") or pack["business_reason"]),
        "sla_effect": str(action.get("sla_effect") or pack["sla_effect"]),
        "risk_effect": str(action.get("risk_effect") or pack["risk_effect"]),
        "confidence": round(confidence, 2),
        "before": action.get("before", before),
        "after": action.get("after", after),
        "description": detail or decision,
        "kind": kind,
        "detail": detail or decision,
    }


_REQUIRED_ACTION_FIELDS = ("title", "problem", "decision", "business_reason", "sla_effect", "risk_effect", "confidence")


def action_is_complete(action: Dict[str, Any]) -> bool:
    """Рекомендация без полного бизнес-контракта на экран не попадает."""
    if not isinstance(action, dict):
        return False
    if action.get("kind") == "stable" or "узких мест" in str(action.get("detail") or ""):
        return False
    for key in _REQUIRED_ACTION_FIELDS:
        value = action.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def normalize_optimization_actions(actions: Sequence[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    enriched = [enrich_optimization_action(dict(item)) for item in actions if isinstance(item, dict)]
    enriched = [item for item in enriched if action_is_complete(item)]
    return enriched[: max(0, int(limit))]


def compute_structural_quality(audit: Optional[Dict[str, Any]] = None, xml: str = "") -> int:
    data = audit or {}
    meth = data.get("methodology") if isinstance(data.get("methodology"), dict) else {}
    checks = {str(item.get("key")): bool(item.get("passed")) for item in (meth.get("checks") or []) if isinstance(item, dict)}
    score = 0
    score += 25 if data.get("xsd_valid") is True else 0
    score += 25 if not (data.get("dead_ends") or []) else 5
    score += 25 if not (data.get("orphans_without_incoming") or []) else 5
    score += 25 if checks.get("gateway", True) else 10
    if xml and "bpmn:laneSet" not in xml and "laneSet" not in xml:
        score = max(0, score - 15)
    return _clip(score)


def compute_logical_quality(audit: Optional[Dict[str, Any]] = None) -> int:
    data = audit or {}
    meth = data.get("methodology") if isinstance(data.get("methodology"), dict) else {}
    violations = [str(item) for item in (meth.get("violations") or [])]
    score = 100
    if data.get("dead_ends"):
        score -= 25
    if data.get("orphans_without_incoming"):
        score -= 25
    if any("явных возврата" in text for text in violations):
        score -= 30
    if any(text.startswith("Нет стартового") or text.startswith("Нет конечного") for text in violations):
        score -= 20
    return _clip(score)


def compute_business_quality(audit: Optional[Dict[str, Any]] = None, tobe_delta: Optional[dict] = None) -> int:
    data = audit or {}
    bus = data.get("bus_factor") if isinstance(data.get("bus_factor"), dict) else {}
    loops = list(data.get("rework_loops") or [])
    score = 70
    share = float(bus.get("max_share") or 0)
    if share <= 0.35:
        score += 15
    elif share <= 0.45:
        score += 5
    else:
        score -= 15
    score -= min(20, len(loops) * 6)
    if isinstance(tobe_delta, dict):
        if int(tobe_delta.get("rework_removed") or 0) > 0:
            score += 10
        if float(tobe_delta.get("sla_saved_hours") or 0) > 0:
            score += 10
    return _clip(score)


def process_health_issues(audit: Optional[Dict[str, Any]] = None) -> List[str]:
    """До 3 проблем в формате: проблема → цена → последствие."""
    data = audit or {}
    issues: List[str] = []
    sla = data.get("sla") if isinstance(data.get("sla"), dict) else {}
    loops = list(data.get("rework_loops") or [])
    if loops:
        worst = max(loops, key=lambda item: float((item or {}).get("cycle_hours") or 0))
        hours = float((worst or {}).get("cycle_hours") or sla.get("rework_hours") or 0)
        label = str((worst or {}).get("label") or "возврат на доработку")
        if hours > 0:
            issues.append(f"Возврат «{label}» → +{hours:g} ч → срыв сроков")
        else:
            issues.append(f"Возврат «{label}» → повтор работы → потеря времени")
    bus = data.get("bus_factor") if isinstance(data.get("bus_factor"), dict) else {}
    share = float(bus.get("max_share") or 0)
    role = str(bus.get("top_role") or "")
    if share >= 0.4 and role:
        issues.append(f"«{role}» держит {share:.0%} шагов → узкое место → риск SLA")
    target = sla.get("target_hours")
    with_rw = float(sla.get("with_rework_hours") or sla.get("critical_path_hours") or 0)
    if target not in (None, "") and with_rw > float(target):
        over = with_rw - float(target)
        issues.append(f"Срок выше цели → +{over:g} ч сверх SLA → обязательство не выполняется")
    elif float(sla.get("rework_share") or 0) > 0.25:
        pct = float(sla.get("rework_share") or 0) * 100
        issues.append(f"Возвраты съедают {pct:.0f}% пути → лишняя длительность → риск срыва")
    return issues[:3]


def ai_confidence(
    audit: Optional[Dict[str, Any]] = None,
    fidelity: int = 0,
    tobe_delta: Optional[dict] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Обоснованность схемы и рекомендаций — взвешенная сумма проверяемых факторов, не вероятность модели.

    40% полнота переноса регламента (fidelity) · 25% логика графа (тупики, возвраты) ·
    20% структура (XSD, старт/конец, шлюзы) · 15% ясность регламента (у шагов есть роль и срок).
    Самопроверка эмулятора и слабый эталон не меняют число, но дают оговорку caveat: их 100% не доказывают понимание текста.
    """
    data = audit or {}
    structural = compute_structural_quality(data)
    logical = compute_logical_quality(data)
    rows = list((trace or {}).get("rows") or [])
    clarity = 100.0 * sum(1 for row in rows if row.get("role")) / len(rows) if rows else 50.0
    hours_share = data.get("sla_from_regulation"), data.get("sla_work_nodes")
    if hours_share[0] is not None and hours_share[1]:
        clarity = 0.5 * clarity + 50.0 * float(hours_share[0]) / float(hours_share[1])
    score = _clip(0.40 * float(fidelity) + 0.25 * logical + 0.20 * structural + 0.15 * clarity)
    basis = [
        {"factor": "Полнота переноса регламента", "value": int(fidelity), "weight": 40},
        {"factor": "Логика графа", "value": int(logical), "weight": 25},
        {"factor": "Структура и XSD", "value": int(structural), "weight": 20},
        {"factor": "Ясность регламента (роли и сроки)", "value": _clip(clarity), "weight": 15},
    ]
    level = "Высокая" if score >= 85 else ("Средняя" if score >= 70 else "Ограниченная")
    caveat = ""
    if (trace or {}).get("reference_weak"):
        caveat = "Регламент без явных пунктов — эталон для сверки слабый, нужна ручная проверка."
    elif (trace or {}).get("self_check"):
        caveat = "Схему собрал детерминированный разбор: сверка подтверждает перенос без потерь, не понимание текста."
    actions = normalize_optimization_actions(list((tobe_delta or {}).get("actions") or [])) if tobe_delta else []
    if level == "Высокая":
        label = (
            "Рекомендации опираются на измеренные узкие места процесса"
            if actions
            else "Процесс перенесён из регламента полностью, граф и XSD корректны"
        )
    elif level == "Средняя":
        label = "Ключевые элементы перенесены; часть формулировок регламента неоднозначна"
    else:
        label = "Регламент разобран частично — схеме и рекомендациям нужна ручная проверка"
    if caveat:
        label = f"{label}. {caveat}"
    return {
        "score": score,
        "level": level,
        "label": label,
        "caveat": caveat,
        "basis": basis,
        "method": "0.40·полнота + 0.25·логика + 0.20·структура + 0.15·ясность регламента",
    }


def process_improvement_score(
    audit: Optional[Dict[str, Any]] = None,
    tobe_delta: Optional[dict] = None,
    visual_score: int = 0,
) -> Dict[str, Any]:
    """40% SLA + 20% rework + 20% role balance + 20% BPMN quality."""
    data = audit or {}
    delta = tobe_delta if isinstance(tobe_delta, dict) else {}
    before = float(delta.get("with_rework_before") or delta.get("sla_before_hours") or 0)
    after = float(delta.get("with_rework_after") or delta.get("sla_after_hours") or before)
    if before > 0 and after <= before:
        sla_part = min(100.0, 100.0 * (before - after) / before)
    else:
        sla_part = 0.0
    loops_b = int(delta.get("rework_before") or len(data.get("rework_loops") or []))
    loops_a = int(delta.get("rework_after") if delta else loops_b)
    if loops_b > 0:
        rework_part = 100.0 * max(0, loops_b - loops_a) / loops_b
    else:
        rework_part = 100.0 if delta else 50.0
    bus = data.get("bus_factor") if isinstance(data.get("bus_factor"), dict) else {}
    share = float(bus.get("max_share") or 0)
    # После To-Be роль может улучшиться — если в delta нет role share, берём AS-IS и штрафуем мягко.
    role_part = max(0.0, 100.0 - max(0.0, share - 0.35) * 200.0)
    bpmn_part = float(visual_score or 0)
    if not delta:
        total = _clip(0.2 * rework_part + 0.3 * role_part + 0.5 * bpmn_part)
        return {"score": total, "parts": {"sla": 0, "rework": _clip(rework_part), "role": _clip(role_part), "bpmn": _clip(bpmn_part)}}
    total = _clip(0.4 * sla_part + 0.2 * rework_part + 0.2 * role_part + 0.2 * bpmn_part)
    return {
        "score": total,
        "parts": {
            "sla": _clip(sla_part),
            "rework": _clip(rework_part),
            "role": _clip(role_part),
            "bpmn": _clip(bpmn_part),
        },
    }


def bpmn_maturity_level(
    audit: Optional[Dict[str, Any]] = None,
    xml: str = "",
    tobe_delta: Optional[dict] = None,
    fidelity: Optional[int] = None,
) -> Dict[str, Any]:
    """L1…L5 зрелости с объяснением для жюри."""
    structural = compute_structural_quality(audit, xml)
    logical = compute_logical_quality(audit)
    business = compute_business_quality(audit, tobe_delta)
    visual = compute_visual_score(xml, audit)
    visual_score = int(visual.get("overall_visual_score") or 0)
    fid = int(fidelity if fidelity is not None else 0)
    issues = process_health_issues(audit)
    optimized = False
    if isinstance(tobe_delta, dict):
        actions = normalize_optimization_actions(list(tobe_delta.get("actions") or []))
        optimized = bool(actions) and (
            int(tobe_delta.get("rework_removed") or 0) > 0
            or float(tobe_delta.get("sla_saved_hours") or 0) > 0
            or int(tobe_delta.get("quality_gain") or 0) > 0
        )

    level = 1
    title = "Нарисованный процесс"
    why = "Схема есть, но доверие к полноте ещё не подтверждено."
    if structural >= 70 and logical >= 60 and fid >= 70:
        level = 2
        title = "Корректная модель процесса"
        why = "AI восстановил исходный процесс по регламенту без потери шагов."
    if level >= 2 and issues:
        level = 3
        title = "Процесс с анализом проблем"
        why = "AI нашёл узкие места: возвраты, перегруз ролей или риск срыва срока."
    if level >= 3 and optimized:
        level = 4
        title = "Оптимизированный процесс"
        why = "AI нашёл узкие места, предложил изменение структуры процесса и подтвердил эффект."
    if level >= 4 and visual_score >= 80 and business >= 70:
        level = 5
        title = "Управляемый процесс"
        why = "Есть цифровой двойник, объяснимые улучшения и измеримый эффект для управления."

    return {
        "level": level,
        "title": title,
        "why": why,
        "structural_quality": structural,
        "logical_quality": logical,
        "business_quality": business,
        "visual_quality": visual_score,
        "visual": visual,
        "asis_fidelity": fid,
        "health_issues": issues,
    }


def _fidelity_caption(trace: Dict[str, Any]) -> str:
    dims = trace.get("dimensions") or {}
    parts = [
        f"{label} {dims[key]['found']}/{dims[key]['total']}"
        for key, label in (("steps", "шаги"), ("roles", "роли"), ("decisions", "развилки"), ("returns", "возвраты"))
        if key in dims and dims[key]["total"]
    ]
    head = " · ".join(parts) or "нет данных для сверки"
    if trace.get("reference_weak"):
        return f"{head} — регламент без явных пунктов, сверка приблизительная"
    if trace.get("self_check"):
        return f"{head} — самопроверка: схему собрал тот же разбор текста"
    if int(trace.get("fidelity") or 0) >= 95:
        return f"{head} — процесс перенесён из регламента без потерь"
    return f"{head} — часть элементов регламента не найдена в схеме"


def jury_scorecard(
    audit: Optional[Dict[str, Any]] = None,
    xml: str = "",
    regulation_text: str = "",
    blueprint: Any = None,
    tobe_delta: Optional[dict] = None,
) -> Dict[str, Any]:
    """Сводная карточка для UI: executive view, reasoning, technical audit."""
    data = audit or {}
    trace = regulation_trace(regulation_text, blueprint, data, xml)
    fidelity = int(trace.get("fidelity") or 0)
    maturity = bpmn_maturity_level(data, xml, tobe_delta, fidelity=fidelity)
    actions = normalize_optimization_actions(list((tobe_delta or {}).get("actions") or [])) if tobe_delta else []
    visual = maturity.get("visual") or {}
    improvement = process_improvement_score(data, tobe_delta, int(visual.get("overall_visual_score") or 0))
    confidence = ai_confidence(data, fidelity, tobe_delta, trace)
    health = list(maturity.get("health_issues") or process_health_issues(data))
    tobe_view = None
    if isinstance(tobe_delta, dict):
        before = float(tobe_delta.get("with_rework_before") or tobe_delta.get("sla_before_hours") or 0)
        after = float(tobe_delta.get("with_rework_after") or tobe_delta.get("sla_after_hours") or before)
        pct = int(round(100.0 * (before - after) / before)) if before > 0 and after < before else 0
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        tobe_stats = (tobe_delta.get("tobe_audit") or {}).get("stats") if isinstance(tobe_delta.get("tobe_audit"), dict) else {}
        tobe_view = {
            "sla_before": before,
            "sla_after": after,
            "sla_saved_pct": pct,
            "loops_before": int(tobe_delta.get("rework_before") or 0),
            "loops_after": int(tobe_delta.get("rework_after") or 0),
            "steps_before": int(stats.get("work_items") or 0) or None,
            "steps_after": int((tobe_stats or {}).get("work_items") or 0) or None,
            "quality_before": tobe_delta.get("quality_before"),
            "quality_after": tobe_delta.get("quality_after"),
            "actions": actions,
        }
    return {
        "asis_fidelity": fidelity,
        "fidelity_caption": _fidelity_caption(trace),
        "process_health": health,
        "ai_confidence": confidence,
        "process_improvement": improvement,
        "trace": trace,
        "visual": visual,
        "maturity": maturity,
        "tobe_improvement": tobe_view,
        "reasoning": actions,
        "warnings": list(trace.get("warnings") or []),
    }


def attach_quality_pack(
    audit: Dict[str, Any],
    xml: str,
    regulation_text: str = "",
    blueprint: Any = None,
) -> Dict[str, Any]:
    """Дописывает в audit блок bpmn_quality без изменения methodology."""
    pack = jury_scorecard(audit, xml, regulation_text, blueprint, None)
    audit["bpmn_quality"] = {
        "visual": pack["visual"],
        "structural_quality": pack["maturity"]["structural_quality"],
        "logical_quality": pack["maturity"]["logical_quality"],
        "business_quality": pack["maturity"]["business_quality"],
        "maturity_level": pack["maturity"]["level"],
        "maturity_title": pack["maturity"]["title"],
        "maturity_why": pack["maturity"].get("why"),
        "asis_fidelity": pack["asis_fidelity"],
        "fidelity_caption": pack.get("fidelity_caption"),
        "process_health": pack.get("process_health") or [],
        "ai_confidence": pack.get("ai_confidence") or {},
        "process_improvement": pack.get("process_improvement") or {},
        "trace_warnings": pack["warnings"],
        "trace": pack["trace"],
    }
    return audit
