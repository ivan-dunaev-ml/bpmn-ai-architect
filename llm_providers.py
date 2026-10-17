"""Провайдеры LLM: один контракт для Yandex AI Studio, OpenAI-совместимых облаков и эмулятора.

Режим выбирается переменной BPMN_PROVIDER:

- auto (по умолчанию) — Yandex AI Studio, если задан ключ, затем облака OPENAI_* → FALLBACK* по порядку;
- yandex — только Yandex AI Studio (стек партнёра на финале);
- cloud — только OpenAI-совместимые облака (Groq, OpenRouter и т. п.);
- emulator — без сети, схему собирает встроенный разбор.

Пайплайн генерации от выбора не зависит: он получает список Provider и вызывает chat().
Смена модели — только переменные окружения / Secrets, код не меняется.

Yandex AI Studio (OpenAI-совместимый Chat Completions):
    YANDEX_API_KEY      — API-ключ сервисного аккаунта (или YANDEX_IAM_TOKEN)
    YANDEX_FOLDER_ID    — каталог (folder) сервисного аккаунта
    YANDEX_MODEL        — «yandexgpt/latest», «yandexgpt-lite/latest» или полный «gpt://…»
    YANDEX_BASE_URL     — по умолчанию https://llm.api.cloud.yandex.net/v1
Облака: {PREFIX}_API_KEY, {PREFIX}_BASE_URL, {PREFIX}_MODEL, {PREFIX}_REASONING_EFFORT,
где PREFIX — OPENAI, FALLBACK, FALLBACK2, FALLBACK3.
Для любого слота: {SLOT}_INPUT_BUDGET (потолок промпта, токенов), {SLOT}_MAX_TOKENS (потолок ответа).
"""

from __future__ import annotations

import contextvars
import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

CLOUD_PREFIXES = ("OPENAI", "FALLBACK", "FALLBACK2", "FALLBACK3")
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
YANDEX_DEFAULT_BASE = "https://llm.api.cloud.yandex.net/v1"
MODES = ("auto", "yandex", "cloud", "emulator")

# Потолок промпта, токенов (по оценке estimate_tokens, она завышает на 0–20%).
# Groq Free, gpt-oss-120b, замер 10.10.2026: вход 5.4K токенов — 200, 6.8K — HTTP 413
# (лимит 8K токенов в минуту; заказанный max_tokens в проверку не входит). Берём 5000.
# У остальных лимит — окно контекста за вычетом ответа.
_HOST_INPUT = (
    ("api.groq.com", 5000),
    ("openrouter.ai", 24000),
    ("llm.api.cloud.yandex.net", 24000),
)
_DEFAULT_INPUT = 12000
_DEFAULT_MAX_OUTPUT = 4096


@dataclass(frozen=True)
class Provider:
    slot: str  # yandex | openai | fallback | fallback2 | fallback3
    kind: str  # yandex | cloud
    base_url: str
    model: str  # как уходит в API
    display_model: str  # как показываем в интерфейсе
    headers: Dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    reasoning_effort: str = ""
    input_limit: int = _DEFAULT_INPUT
    max_output: int = _DEFAULT_MAX_OUTPUT

    @property
    def label(self) -> str:
        """Метка движка в audit.generation.engine: «openai:openai/gpt-oss-120b», «yandex:yandexgpt/latest»."""
        return f"{self.slot}:{self.display_model}"

    @property
    def host(self) -> str:
        return re.sub(r"^https?://([^/]+).*$", r"\1", self.base_url)

    def input_budget(self) -> int:
        """Сколько токенов (по estimate_tokens) можно отдать под промпт без HTTP 413."""
        return self.input_limit


# Выбор движка в интерфейсе — на сессию пользователя, не на весь сервер: переменная окружения общая
# для всех посетителей, а contextvar живёт в потоке сессии Streamlit.
_SESSION_MODE: "contextvars.ContextVar[str]" = contextvars.ContextVar("bpmn_provider_mode", default="")


def set_session_mode(value: str) -> None:
    """Режим из переключателя интерфейса: auto | yandex | cloud | emulator; пустая строка — как в окружении."""
    value = (value or "").strip().lower()
    _SESSION_MODE.set(value if value in MODES else "")


@contextmanager
def session_mode(value: str):
    token = _SESSION_MODE.set((value or "").strip().lower() if (value or "").strip().lower() in MODES else "")
    try:
        yield
    finally:
        _SESSION_MODE.reset(token)


def mode() -> str:
    if os.getenv("BPMN_AI_MODE", "auto").lower() == "emulator":
        return "emulator"
    chosen = _SESSION_MODE.get()
    if chosen:
        return chosen
    value = os.getenv("BPMN_PROVIDER", "auto").strip().lower()
    return value if value in MODES else "auto"


def yandex_configured() -> bool:
    return yandex_provider() is not None


def cloud_configured() -> bool:
    return bool(cloud_providers())


def estimate_tokens(text: str) -> int:
    """Грубая оценка без токенизатора: кириллица ~3 символа на токен (gpt-oss: пример 1 — 16.3K симв. ≈ 4.6K ток.)."""
    return int(math.ceil(len(text or "") / 3.0)) + 8


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _input_limit(slot_env: str, base_url: str) -> int:
    host_default = next((limit for host, limit in _HOST_INPUT if host in base_url), _DEFAULT_INPUT)
    return _int_env(f"{slot_env}_INPUT_BUDGET", host_default)


def _max_output(slot_env: str) -> int:
    return _int_env(f"{slot_env}_MAX_TOKENS", _DEFAULT_MAX_OUTPUT)


def yandex_provider(fallback: bool = False) -> Optional[Provider]:
    """Yandex AI Studio. fallback=True — запасная модель того же каталога (YANDEX_FALLBACK_MODEL).

    AI Studio раздаёт и открытые модели: gpt-oss-120b (та же, что на Groq), qwen3.6, а также YandexGPT.
    YANDEX_REASONING_EFFORT («low») понимают рассуждающие модели (gpt-oss): ответ вдвое быстрее.
    """
    key = os.getenv("YANDEX_API_KEY", "").strip()
    iam = os.getenv("YANDEX_IAM_TOKEN", "").strip()
    folder = os.getenv("YANDEX_FOLDER_ID", "").strip()
    if not (key or iam) or not folder:
        return None
    base = os.getenv("YANDEX_BASE_URL", YANDEX_DEFAULT_BASE).rstrip("/")
    if fallback:
        name = os.getenv("YANDEX_FALLBACK_MODEL", "").strip()
        if not name:
            return None
        effort = os.getenv("YANDEX_FALLBACK_REASONING_EFFORT", "").strip()
    else:
        name = os.getenv("YANDEX_MODEL", "yandexgpt/latest").strip() or "yandexgpt/latest"
        effort = os.getenv("YANDEX_REASONING_EFFORT", "").strip()
    model = name if name.startswith("gpt://") else f"gpt://{folder}/{name.lstrip('/')}"
    display = re.sub(r"^gpt://[^/]+/", "", model)
    auth = f"Api-Key {key}" if key else f"Bearer {iam}"
    return Provider(
        slot="yandex2" if fallback else "yandex",
        kind="yandex",
        base_url=base,
        model=model,
        display_model=display,
        headers={"Authorization": auth, "x-folder-id": folder, "OpenAI-Project": folder},
        reasoning_effort=effort,
        input_limit=_input_limit("YANDEX", base),
        max_output=_max_output("YANDEX"),
    )


def yandex_providers() -> List[Provider]:
    return [item for item in (yandex_provider(), yandex_provider(fallback=True)) if item is not None]


def cloud_providers() -> List[Provider]:
    out: List[Provider] = []
    for prefix in CLOUD_PREFIXES:
        key = os.getenv(f"{prefix}_API_KEY", "").strip()
        if not key:
            continue
        base = os.getenv(f"{prefix}_BASE_URL", OPENAI_DEFAULT_BASE).rstrip("/")
        model = os.getenv(f"{prefix}_MODEL", "gpt-4o-mini")
        out.append(
            Provider(
                slot="openai" if prefix == "OPENAI" else prefix.lower(),
                kind="cloud",
                base_url=base,
                model=model,
                display_model=model,
                headers={"Authorization": f"Bearer {key}"},
                reasoning_effort=os.getenv(f"{prefix}_REASONING_EFFORT", ""),
                input_limit=_input_limit(prefix, base),
                max_output=_max_output(prefix),
            )
        )
    return out


def providers() -> List[Provider]:
    """Провайдеры в порядке обращения для текущего режима. Пусто — работает эмулятор."""
    current = mode()
    if current == "emulator":
        return []
    yandex = yandex_providers()
    if current == "yandex":
        return yandex
    if current == "cloud":
        return cloud_providers()
    return yandex + cloud_providers()


def find(slot: str) -> Optional[Provider]:
    """Провайдер по слоту среди настроенных, без учёта режима (для старых вызовов _call_openai(prompt, prefix))."""
    slot = (slot or "").lower()
    return next((item for item in yandex_providers() + cloud_providers() if item.slot == slot), None)


def build_payload(
    provider: Provider,
    messages: List[Dict[str, str]],
    temperature: float,
    max_output: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": provider.model,
        "temperature": temperature,
        "messages": messages,
        "max_tokens": int(max_output or provider.max_output),
    }
    effort = provider.reasoning_effort
    if provider.kind == "yandex":
        # AI Studio в режиме OpenAI: temperature / max_tokens / stream / response_format;
        # reasoning_effort — только если задан явно (gpt-oss в AI Studio его понимает: 17.8 → 8.4 с).
        if effort:
            payload["reasoning_effort"] = effort
        return payload
    if effort == "none" and "openrouter.ai" in provider.base_url:
        payload["reasoning"] = {"enabled": False}  # OpenRouter: без «размышлений» отвечает вдвое быстрее
    elif effort:
        payload["reasoning_effort"] = effort
    return payload


def chat(
    provider: Provider,
    messages: List[Dict[str, str]],
    http_json: Callable[..., Dict[str, Any]],
    *,
    temperature: float = 0.1,
    timeout: float = 90.0,
    max_output: Optional[int] = None,
) -> str:
    """Один запрос chat/completions. Сетевые ошибки и HTTP-статусы поднимаются как есть — их разбирает вызывающий."""
    data = http_json(
        f"{provider.base_url}/chat/completions",
        build_payload(provider, messages, temperature, max_output),
        headers=dict(provider.headers),
        timeout=timeout,
    )
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or "")


def status_line() -> str:
    """Для интерфейса: режим и подключённые модели по порядку (без ключей)."""
    current = mode()
    if current == "emulator":
        return "выключена (режим эмулятора)"
    items = providers()
    if not items:
        if current == "yandex":
            return "Yandex AI Studio не подключена (нужны YANDEX_API_KEY и YANDEX_FOLDER_ID)"
        return "не подключена (OPENAI_API_KEY не задан)"
    parts = [f"{item.display_model} · {item.host}" for item in items]
    return parts[0] + "".join(f" · запасная: {p}" for p in parts[1:])
