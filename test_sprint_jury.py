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


PASSPORT = (ROOT / "examples" / "example_4_access_route_passport.txt").read_text(encoding="utf-8")


def _passport_with_word_markup() -> str:
    """Тот же паспорт, как его копируют из PDF с рецензированием: «Formatted […]», «| » из таблиц."""
    junk = "Formatted [Иванов И.И.]: Font: 12 pt\nBulleted + Level: 1 + Aligned at: 0.63 cm\nIndent at: 1.27 cm\nNo bullets or numbering\n"
    text = PASSPORT.replace("Ответственный: ", "Ответственный: | ").replace("Длительность: ", "Длительность: | ")
    lines = text.splitlines()
    return "\n".join(lines[:3] + junk.splitlines() + lines[3:]) + "\n"


@pytest.mark.parametrize("text", [PASSPORT, _passport_with_word_markup()], ids=["clean", "word-markup"])
def test_customer_passport_regulation(text):
    """Пример кейсодателя (Шаг 4.1–4.3 + поля): 3 дорожки, 3 задачи по 16 ч, КП 48 ч, ИС1/ИС3, документы, QS ≥ 90."""
    parsed = gen.parse_regulation(gen.normalize_regulation(text))
    assert len(parsed.steps) == 3
    assert [s.hours for s in parsed.steps] == [16.0, 16.0, 16.0]
    assert parsed.steps[0].title.startswith("Установить ответственного")
    assert parsed.steps[1].title.startswith("Подготовить и согласовать запрос")
    assert parsed.steps[2].title.startswith("Исполнить запрос")
    stop = {"входы", "выходы", "длительность", "участвуют", "ответственный", "основание", "результат", "срок"}
    assert len(parsed.roles) == 3 and not {r.lower() for r in parsed.roles} & stop
    assert parsed.roles[0] == "Менеджер процесса" and all("|" not in r for r in parsed.roles)
    assert parsed.steps[0].participants and parsed.steps[0].inputs and parsed.steps[0].outputs
    xml, audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert audit["sla"]["critical_path_hours"] == 48.0
    assert len(audit["lane_load"]) == 3 and audit["stats"]["work_items"] == 3
    assert int(audit["methodology"]["score"]) >= 90
    systems = {item["name"] for item in audit["it_systems"]}
    assert {"ИС1", "ИС3"} <= systems
    documents = " ".join(item["name"] for item in audit["artifacts"])
    assert "Корпоративное письмо" in documents and "Запрос на изменение маршрута" in documents
    assert xsd_errors_xml(xml) == []


def test_passport_participants_are_consulted_in_raci():
    parsed = gen.parse_regulation(gen.normalize_regulation(PASSPORT))
    matrix = gen.generate_raci_matrix(parsed.steps, parsed.roles)
    first = matrix[0]["assignments"]
    assert "R" in first["Менеджер процесса"]
    assert "C" in first["Работник Центра информационных технологий"]


def test_passport_artifacts_layer_and_tobe_keep_fields_out():
    from app import artifacts_xml, regulation_has_io

    assert regulation_has_io(PASSPORT)
    xml, audit, err = gen.generate_bpmn_from_text(PASSPORT, use_llm=False)
    layered = artifacts_xml(xml, PASSPORT, audit)
    assert layered.count("<bpmn:dataObjectReference") >= 6 and "<bpmn:dataInputAssociation" in layered
    assert xsd_errors_xml(layered) == [] and _validate_xml(layered) == [] and _diagonals(layered) == 0
    _opt, delta = gen.optimize_process_to_be(PASSPORT, audit)
    tobe_roles = {item["role"] for item in (delta["tobe_audit"].get("lane_load") or [])}
    assert not {r.lower() for r in tobe_roles} & {"входы", "выходы", "длительность"}
    # ЭДО — только для шага, где согласование — действие («Подготовить и согласовать»), не слово объекта.
    edo = [a for a in delta["actions"] if a.get("kind") == "edo_approval" or a.get("type") == "edo_approval"]
    assert len(edo) == 1 and "Шаг 2" in edo[0]["detail"]


def test_docx_table_reader_has_no_pipes_or_duplicates(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    table = doc.add_table(rows=4, cols=2)
    head = table.cell(0, 0).merge(table.cell(0, 1))
    head.text = "Шаг 1.1 Проверка комплектности заявки"
    table.cell(1, 0).text, table.cell(1, 1).text = "Ответственный:", "Диспетчер"
    table.cell(2, 0).text, table.cell(2, 1).text = "Длительность:", "2 рабочих часа"
    table.cell(3, 0).text, table.cell(3, 1).text = "Выходы:", "Проверенная заявка\nЖурнал в ИСЗ"
    path = tmp_path / "p.docx"
    doc.save(path)
    text, err = gen.read_docx_regulation(path.read_bytes())
    assert not err and "|" not in text
    assert text.count("Шаг 1.1") == 1  # объединённая ячейка — один раз
    assert "Выходы: Проверенная заявка; Журнал в ИСЗ" in text
    assert gen.extract_it_systems(text) == ["ИС3"]  # OCR: «ИСЗ» → ИС3


def test_task_types_follow_the_work():
    xml, _audit, err = gen.generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    manual = re.findall(r'<bpmn:manualTask id="[^"]+" name="([^"]+)"', xml)
    assert "Выполнить аварийный ремонт оборудования" in manual
    assert "<bpmn:scriptTask" in xml  # фиксация в журнале системы
    assert "Принять сообщение об аварийном отключении оборудования ПС 110 кВ" not in manual
    assert "add_manual_task" in gen.build_prompt(EX1)


def test_speed_cloud_tobe_is_opt_in_and_inspector_is_lazy(monkeypatch):
    """Скорость: облачный To-Be (два запроса к модели, ~12 с) — только по BPMN_TOBE_LLM=1; инспектор не зовёт модель сам."""
    monkeypatch.delenv("BPMN_AI_MODE", raising=False)
    monkeypatch.delenv("BPMN_TOBE_LLM", raising=False)
    monkeypatch.setattr(gen, "_chat_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("облако не нужно")))
    audit = {"generation": {"engine": "openai:openai/gpt-oss-120b"}}
    assert gen._try_llm_optimize_to_be(EX1, audit) is None
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'key="inspector_choice", index=None' in app_src


def test_model_sla_is_pinned_to_regulation():
    from process_blueprint import analyze_process, compile_blueprint

    code, _ = compile_blueprint(analyze_process(EX1))
    drifted = code.replace("DIAGRAM.set_sla(n0, 0.083)", "DIAGRAM.set_sla(n0, 0.5)")
    assert drifted != code
    _xml, audit, err = gen.execute_generated_code(drifted, "x", regulation_text=EX1)
    assert not err
    assert audit["sla"]["critical_path_hours"] == 9.7  # 30 мин модели → 5 мин регламента
    assert any("приведён к регламенту" in line for line in audit["auto_healed"])


@pytest.mark.parametrize("name,roles,tasks", [
    ("13_word_table_pipes", ["Инициатор", "Закупочная служба", "Тендерный комитет", "Юридическая служба"], 5),
    ("14_tab_table", ["Сотрудник", "Служба безопасности", "Бюро пропусков"], 4),
    ("19_all_caps", ["Кладовщик", "Бухгалтерия"], 3),
    ("31_one_line", ["Сотрудник", "Руководитель", "Отдел кадров", "Бухгалтерия"], 4),
    ("30_roles_with_punctuation", ["Ведущий инженер", "Руководитель", "Заместитель генерального директора по развитию"], 3),
    ("22_skipped_numbers", ["Заявитель", "Диспетчер", "Мастер", "Ремонтная бригада"], 4),
    ("21_single_step", ["Служба охраны труда"], 1),
])
def test_wild_formats_parse_like_a_human(name, roles, tasks):
    """Как жюри вставит текст: таблица Word, капс, одной строкой, роли с уточнениями, дубль номера, один шаг."""
    text = (ROOT / "stress_tests" / f"{name}.txt").read_text(encoding="utf-8")
    xml, audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not err, err
    assert [item["role"] for item in audit["lane_load"]] == roles or sorted(r["role"] for r in audit["lane_load"]) == sorted(roles)
    assert audit["stats"]["work_items"] == tasks
    assert xsd_errors_xml(xml) == [] and _validate_xml(xml) == []
    assert int(audit["methodology"]["score"]) == 100


@pytest.mark.parametrize("name", ["06_garbage", "34_title_only"])
def test_garbage_gets_clear_error_not_a_fake_diagram(name):
    text = (ROOT / "stress_tests" / f"{name}.txt").read_text(encoding="utf-8")
    xml, _audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not xml and "Не удалось распознать" in err


def test_docx_steps_table_becomes_steps(tmp_path):
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Регламент: Выдача пропуска")
    table = doc.add_table(rows=3, cols=4)
    for c, h in enumerate(["№", "Действие", "Исполнитель", "Срок"]):
        table.cell(0, c).text = h
    for r, row in enumerate([["1", "Подать заявку на пропуск", "Сотрудник", "15 минут"], ["2", "Проверить данные сотрудника", "Служба безопасности", "2 часа"]], start=1):
        for c, v in enumerate(row):
            table.cell(r, c).text = v
    path = tmp_path / "t.docx"
    doc.save(path)
    text, err = gen.read_docx_regulation(path.read_bytes())
    assert not err and "|" not in text
    assert "1. Сотрудник: Подать заявку на пропуск (15 минут)." in text
    parsed = gen.parse_regulation(gen.normalize_regulation(text))
    assert [s.role for s in parsed.steps] == ["Сотрудник", "Служба безопасности"]


def test_prompt_injection_is_not_a_step_and_not_sent_to_model():
    text = (ROOT / "stress_tests" / "35_prompt_injection.txt").read_text(encoding="utf-8")
    xml, audit, err = gen.generate_bpmn_from_text(text, use_llm=False)
    assert not err
    names = re.findall(r'<bpmn:\w*[tT]ask id="[^"]+" name="([^"]+)"', xml)
    assert names == ["Снять показания счётчика", "Внести показания в биллинг", "Проверить отчёт"]
    prompt = gen.build_prompt(text)
    assert "import os" not in prompt and "Игнорируй" not in prompt
    assert "Контролёр вносит показания в биллинг" in prompt  # остальной текст шага на месте


def test_ui_defaults_to_yandex_and_custom_text_is_kept():
    """Жюри: первый запрос — в Yandex AI Studio; свой текст в поле переключает селектор и не откатывается."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert [r.value for r in at.radio if r.label == "Движок генерации"] == ["Yandex AI Studio"]
    mine = "Регламент: Мой процесс\n1. Кассир принимает оплату (5 минут).\n2. Бухгалтер проводит платёж в 1С (10 минут).\n"
    at.text_area(key="reg_text").set_value(mine).run()
    assert at.session_state["example_choice"].endswith("Свой текст регламента")
    assert at.session_state["reg_text"] == mine
    [b for b in at.button if "Сгенерировать" in b.label][0].click().run()
    assert not at.exception
    assert "Кассир" in str(at.session_state["result"]["xml"])
    assert at.session_state["reg_text"] == mine
