"""Паспортные регламенты (выгрузки Business Studio / SILA Union / таблицы Word).

Шаг описан карточкой, а не строкой «1. Роль делает X»:

    Шаг 4.2 Подготовка и согласование запроса на изменение маршрута
    Ответственный: Ответственный за определение маршрутов
    Участвуют: …; …
    Длительность: 16 рабочих часов
    Входы: …; …
    Выходы: …
    <поясняющий текст>

Один паспорт = одна задача на схеме. Поля не становятся ни шагами, ни дорожками:
«Ответственный» → роль, «Длительность» → часы, «Участвуют» → C в RACI,
«Входы»/«Выходы» → артефакты шага. Пояснительный текст в задачи не режется.

На выходе — канонический текст движка: одна строка на паспорт, поля — хвостом строки
(«… Участвуют: …. Входы: …. Выходы: …»); parse_regulation вынимает их до остальных эвристик.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Ключ поля → синонимы заголовков (в начале строки или ячейки, с двоеточием).
FIELD_KEYS: Dict[str, tuple] = {
    "role": ("Ответственный исполнитель", "Ответственный", "Исполнитель", "Владелец шага", "Владелец"),
    "participants": ("Участвуют", "Участники", "Соисполнители", "Консультанты"),
    "duration": ("Длительность", "Срок исполнения", "Срок", "Норматив", "Трудоёмкость", "Трудоемкость", "Время выполнения"),
    "inputs": ("Входящие документы", "Входные данные", "Входы", "Вход"),
    "outputs": ("Выходные данные", "Выходы", "Выход", "Результат"),
    "basis": ("Основание",),
}
# Эти слова — заголовки полей паспорта. Ни дорожкой, ни задачей они быть не могут.
STOP_WORDS = {
    "входы", "вход", "выходы", "выход", "длительность", "участвуют", "участники", "ответственный",
    "исполнитель", "основание", "результат", "срок", "норматив", "трудоёмкость", "трудоемкость",
    "соисполнители", "консультанты", "входные данные", "выходные данные", "входящие документы",
    "владелец шага",
}
INLINE_KEYS = ("Участвуют", "Входы", "Выходы")  # поля, которые едут хвостом канонической строки

_FIELD_RE = re.compile(
    r"^\s*[|•\-–]?\s*(?P<key>"
    + "|".join(sorted({re.escape(s) for v in FIELD_KEYS.values() for s in v}, key=len, reverse=True))
    + r")\s*[:：]\s*\|?\s*(?P<value>.*)$",
    re.I,
)
_HEADER_RE = re.compile(
    r"^\s*(?P<kind>Шаг|Пункт|Этап|Операция|Действие|Подпроцесс)\s+(?P<num>\d+(?:\.\d+)*)\.?\s*(?P<title>.*)$",
    re.I,
)
INLINE_FIELDS_RE = re.compile(r"\s*\b(Участвуют|Входы|Выходы)\s*:\s*(.*?)(?=\s*\b(?:Участвуют|Входы|Выходы)\s*:|$)")
_WORD_MARKUP_RE = re.compile(
    r"^\s*(?:Formatted\s*(?:\[[^\]]*\]?|:)[^\n]*|Formatted\s*$|(?:Bulleted|Numbered)\s*\+\s*Level\s*:[^\n]*|"
    r"(?:Level|Aligned at|Indent at|Tab after|Tab stops)\s*:[^\n]*|No bullets or numbering[^\n]*|"
    r"Deleted\s*:[^\n]*|Inserted\s*:[^\n]*|Comment\s*\[[^\]]*\][^\n]*)\s*$",
    re.I | re.M,
)


def strip_word_markup(text: str) -> str:
    """Строки рецензирования Word из PDF/копий: «Formatted [...]», «Aligned at: …», «No bullets or numbering»."""
    text = _WORD_MARKUP_RE.sub("", text or "")
    text = re.sub(r"\bFormatted\s*\[[^\]]*\]\s*:?", "", text)
    return re.sub(r"\n{3,}", "\n\n", text)


# ----------------------------------------------------------------------------- заголовок → глагол + объект
_NOUN_TO_VERB = {
    "установление": "установить", "исполнение": "исполнить", "подготовка": "подготовить",
    "согласование": "согласовать", "проверка": "проверить", "регистрация": "зарегистрировать",
    "оформление": "оформить", "направление": "направить", "получение": "получить",
    "предоставление": "предоставить", "внесение": "внести", "актуализация": "актуализировать",
    "определение": "определить", "назначение": "назначить", "проведение": "провести",
    "выполнение": "выполнить", "создание": "создать", "разработка": "разработать",
    "обработка": "обработать", "оценка": "оценить", "публикация": "опубликовать",
    "заключение": "заключить", "подписание": "подписать", "передача": "передать",
    "уведомление": "уведомить", "контроль": "проконтролировать", "анализ": "проанализировать",
    "сбор": "собрать", "доработка": "доработать", "корректировка": "скорректировать",
    "приёмка": "принять", "приемка": "принять", "выдача": "выдать", "закрытие": "закрыть",
    "открытие": "открыть", "изменение": "изменить", "обеспечение": "обеспечить",
    "организация": "организовать", "планирование": "спланировать", "расчёт": "рассчитать",
    "расчет": "рассчитать", "составление": "составить", "отправка": "отправить", "ввод": "ввести",
    "вывод": "вывести", "запрос": "запросить", "оплата": "оплатить", "закупка": "закупить",
    "поставка": "поставить", "монтаж": "смонтировать", "демонтаж": "демонтировать",
    "осмотр": "осмотреть", "ремонт": "отремонтировать", "испытание": "испытать",
    "утверждение": "утвердить", "рассмотрение": "рассмотреть", "формирование": "сформировать",
    "размещение": "разместить", "принятие": "принять", "отклонение": "отклонить",
    "информирование": "проинформировать", "архивирование": "заархивировать",
    "фиксация": "зафиксировать", "настройка": "настроить", "блокировка": "заблокировать",
    "удаление": "удалить", "добавление": "добавить", "обновление": "обновить",
    "инициирование": "инициировать", "инициация": "инициировать", "экспертиза": "провести экспертизу",
    "диагностика": "провести диагностику", "инвентаризация": "провести инвентаризацию",
    "мониторинг": "провести мониторинг",
}
_NEUTER_GEN = {"письма": "письмо", "решения": "решение", "права": "право", "дела": "дело", "места": "место"}


def _noun_to_verb(word: str, strict: bool = False) -> Optional[str]:
    """strict — только словарь: в обычном тексте «Решение принимает…» не должно стать «Решить»."""
    low = word.lower()
    if low in _NOUN_TO_VERB:
        return _NOUN_TO_VERB[low]
    if strict or len(low) < 7:
        return None
    for suffix, repl in (("вление", "вить"), ("ация", "ировать"), ("ание", "ать"), ("тие", "ть"), ("ение", "ить")):
        if low.endswith(suffix):
            return low[: -len(suffix)] + repl
    return None


def _accusative(word: str) -> str:
    """Первое слово объекта после глагола: родительный → винительный («запроса» → «запрос»).

    Только существительные с надёжным окончанием; прилагательные и одушевлённые
    («ответственного») не трогаем — у них винительный совпадает с родительным.
    """
    low = word.lower()
    if low in _NEUTER_GEN:
        return _NEUTER_GEN[low]
    if re.search(r"(ого|его|ых|их|ой|ей)$", low):
        return word
    for suffix, repl in (("ии", "ию"), ("ния", "ние"), ("тия", "тие"), ("ости", "ость"), ("ства", "ство")):
        if low.endswith(suffix):
            return word[: -len(suffix)] + repl
    if re.search(r"[кгх]и$", low):
        return word[:-1] + "у"
    if re.search(r"[^аеёиоуыэюяьй]ы$", low) and len(low) > 3:
        return word[:-1] + "у"
    if re.search(r"[бвгджзклмнпрстфхцчшщ]а$", low) and len(low) > 3:
        return word[:-1]
    return word


def nominal_to_infinitive(title: str, strict: bool = False) -> str:
    """«Подготовка и согласование запроса на изменение» → «Подготовить и согласовать запрос на изменение».

    Если первое слово не отглагольное существительное — название не меняется.
    """
    words = (title or "").strip().split()
    if not words:
        return title
    verbs: List[str] = []
    i = 0
    while i < len(words):
        verb = _noun_to_verb(words[i].strip(",;:"), strict)
        if verb is None:
            break
        verbs.append(verb)
        i += 1
        if i < len(words) and words[i].lower() in ("и", "или") and i + 1 < len(words) and _noun_to_verb(words[i + 1], strict):
            verbs.append(words[i].lower())
            i += 1
            continue
        break
    if not verbs:
        return title
    rest = words[i:]
    if rest:
        if len(rest) > 1 and re.search(r"(ого|его|ой)$", rest[0].lower()):
            # «корпоративного письма» → «корпоративное письмо», «новой заявки» → «новую заявку».
            noun = _accusative(rest[1])
            if noun != rest[1]:
                adj = rest[0]
                if adj.lower().endswith("ой"):
                    adj = adj[:-2] + "ую"
                elif re.search(r"(о|е|ие|ние|тие)$", noun.lower()):
                    adj = adj[:-3] + ("ее" if adj.lower().endswith("его") else "ое")
                else:
                    adj = adj[:-3] + ("ий" if adj.lower().endswith("его") else "ый")
                rest[0], rest[1] = adj, noun
        else:
            rest[0] = _accusative(rest[0])
    phrase = " ".join(verbs + rest)
    return phrase[:1].upper() + phrase[1:]


# ----------------------------------------------------------------------------- разбор паспортов
@dataclass
class Passport:
    num: str
    title: str
    role: str = ""
    participants: List[str] = field(default_factory=list)
    duration: str = ""
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    basis: str = ""
    description: List[str] = field(default_factory=list)


@dataclass
class PassportDoc:
    title: str
    steps: List[Passport]
    sla: str = ""


def _field_key(raw: str) -> Optional[str]:
    low = raw.lower()
    for key, names in FIELD_KEYS.items():
        if any(low == n.lower() for n in names):
            return key
    return None


def _split_items(value: str) -> List[str]:
    items = []
    for part in re.split(r"\s*(?:;|\n|•)\s*", value or ""):
        part = part.strip(" .,|-–•")
        if part and part.lower() not in ("или", "и", "или/и", "и/или"):
            items.append(part)
    return items


def parse_passports(text: str) -> Optional[PassportDoc]:
    """Паспортный регламент → структура. Не паспорт (нет «Шаг X.Y» с полями) → None."""
    lines = [ln.rstrip() for ln in strip_word_markup(text or "").splitlines()]
    headers = [(i, _HEADER_RE.match(ln)) for i, ln in enumerate(lines) if _HEADER_RE.match(ln)]
    fields_total = sum(1 for ln in lines if _FIELD_RE.match(ln))
    if not headers or fields_total < 2:
        return None
    depth = max(m.group("num").count(".") for _, m in headers)
    title = ""
    steps: List[Passport] = []
    current: Optional[Passport] = None
    key: Optional[str] = None
    continues = False  # прошлая строка поля оборвалась на «;», «,» или «или/и» — перечисление идёт дальше
    for line in lines:
        s = line.strip().strip("|").strip()
        if not s:
            continue
        head = _HEADER_RE.match(s)
        if head:
            # Объединённые ячейки Word: «Шаг 4.1 X | Шаг 4.1 X» — берём одну копию.
            name = head.group("title").split(" | ")[0].strip(" .|")
            if head.group("num").count(".") == depth:
                current = Passport(num=head.group("num"), title=name)
                steps.append(current)
                key = None
            elif not steps and not title:
                title = f"{head.group('kind')} {head.group('num')}. {name}".strip()
            continue
        fm = _FIELD_RE.match(s)
        if fm and current is not None:
            key = _field_key(fm.group("key"))
            value = fm.group("value").strip(" |")
            if key == "role":
                current.role = value.strip(" .;")
            elif key == "duration":
                current.duration = value
            elif key == "basis":
                current.basis = value
            elif key in ("participants", "inputs", "outputs"):
                getattr(current, key).extend(_split_items(value))
            continues = bool(re.search(r"[;,]$|\b(?:или|и)(?:/(?:или|и))?$", value))
            continue
        if current is None:
            if not title and not re.match(r"^(?:регламент|название)\b", s, re.I):
                title = s.strip(" .")
            continue
        # Продолжение многострочного поля: перечисление («…;», «или/и», строчная буква, буллит).
        if key in ("participants", "inputs", "outputs") and (continues or re.match(r"^[•\-–]|^(?:или|и)(?:/|\b)", s)):
            getattr(current, key).extend(_split_items(s))
            continues = bool(re.search(r"[;,]$|^(?:или|и)(?:/(?:или|и))?$", s))
            continue
        if key == "role" and not current.role:
            current.role = s.strip(" .;")
            continue
        key = None
        continues = False
        current.description.append(s.split(" | ")[0])
    steps = [p for p in steps if p.title]
    if not steps or not any(p.role or p.duration or p.inputs or p.outputs for p in steps):
        return None
    return PassportDoc(title=title or "Бизнес-процесс по регламенту", steps=steps)


def _clean_role(role: str) -> str:
    role = re.sub(r"\s+", " ", role or "").strip(" .;|")
    return "" if role.lower() in STOP_WORDS else role


def render_canonical(doc: PassportDoc) -> str:
    """Канонический текст движка: одна строка на паспорт, поля — хвостом строки."""
    out = [f"Регламент: {doc.title}", ""]
    for n, step in enumerate(doc.steps, start=1):
        role = _clean_role(step.role) or "Ответственный исполнитель"
        name = nominal_to_infinitive(step.title)
        line = f"{n}. {role}: {name}"
        if step.duration:
            line += f" ({step.duration.strip(' .')})"
        line += "."
        participants = [p for p in step.participants if _clean_role(p) and _clean_role(p) != role]
        if participants:
            line += " Участвуют: " + "; ".join(_clean_role(p) for p in participants) + "."
        if step.inputs:
            line += " Входы: " + "; ".join(step.inputs) + "."
        if step.outputs:
            line += " Выходы: " + "; ".join(step.outputs) + "."
        out.append(line)
    return "\n".join(out) + "\n"


def passport_to_canonical(text: str) -> Optional[str]:
    doc = parse_passports(text)
    return render_canonical(doc) if doc is not None else None


def split_inline_fields(body: str) -> tuple:
    """Строка шага → (текст без полей, {participants, inputs, outputs})."""
    found: Dict[str, List[str]] = {"participants": [], "inputs": [], "outputs": []}
    names = {"участвуют": "participants", "входы": "inputs", "выходы": "outputs"}
    first = INLINE_FIELDS_RE.search(body or "")
    if not first:
        return body, found
    core = body[: first.start()].rstrip(" .")
    for match in INLINE_FIELDS_RE.finditer(body[first.start():]):
        found[names[match.group(1).lower()]].extend(_split_items(match.group(2)))
    return core, found


# ----------------------------------------------------------------------------- таблицы, одна строка, капс
_COL_ACTION = re.compile(r"действ|операц|наименован|содержан|мероприят|работ|описан|шаг процесса|этап", re.I)
_COL_ROLE = re.compile(r"исполнит|ответствен|роль|кто|участник|подразделен|владел", re.I)
_COL_TIME = re.compile(r"срок|длительн|время|норматив|трудо[её]мк", re.I)
_COL_NUM = re.compile(r"^(?:№|n|#|п/п|№\s*п/п|шаг|пункт)$", re.I)


def _cells(line: str, sep: str) -> List[str]:
    parts = [p.strip() for p in line.strip().strip("|").split(sep)] if sep == "|" else [p.strip() for p in line.split("\t")]
    return parts


def tables_to_steps(text: str) -> str:
    """Таблица регламента (Word → «| № | Действие | Исполнитель | Срок |» или через табуляцию) → «N. Роль: действие (срок)».

    Колонки ищем по заголовку. Без заголовка с «Действие» и «Исполнитель» таблицу не трогаем.
    """
    lines = (text or "").splitlines()
    out: List[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        sep = "|" if line.count("|") >= 2 else ("\t" if line.count("\t") >= 2 else "")
        if not sep:
            out.append(line)
            i += 1
            continue
        header = _cells(line, sep)
        act = next((k for k, h in enumerate(header) if _COL_ACTION.search(h)), None)
        role = next((k for k, h in enumerate(header) if _COL_ROLE.search(h) and k != act), None)
        if act is None or role is None:
            out.append(line)
            i += 1
            continue
        num = next((k for k, h in enumerate(header) if _COL_NUM.match(h.strip())), None)
        dur = next((k for k, h in enumerate(header) if _COL_TIME.search(h) and k not in (act, role)), None)
        i += 1
        n = 0
        while i < len(lines) and (lines[i].count(sep) >= 2 or re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[i]) and lines[i].strip()):
            row = lines[i]
            i += 1
            if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", row):
                continue  # разделитель Markdown «|---|---|»
            cells = _cells(row, sep)
            get = lambda k: cells[k].strip() if k is not None and k < len(cells) else ""  # noqa: E731
            action, who = get(act), get(role)
            if not action:
                continue
            n += 1
            label = get(num) if num is not None and re.fullmatch(r"\d+", get(num) or "") else str(n)
            when = get(dur)
            action = action[:1].lower() + action[1:] if re.match(r"^[А-ЯЁ][а-яё]+(?:ть|ти|чь)\b", action) else action
            step = f"{label}. {who}: {action[:1].upper() + action[1:]}" if who else f"{label}. {action}"
            if when:
                step += f" ({when})"
            out.append(step.rstrip(" .") + ".")
            changed = True
    return "\n".join(out) if changed else text


def split_inline_numbering(text: str) -> str:
    """Регламент одной строкой: «Регламент: Х. 1. … 2. …» → по строке на пункт."""
    src = text or ""
    if src.count("\n") > 2 or len(re.findall(r"(?:^|[.!?;]\s+)\d{1,3}[.)]\s+\S", src)) < 2:
        return src
    src = re.sub(r"(?<=[.!?;])\s+(?=\d{1,3}[.)]\s+\S)", "\n", src)
    src = re.sub(r"^((?:Регламент|Процесс|Название)\s*[:—–-][^\n]*?[.!?])\s+(?=\S)", r"\1\n", src, flags=re.I)
    return src


# Аббревиатуры, которые в отраслевых регламентах пишутся заглавными; остальное в капсе — просто слова («ГРУЗ»).
_ABBREVIATIONS = {
    "ИС", "АС", "АИС", "ГИС", "СЭД", "ЭДО", "ЭТП", "ЕИС", "ТП", "ЛЭП", "ВЛ", "КЛ", "ПС", "КТП", "РП", "РЗА", "ДЭС",
    "ОТК", "ЦОК", "СОД", "РВБ", "ОМТО", "АСУ", "ФАС", "ЦЗК", "ТК", "СБ", "ТЗ", "ТУ", "КП", "ИТ", "ООО", "ПАО", "ОАО",
    "АО", "РФ", "ТЭЦ", "ГЭС", "АЭС", "СИЗ", "ЕОЛ", "ПЭО", "НД", "ПТЭ", "SLA", "CRM", "ERP", "SAP", "SCADA", "ОИК",
}


def soften_caps(text: str) -> str:
    """Регламент капсом: «КЛАДОВЩИК ПРИНИМАЕТ ГРУЗ» → «Кладовщик принимает груз». Аббревиатуры до 4 букв — как есть."""
    letters = re.findall(r"[А-ЯЁа-яё]", text or "")
    if len(letters) < 40 or sum(1 for ch in letters if ch.isupper()) / len(letters) < 0.8:
        return text

    def word(m: "re.Match[str]") -> str:
        w = m.group(0)
        return w if w in _ABBREVIATIONS else w.lower()

    lines = []
    for line in text.splitlines():
        low = re.sub(r"(?<![\w-])[А-ЯЁA-Z]+(?![\w-])", word, line)  # «ИС3», «110КВ» — с цифрами, не трогаем
        low = re.sub(r"^(\s*(?:\d+[.)]\s+)?)([а-яёa-z])", lambda m: m.group(1) + m.group(2).upper(), low)
        low = re.sub(r"([.!?:]\s+)([а-яё])", lambda m: m.group(1) + m.group(2).upper(), low)
        lines.append(low)
    return "\n".join(lines)
