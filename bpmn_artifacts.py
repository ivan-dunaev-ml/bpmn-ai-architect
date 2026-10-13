"""Артефакты данных на схеме — только по флажку «Отображать артефакты данных» (по умолчанию выключено).

Документы регламента (наряд-допуск, дефектная ведомость…) → bpmn:dataObjectReference,
информационные системы (журнал, 1С, СЭД…) → bpmn:dataStoreReference.

Схема без флажка не меняется. С флажком под дорожкой с артефактами добавляется полоса,
всё ниже неё сдвигается вниз, объект встаёт в полосу под своей задачей, связь — вертикаль
(или ортогональный излом), без диагоналей и без пересечения блоков. Не больше MAX_OBJECTS объектов:
«метро Токио» из 15 документов над задачами — антипаттерн.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
    "dc": "http://www.omg.org/spec/DD/20100524/DC",
    "di": "http://www.omg.org/spec/DD/20100524/DI",
    "bioc": "http://bpmn.io/schema/bpmn/biocolor/1.0",
    "color": "http://www.omg.org/spec/BPMN/non-normative/color/1.0",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

MAX_OBJECTS = 6
STRIP_H = 84.0  # полоса артефактов внизу дорожки
OBJ_W, OBJ_H = 36.0, 50.0
STORE_W, STORE_H = 50.0, 50.0
OBJ_GAP_X = 24.0
_WORK = {"task", "userTask", "scriptTask", "manualTask", "serviceTask", "sendTask", "receiveTask", "businessRuleTask"}


def _q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _stems(text: str) -> set:
    return {w[:6] for w in re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", (text or "").lower())}


def _best_node(title: str, nodes: List[Tuple[str, str]]) -> Optional[str]:
    """Узел, чьё имя ближе всего к названию шага регламента (общие основы слов)."""
    want = _stems(title)
    best, score = None, 0
    for node_id, name in nodes:
        got = len(want & _stems(name))
        if got > score:
            best, score = node_id, got
    return best if score >= 2 or (score >= 1 and len(want) <= 2) else None


def _plan(regulation_text: str, audit: Dict[str, Any], nodes: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Что и к какой задаче привязать: документ — к первому шагу, где он упомянут (там он создаётся)."""
    try:
        from ai_generator import normalize_regulation, parse_regulation

        steps = {s.num: s.title for s in parse_regulation(normalize_regulation(regulation_text or "")).steps}
    except Exception:  # noqa: BLE001
        steps = {}
    plan: List[Dict[str, Any]] = []
    used = set()
    sources = [("object", item) for item in (audit.get("artifacts") or [])]
    sources += [("store", item) for item in (audit.get("it_systems") or [])]
    for kind, item in sources:
        name = str(item.get("name") or "").strip()
        if not name or name in used:
            continue
        for num in item.get("steps") or []:
            host = _best_node(steps.get(num, ""), nodes)
            if host:
                plan.append({"kind": kind, "name": name, "task": host})
                used.add(name)
                break
        if len(plan) >= MAX_OBJECTS:
            break
    return plan


def with_data_artifacts(xml: str, regulation_text: str, audit: Dict[str, Any]) -> str:
    """XML схемы + артефакты данных. Ошибка или нечего добавить — исходный XML без изменений."""
    try:
        return _with_data_artifacts(xml, regulation_text, audit or {})
    except Exception:  # noqa: BLE001 — опциональный слой не должен ломать основную схему
        return xml


def _with_data_artifacts(xml: str, regulation_text: str, audit: Dict[str, Any]) -> str:
    root = ET.fromstring(xml.encode("utf-8"))
    plane = root.find(".//bpmndi:BPMNPlane", NS)
    if plane is None:
        return xml
    shapes = {s.get("bpmnElement"): s for s in plane.findall("bpmndi:BPMNShape", NS)}

    def bounds(element_id: str) -> Optional[ET.Element]:
        shape = shapes.get(element_id)
        return shape.find("dc:Bounds", NS) if shape is not None else None

    # Узлы верхнего уровня процессов (задачи и подпроцессы) и вложенные задачи → их подпроцесс.
    host_of: Dict[str, Tuple[ET.Element, ET.Element]] = {}  # id задачи → (элемент-хост, процесс)
    named: List[Tuple[str, str]] = []
    for process in root.findall("bpmn:process", NS):
        for el in list(process):
            kind = _local(el.tag)
            if kind in _WORK or kind == "subProcess":
                host_of[el.get("id")] = (el, process)
                named.append((el.get("id"), el.get("name") or ""))
                if kind == "subProcess":
                    for inner in el.iter():
                        if _local(inner.tag) in _WORK:
                            host_of[inner.get("id")] = (el, process)
                            named.append((inner.get("id"), inner.get("name") or ""))
    plan = _plan(regulation_text, audit, named)
    if not plan:
        return xml

    lane_of: Dict[str, ET.Element] = {}
    for lane in root.iter(_q("bpmn", "lane")):
        for ref in lane.findall("bpmn:flowNodeRef", NS):
            lane_of[(ref.text or "").strip()] = lane

    # Хост — задача верхнего уровня или подпроцесс, внутри которого задача.
    for item in plan:
        host_el, process = host_of[item["task"]]
        item["host"], item["process"] = host_el, process
        item["lane"] = lane_of.get(host_el.get("id"))
    plan = [item for item in plan if item["lane"] is not None and bounds(item["host"].get("id")) is not None]
    if not plan:
        return xml

    # 1. Полоса под каждой дорожкой с артефактами: сдвигаем всё, что ниже её нижней границы.
    def shift_below(limit: float, dy: float) -> None:
        for shape in plane.findall("bpmndi:BPMNShape", NS):
            for b in shape.iter(_q("dc", "Bounds")):
                if float(b.get("y")) >= limit - 0.5:
                    b.set("y", f"{float(b.get('y')) + dy:.1f}")
        for edge in plane.findall("bpmndi:BPMNEdge", NS):
            for wp in edge.findall("di:waypoint", NS):
                if float(wp.get("y")) >= limit - 0.5:
                    wp.set("y", f"{float(wp.get('y')) + dy:.1f}")
            for b in edge.iter(_q("dc", "Bounds")):
                if float(b.get("y")) >= limit - 0.5:
                    b.set("y", f"{float(b.get('y')) + dy:.1f}")

    lanes_used = {item["lane"].get("id"): item["lane"] for item in plan}
    participants = [p.get("id") for p in root.iter(_q("bpmn", "participant"))]
    lane_bottom: Dict[str, float] = {}
    for lane_id in sorted(lanes_used, key=lambda lid: -float(bounds(lid).get("y"))):
        lb = bounds(lane_id)
        bottom = float(lb.get("y")) + float(lb.get("height"))
        shift_below(bottom, STRIP_H)
        lb.set("height", f"{float(lb.get('height')) + STRIP_H:.1f}")
        lane_bottom[lane_id] = bottom
        # Пул, в котором дорожка, растёт на ту же полосу.
        for participant_id in participants:
            b = bounds(participant_id)
            if b is None:
                continue
            top, height = float(b.get("y")), float(b.get("height"))
            if top <= bottom - 1 <= top + height:
                b.set("height", f"{height + STRIP_H:.1f}")
    # Нижние границы дорожек после сдвигов (выше лежащие дорожки сдвигали нижние).
    for lane_id in lane_bottom:
        lb = bounds(lane_id)
        lane_bottom[lane_id] = float(lb.get("y")) + float(lb.get("height")) - STRIP_H

    # 2. Объекты в полосе под хостом. Места, где через полосу идут потоки, и уже поставленные объекты заняты:
    #    объект сдвигается к ближайшему свободному месту, связь — ортогональный излом.
    flow_ids = {f.get("id") for f in root.iter(_q("bpmn", "sequenceFlow"))}
    flow_segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for edge in plane.findall("bpmndi:BPMNEdge", NS):
        if edge.get("bpmnElement") in flow_ids:
            pts = [(float(w.get("x")), float(w.get("y"))) for w in edge.findall("di:waypoint", NS)]
            flow_segments += list(zip(pts, pts[1:]))
    block_rects = [
        tuple(float(b.get(k)) for k in ("x", "y", "width", "height"))
        for shape in plane.findall("bpmndi:BPMNShape", NS)
        if shape.get("isHorizontal") != "true" and (b := shape.find("dc:Bounds", NS)) is not None
    ]

    def busy(x0: float, x1: float, y0: float, y1: float) -> bool:
        for (ax, ay), (bx, by) in flow_segments:
            if max(ay, by) >= y0 and min(ay, by) <= y1 and max(ax, bx) >= x0 and min(ax, bx) <= x1:
                return True
        for bx, by, bw, bh in block_rects:
            if bx < x1 and bx + bw > x0 and by < y1 and by + bh > y0:
                return True
        return False

    placed: Dict[str, List[Tuple[float, float]]] = {}
    definitions_store_at = list(root).index(root.find("bpmn:collaboration", NS))
    for n, item in enumerate(plan, start=1):
        hb = bounds(item["host"].get("id"))
        hx, hy, hw, hh = (float(hb.get(k)) for k in ("x", "y", "width", "height"))
        cx = hx + hw / 2.0
        w, h = (STORE_W, STORE_H) if item["kind"] == "store" else (OBJ_W, OBJ_H)
        strip_top = lane_bottom[item["lane"].get("id")]
        y = strip_top + 12.0
        row = placed.setdefault(item["lane"].get("id"), [])
        label_w = min(140.0, 7.0 * len(item["name"]) + 10.0)
        span = max(w, label_w)

        def free(left: float) -> bool:
            x0, x1 = left + w / 2.0 - span / 2.0 - 8.0, left + w / 2.0 + span / 2.0 + 8.0
            if any(x0 < r + OBJ_GAP_X and x1 > l - OBJ_GAP_X for l, r in row):
                return False
            return not busy(x0, x1, strip_top, y + h + 22.0)

        x = cx - w / 2.0
        for step in range(0, 61):
            offsets = (0.0,) if step == 0 else (step * 12.0, -step * 12.0)
            hit = next((x + off for off in offsets if free(x + off)), None)
            if hit is not None:
                x = hit
                break
        else:
            continue  # места в полосе нет — объект не рисуем, схема важнее
        row.append((x + w / 2.0 - span / 2.0, x + w / 2.0 + span / 2.0))
        ref_id = f"DataRef_{n}"
        assoc_id = f"DataOutputAssociation_{n}"
        process = item["process"]
        lane_set = process.find("bpmn:laneSet", NS)
        at = list(process).index(lane_set) + 1 if lane_set is not None else 0
        if item["kind"] == "store":
            store = ET.Element(_q("bpmn", "dataStore"), {"id": f"DataStore_{n}", "name": item["name"]})
            root.insert(definitions_store_at, store)
            definitions_store_at += 1
            process.insert(at, ET.Element(_q("bpmn", "dataStoreReference"), {
                "id": ref_id, "name": item["name"], "dataStoreRef": f"DataStore_{n}",
            }))
        else:
            process.insert(at, ET.Element(_q("bpmn", "dataObjectReference"), {
                "id": ref_id, "name": item["name"], "dataObjectRef": f"DataObject_{n}",
            }))
            process.insert(at, ET.Element(_q("bpmn", "dataObject"), {"id": f"DataObject_{n}"}))
        # dataOutputAssociation — после incoming/outgoing хоста (порядок tActivity в XSD).
        host = item["host"]
        pos = 0
        for i, child in enumerate(list(host)):
            if _local(child.tag) in ("documentation", "extensionElements", "incoming", "outgoing"):
                pos = i + 1
        assoc = ET.Element(_q("bpmn", "dataOutputAssociation"), {"id": assoc_id})
        ET.SubElement(assoc, _q("bpmn", "targetRef")).text = ref_id
        host.insert(pos, assoc)
        # DI: форма объекта с подписью снизу и ортогональная связь.
        shape = ET.SubElement(plane, _q("bpmndi", "BPMNShape"), {"id": f"{ref_id}_di", "bpmnElement": ref_id})
        ET.SubElement(shape, _q("dc", "Bounds"), {"x": f"{x:.1f}", "y": f"{y:.1f}", "width": f"{w:.0f}", "height": f"{h:.0f}"})
        label = ET.SubElement(shape, _q("bpmndi", "BPMNLabel"))
        lw = min(140.0, 7.0 * len(item["name"]) + 10.0)
        ET.SubElement(label, _q("dc", "Bounds"), {
            "x": f"{x + w / 2.0 - lw / 2.0:.1f}", "y": f"{y + h + 4:.1f}", "width": f"{lw:.0f}", "height": "14",
        })
        edge = ET.SubElement(plane, _q("bpmndi", "BPMNEdge"), {"id": f"{assoc_id}_di", "bpmnElement": assoc_id})
        ox = x + w / 2.0
        # Выход пунктира — с низа хоста под объектом, но не по вертикали потока, который выходит/входит снизу.
        bottom = hy + hh

        def on_flow(px: float) -> bool:
            return any(
                abs(ax - bx) < 0.5 and abs(ax - px) < 8.0 and min(ay, by) <= bottom + 1.0 <= max(ay, by) + 1.0
                for (ax, ay), (bx, by) in flow_segments
            )

        sx = min(max(ox, hx + 12.0), hx + hw - 12.0)
        for shift in (0.0, 16.0, -16.0, 30.0, -30.0):
            cand = min(max(sx + shift, hx + 8.0), hx + hw - 8.0)
            if not on_flow(cand):
                sx = cand
                break
        points = [(sx, bottom)]
        if abs(ox - sx) > 0.5:
            mid = strip_top + 4.0
            points += [(sx, mid), (ox, mid)]
        points.append((ox, y))
        for px, py in points:
            ET.SubElement(edge, _q("di", "waypoint"), {"x": f"{px:.1f}", "y": f"{py:.1f}"})
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
