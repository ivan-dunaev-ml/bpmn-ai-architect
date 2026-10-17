"""Облачный мозг без 413: провайдеры yandex | cloud | emulator, бюджет промпта, честные метрики. Без сети."""

from pathlib import Path

import pytest

import ai_generator as gen
import llm_providers
from bpmn_quality import jury_scorecard, regulation_trace
from process_blueprint import analyze_process, compile_blueprint

ROOT = Path(__file__).resolve().parent
EX1 = (ROOT / "examples" / "example_1_substation_repair.txt").read_text(encoding="utf-8")
LONG = (ROOT / "stress_tests" / "05_very_long_40_steps.txt").read_text(encoding="utf-8")

_ENV = [
    "BPMN_PROVIDER", "BPMN_AI_MODE",
    "YANDEX_API_KEY", "YANDEX_IAM_TOKEN", "YANDEX_FOLDER_ID", "YANDEX_MODEL", "YANDEX_BASE_URL",
    "YANDEX_REASONING_EFFORT", "YANDEX_FALLBACK_MODEL", "YANDEX_FALLBACK_REASONING_EFFORT",
] + [f"{p}_{k}" for p in llm_providers.CLOUD_PREFIXES for k in (
    "API_KEY", "BASE_URL", "MODEL", "REASONING_EFFORT", "INPUT_BUDGET", "MAX_TOKENS",
)]


@pytest.fixture
def env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _groq(env, prefix="OPENAI"):
    env.setenv(f"{prefix}_API_KEY", "gsk_test")
    env.setenv(f"{prefix}_BASE_URL", "https://api.groq.com/openai/v1")
    env.setenv(f"{prefix}_MODEL", "openai/gpt-oss-120b")
    env.setenv(f"{prefix}_REASONING_EFFORT", "low")


def _yandex(env):
    env.setenv("YANDEX_API_KEY", "AQVN_test")
    env.setenv("YANDEX_FOLDER_ID", "b1gfolder")


def test_modes_pick_providers(env):
    _groq(env)
    _yandex(env)
    assert [p.slot for p in llm_providers.providers()] == ["yandex", "openai"]
    env.setenv("BPMN_PROVIDER", "yandex")
    assert [p.slot for p in llm_providers.providers()] == ["yandex"]
    env.setenv("BPMN_PROVIDER", "cloud")
    assert [p.slot for p in llm_providers.providers()] == ["openai"]
    env.setenv("BPMN_PROVIDER", "emulator")
    assert llm_providers.providers() == [] and gen._engines() == []
    env.setenv("BPMN_PROVIDER", "auto")
    env.setenv("BPMN_AI_MODE", "emulator")  # старый переключатель по-прежнему главный
    assert llm_providers.providers() == []


def test_yandex_request_contract(env):
    _yandex(env)
    provider = llm_providers.yandex_provider()
    assert provider.model == "gpt://b1gfolder/yandexgpt/latest"
    assert provider.label == "yandex:yandexgpt/latest"
    assert provider.headers["Authorization"] == "Api-Key AQVN_test"
    assert provider.headers["x-folder-id"] == "b1gfolder"
    payload = llm_providers.build_payload(provider, [{"role": "user", "content": "x"}], 0.1)
    # AI Studio в режиме OpenAI: только temperature / max_tokens / stream / response_format.
    assert set(payload) == {"model", "temperature", "messages", "max_tokens"}

    seen = {}

    def fake_http(url, payload, headers=None, timeout=0):
        seen.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": "ok"}}]}

    env.setattr(gen, "_http_json", fake_http)
    label, text = gen._call_openai(gen.build_prompt(EX1), "YANDEX")
    assert (label, text) == ("yandex:yandexgpt/latest", "ok")
    assert seen["url"] == "https://llm.api.cloud.yandex.net/v1/chat/completions"
    assert seen["payload"]["messages"][0]["role"] == "system"
    assert "REGULATION_DATA" in seen["payload"]["messages"][1]["content"]


def test_groq_budget_keeps_every_request_under_8k(env):
    _groq(env)
    provider = llm_providers.find("openai")
    budget = provider.input_budget()
    # Замер Groq Free: вход 5.4K токенов проходит, 6.8K — HTTP 413. Оценка завышает, бюджет 5000.
    assert budget == 5000
    for text in (EX1, LONG):
        prompt = gen.build_prompt(text, max_tokens=budget)
        assert llm_providers.estimate_tokens(prompt) <= budget
        code, _ = compile_blueprint(analyze_process(text))
        repair = gen.build_rework_repair_prompt(text, code, ["REWORK VALIDATION FAILED"], max_tokens=budget)
        assert llm_providers.estimate_tokens(repair) <= budget, "починка не должна давать HTTP 413"
    assert gen.prompt_has_draft(gen.build_prompt(EX1, max_tokens=budget))
    assert gen.prompt_has_draft(gen.build_prompt(LONG, max_tokens=budget))
    assert not gen.prompt_has_draft(gen.build_prompt(LONG, max_tokens=4000))  # меньше лимит — черновик уходит первым


def test_prompt_has_one_rework_rule():
    prompt = gen.build_prompt(EX1)
    # Раньше общий раздел требовал задачу исправления для любого возврата, а чертёж для rollback — запрещал.
    assert "rollback: обратное ребро" in prompt and "новую задачу не создавай" in prompt
    assert "Отдельную задачу исправления" not in prompt
    assert '"Reworks"' in prompt and "ЧЕРНОВИК —" in prompt


def test_http_413_retries_with_compact_prompt(env):
    _groq(env)
    env.setenv("BPMN_PROVIDER", "cloud")
    draft = gen._blueprint_parts(EX1)[1]
    sent = []

    class _Resp:
        status_code = 413
        headers = {}
        text = "Request too large"

    class _HTTP413(Exception):
        response = _Resp()

    def fake_http(url, payload, headers=None, timeout=0):
        system = payload["messages"][0]["content"]
        sent.append("ЧЕРНОВИК —" in system)
        if "ЧЕРНОВИК —" in system:
            raise _HTTP413("413 Payload Too Large")
        return {"choices": [{"message": {"content": draft}}]}

    env.setattr(gen, "_http_json", fake_http)
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=True)
    assert not err and xml
    assert sent == [True, False]
    generation = audit["generation"]
    assert generation["engine"] == "openai:openai/gpt-oss-120b"
    assert generation["contract"] == "text"
    assert any("компактным промптом" in line for line in generation["trace"])


def test_model_edits_are_reported(env):
    draft = gen._blueprint_parts(EX1)[1]
    same = gen.model_edit_summary(draft, draft)
    assert same["identical"] and "без правок" in same["summary"]
    edited = draft.replace("Составить дефектную ведомость", "Оформить дефектную ведомость по итогам диагностики")
    changes = gen.model_edit_summary(draft, edited)
    assert not changes["identical"] and changes["renamed"] == 1


def test_fidelity_does_not_punish_paraphrase(env):
    env.setenv("BPMN_AI_MODE", "emulator")
    blueprint = analyze_process(EX1)
    code, _ = compile_blueprint(blueprint)
    paraphrased = (
        code.replace("Провести визуальный осмотр ячейки и ошиновки", "Осмотреть ячейку и ошиновку визуально")
        .replace("Составить дефектную ведомость", "Составить ведомость дефектов")
    )
    xml, audit, err = gen.execute_generated_code(paraphrased, "x", regulation_text=EX1)
    assert not err, err
    audit["generation"] = {"engine": "openai:openai/gpt-oss-120b"}
    trace = regulation_trace(EX1, blueprint, audit, xml)
    assert trace["dimensions"]["returns"] == {"found": 2, "total": 2}
    assert trace["dimensions"]["roles"]["found"] == trace["dimensions"]["roles"]["total"]
    assert trace["fidelity"] >= 90, trace["warnings"]
    assert trace["self_check"] is False


def test_emulator_fidelity_is_labelled_self_check(env):
    env.setenv("BPMN_AI_MODE", "emulator")
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    card = jury_scorecard(audit, xml, EX1, analyze_process(EX1), None)
    assert card["trace"]["self_check"] is True
    assert "самопроверка" in card["fidelity_caption"]
    confidence = card["ai_confidence"]
    assert confidence["caveat"] and confidence["basis"] and confidence["method"]
    weights = sum(item["weight"] for item in confidence["basis"])
    assert weights == 100


def test_confidence_is_computed_not_constant(env):
    env.setenv("BPMN_AI_MODE", "emulator")
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    from bpmn_quality import ai_confidence

    full = ai_confidence(audit, 100)["score"]
    half = ai_confidence(audit, 50)["score"]
    assert full - half == 20  # 40% веса у полноты переноса


def test_parent_task_id_keeps_role_lane():
    from bpmn_framework import BPMNDiagramBuilder

    diagram = BPMNDiagramBuilder(process_name="x", process_id=gen.ROOT_PROCESS_ID)
    _pool, lanes = diagram.add_pool(gen.ROOT_PROCESS_ID, ["Диспетчер", "Служба безопасности"])
    check = diagram.add_user_task("Проверить наряд-допуск", lanes[1])
    gate = diagram.add_exclusive_gateway("Допуск выдан?", check)  # частая ошибка модели
    assert diagram.nodes[gate].lane_id == lanes[1]


def test_lost_returns_fall_back_to_local_blueprint(env):
    _groq(env)
    env.setenv("BPMN_PROVIDER", "cloud")
    real_exec = gen.execute_generated_code

    def model_loses_returns(code, *args, **kwargs):
        if "blueprint" not in kwargs:
            return "", {}, gen._return_diagram_not_ready(EX1, 2, 1)
        return real_exec(code, *args, **kwargs)

    env.setattr(gen, "execute_generated_code", model_loses_returns)
    env.setattr(gen, "_http_json", lambda *a, **k: {"choices": [{"message": {"content": "x = 1"}}]})
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=True)
    assert not err and xml
    assert audit["generation"]["engine"] == "semantic-emulator"
    assert any("потеряла возвраты" in line for line in audit["generation"]["trace"])


def test_named_stage_becomes_subprocess(env):
    env.setenv("BPMN_AI_MODE", "emulator")
    xml, audit, err = gen.generate_bpmn_from_text(EX1, use_llm=False)
    assert not err
    assert audit["stats"]["subprocesses"] == 1
    assert int(audit["methodology"]["score"]) == 100
    loops = {item["to"] for item in audit["rework_loops"]}
    assert "Комплексная диагностика повреждений" in loops
    assert audit["sla"]["with_rework_hours"] == 12.55


def test_yandex_fallback_and_reasoning(env):
    _yandex(env)
    env.setenv("YANDEX_MODEL", "gpt-oss-120b/latest")
    env.setenv("YANDEX_REASONING_EFFORT", "low")
    env.setenv("YANDEX_FALLBACK_MODEL", "yandexgpt-5.1/latest")
    env.setenv("BPMN_PROVIDER", "yandex")
    chain = llm_providers.providers()
    assert [p.slot for p in chain] == ["yandex", "yandex2"]
    assert chain[0].model == "gpt://b1gfolder/gpt-oss-120b/latest"
    assert chain[1].label == "yandex2:yandexgpt-5.1/latest"
    main = llm_providers.build_payload(chain[0], [{"role": "user", "content": "x"}], 0.1)
    assert main["reasoning_effort"] == "low"  # gpt-oss в AI Studio: вдвое быстрее
    backup = llm_providers.build_payload(chain[1], [{"role": "user", "content": "x"}], 0.1)
    assert "reasoning_effort" not in backup
