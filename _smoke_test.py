"""Смоук-тест story_main.py: справочники, мастер создания героя в личке, /join, ход мастера.

Запуск: python _smoke_test.py
Не часть продакшена: файл нужен только для ручной проверки полного цикла бота.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
os.environ["STORY_DB_PATH"] = str(BASE / "_smoke_story.db")
os.environ["BOT_TOKEN"] = os.environ.get("BOT_TOKEN") or "111:smoke"
os.environ["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY") or "smoke-key"

sys.path.insert(0, str(BASE))

import aiosqlite  # noqa: E402
import story_main as sb  # noqa: E402

from aiogram.enums import ChatType  # noqa: E402
from aiogram.filters import CommandObject  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

GROUP_ID = -1001234567890          # игровой чат
P1, P2, P3, P4, P5, P6 = 501, 502, 503, 504, 505, 506
STRANGER = 999
BOT_ID, BOT_USERNAME = 777, "story_bot_test"

SENT: list[str] = []               # тексты отправленных сообщений
KEYBOARDS: list[Any] = []          # reply_markup каждого сообщения (None, если нет)
ANSWERS: list[str] = []            # ответы callback.answer()


class FakeDeepSeek:
    """Заглушка клиента DeepSeek: возвращает скриптованный ответ мастера."""

    def __init__(
        self,
        target: int = P2,
        hp_diff: int = -7,
        xp_diff: int = 40,
        injuries: Optional[list[str]] = None,
        items: Optional[list[str]] = None,
    ) -> None:
        self.calls = 0
        self.target = target
        self.hp_diff = hp_diff
        self.xp_diff = xp_diff
        self.injuries = ["Отрублена левая нога"] if injuries is None else injuries
        self.items = ["Кинжал гнолла"] if items is None else items

    async def chat(self, messages, temperature=None, max_tokens=None, attempts=3):  # noqa: ANN001
        self.calls += 1
        assert messages[0]["role"] == "system"
        prompt = messages[1]["content"]
        assert "[АКТИВНЫЙ ИГРОК В ЭТОТ ХОД]" in prompt
        # инъекция физического состояния и травм из ТЗ
        assert "[ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ]:" in prompt, prompt
        assert "- Тяжёлые травмы:" in prompt and "- Действующие эффекты:" in prompt, prompt
        assert "ПРАВИЛО МАСТЕРА: Любые действия героя строго ограничены его травмами!" in prompt
        change = {
            "user_id": self.target,
            "hp_diff": self.hp_diff,
            "xp_diff": self.xp_diff,
            "add_injuries": self.injuries,
            "add_items": self.items,
        }
        return (
            "Торн обрушивает меч на гнолла, и клинок рассекает ключицу врага.\n\n"
            "Зверь оседает в грязь, а отряд получает передышку.\n\n"
            "<!--STATE_UPDATE: "
            + json.dumps({"changes": [change]}, ensure_ascii=False)
            + "-->"
        )

    async def summarize(self, previous_summary: str, turns) -> str:  # noqa: ANN001
        return f"Сводка: {previous_summary or 'новая кампания'} | записей: {len(list(turns))}"


class FakeResponse:
    """Ложный ответ httpx: только то, что читает DeepSeekClient."""

    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "Ответ мастера."}}]}


class FakeHttp:
    """Ложный httpx.AsyncClient: запоминает тела POST-запросов к DeepSeek."""

    def __init__(self) -> None:
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:  # noqa: A002
        self.bodies.append((url, json))
        return FakeResponse()


class FakeBot:
    """Минимальная замена aiogram.Bot: копит сообщения и клавиатуры."""

    id = BOT_ID
    username = BOT_USERNAME

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001
        SENT.append(text)
        KEYBOARDS.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=len(SENT))

    async def send_chat_action(self, chat_id, action):  # noqa: ANN001
        return True

    async def get_chat_member(self, chat_id, user_id):  # noqa: ANN001
        return SimpleNamespace(status="administrator")


def make_message(
    user_id: int, username: str, text: str, private: bool = False, reply_to_bot: bool = False
) -> Any:
    """Ложное сообщение: в личке chat.id == user_id, в группе — GROUP_ID."""
    reply = SimpleNamespace(from_user=SimpleNamespace(id=BOT_ID)) if reply_to_bot else None
    return SimpleNamespace(
        bot=FakeBot(),
        message_id=100 + len(SENT),
        chat=SimpleNamespace(
            id=user_id if private else GROUP_ID,
            type=ChatType.PRIVATE if private else ChatType.SUPERGROUP,
        ),
        from_user=SimpleNamespace(id=user_id, username=username, is_bot=False),
        text=text,
        caption=None,
        entities=[],
        reply_to_message=reply,
    )


class FakeCallback:
    """Ложный CallbackQuery для нажатия инлайн-кнопок мастера."""

    def __init__(self, user_id: int, username: str, data: str, chat_id: int) -> None:
        self.bot = FakeBot()
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, username=username, is_bot=False)
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=ChatType.PRIVATE),
            bot=self.bot,
            message_id=1000 + len(SENT),
        )

    async def answer(self, text: Optional[str] = None, show_alert: bool = False) -> None:
        ANSWERS.append(text or "")


STORAGE = MemoryStorage()


def fsm(user_id: int) -> FSMContext:
    """FSM-контекст пользователя в личке (chat_id == user_id)."""
    return FSMContext(
        storage=STORAGE,
        key=StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id),
    )


def button_labels(keyboard: Any) -> list[str]:
    """Все подписи кнопок клавиатуры по порядку."""
    return [button.text for row in keyboard.inline_keyboard for button in row]


def button_data(keyboard: Any, label: str) -> str:
    """callback_data кнопки по подписи."""
    for row in keyboard.inline_keyboard:
        for button in row:
            if button.text == label:
                return button.callback_data or ""
    raise AssertionError(f"Кнопка «{label}» не найдена среди {button_labels(keyboard)}")


def button_urls(keyboard: Any) -> list[str]:
    """URL-кнопки клавиатуры (кнопка-ссылка на личку бота)."""
    if keyboard is None:
        return []
    return [button.url for row in keyboard.inline_keyboard for button in row if button.url]


def refusal_prefixes() -> list[str]:
    """Начала шаблонов отказа из story_main (до первого {placeholder}).

    Текст отказа выбирается через random.choice, поэтому проверять надо именно
    принадлежность ответа одному из шаблонов, а не наличие конкретных слов.
    """
    return [template.split("{")[0].strip().rstrip(",") for template in sb.REFUSAL_TEMPLATES]


async def create_hero(
    user_id: int, username: str, char_name: str, race: str, char_class: str
) -> dict[str, Any]:
    """Проходит весь мастер в личке (/start → имя → раса → класс) и отдаёт лист героя."""
    state = fsm(user_id)
    await sb.cmd_start(make_message(user_id, username, "/start", private=True), state)
    assert await state.get_state() == sb.CharCreation.waiting_name
    await sb.step_name(make_message(user_id, username, char_name, private=True), state)
    assert await state.get_state() == sb.CharCreation.waiting_race
    await sb.step_race(
        FakeCallback(user_id, username, button_data(KEYBOARDS[-1], race), chat_id=user_id), state
    )
    assert await state.get_state() == sb.CharCreation.waiting_class
    await sb.step_class(
        FakeCallback(user_id, username, button_data(KEYBOARDS[-1], char_class), chat_id=user_id),
        state,
    )
    assert await state.get_state() is None
    profile = await sb.get_profile_player(user_id)
    assert profile is not None, "герой должен сохраниться в личке бота"
    return profile


async def main() -> None:
    sb.configure_logging()
    cat = sb.load_catalog()
    races = [race.name for race in cat.all_races]
    classes = [char_class.name for char_class in cat.all_classes]

    # --- [1] справочники: ровно 9 рас и 6 классов --------------------------------
    print(f"[1] Справочники: оружий={len(cat.all_weapons)}, брони={len(cat.all_armors)}, "
          f"щитов={len(cat.all_shields)}, рас={len(races)}, классов={len(classes)}")
    assert len(races) == 9, f"ожидалось 9 рас из race.txt, разобрано {len(races)}: {races}"
    assert races == [
        "Человек", "Нежить", "Эльф", "Дварф", "Тифлинг",
        "Драконорожденный", "Полурослик / Хафлинг", "Орк", "Гном",
    ], races
    assert cat.find_race("хафлинг") is cat.find_race("полурослик")
    assert cat.find_race("голиаф") is None, "в race.txt нет Голиафа — раса не должна резолвиться"
    assert cat.find_race("аасимар") is None, cat.find_race("аасимар")
    assert cat.find_class("1") is None and cat.find_class("200") is None
    assert len(classes) == 6, f"ожидалось 6 классов из class.txt, разобрано {len(classes)}: {classes}"
    assert classes == ["Воин", "Плут", "Маг", "Следопыт", "Инквизитор", "Церковник"], classes
    assert all(cls.role and len(cls.perks) == 10 for cls in cat.all_classes), [
        (cls.name, len(cls.perks)) for cls in cat.all_classes
    ]
    assert cat.xp_thresholds[1] == 200 and cat.xp_thresholds[9] == 10000, cat.xp_thresholds
    print(f"    Расы ({len(races)}): {', '.join(races)}")
    print(f"    Классы ({len(classes)}): {', '.join(classes)}")

    # --- [2] стартовое снаряжение классов (фиксировано ТЗ) ----------------------
    expected_kits: dict[str, tuple[str, int, str, int, bool, int]] = {
        "Воин": ("Длинный меч", 5, "Кольчужная рубаха", 2, True, 28),
        "Плут": ("Парные кинжалы", 3, "Кожаный доспех", 1, False, 22),
        "Маг": ("Боевой посох", 6, "Мантия", 0, False, 18),
        "Следопыт": ("Охотничий лук", 4, "Кожаная куртка", 1, False, 24),
        "Инквизитор": ("Боевой молот", 5, "Латный нагрудник", 3, False, 26),
        "Церковник": ("Освященный посох", 3, "Ряса с кольчужкой", 1, False, 22),
    }
    assert len(sb.STARTING_KITS) == 6, sorted(sb.STARTING_KITS)
    for class_name, spec in expected_kits.items():
        char_class = cat.find_class(class_name)
        kit = sb.start_kit_for(char_class)
        got = (kit.weapon_name, kit.weapon_dmg, kit.armor_name, kit.armor_def, kit.shield, kit.hp)
        assert got == spec, (class_name, got, spec)
        assert kit.hp == char_class.base_hp, (class_name, kit.hp, char_class.base_hp)
    assert [kit.char_class for kit in sb.class_kits()] == classes, sb.class_kits()
    print("[2] Стартовые комплекты из ТЗ на месте:")
    for kit in sb.class_kits():
        print(f"    • {sb.kit_summary(kit)}")

    # --- [3] мастер создания героя в личке: шаги, кнопки, валидация --------------
    await sb.init_db()
    await sb.reset_chat(GROUP_ID)
    sb.BOT_INFO["id"] = BOT_ID
    sb.BOT_INFO["username"] = BOT_USERNAME
    assert sb.creation_deeplink() == f"https://t.me/{BOT_USERNAME}?start=create"

    state = fsm(P1)
    SENT.clear()
    KEYBOARDS.clear()
    ANSWERS.clear()
    await sb.cmd_start(make_message(P1, "thorn", "/start", private=True), state)
    assert await state.get_state() == sb.CharCreation.waiting_name
    assert "шаг 1/3" in SENT[0] and "имя героя" in SENT[0], SENT[0]
    assert button_labels(KEYBOARDS[0]) == ["✖️ Отменить создание"], button_labels(KEYBOARDS[0])

    # слишком длинное имя и имя-команда отклоняются, состояние не сдвигается
    await sb.step_name(make_message(P1, "thorn", "Я" * 40, private=True), state)
    assert "1 до 32" in SENT[-1] and await state.get_state() == sb.CharCreation.waiting_name
    await sb.step_name(make_message(P1, "thorn", "/join", private=True), state)
    assert "не может быть командой" in SENT[-1]
    assert await state.get_state() == sb.CharCreation.waiting_name
    print("[3] Шаг 1: имя запрошено, некорректные имена отклоняются")

    # шаг 2: имя принято (кавычки обрезаны), показываются 9 рас кнопками
    await sb.step_name(make_message(P1, "thorn", "«Торн»", private=True), state)
    assert await state.get_state() == sb.CharCreation.waiting_race
    assert "Шаг 2/3" in SENT[-1]
    assert button_labels(KEYBOARDS[-1]) == [
        "Человек", "Нежить", "Эльф", "Дварф", "Тифлинг",
        "Драконорожденный", "Полурослик", "Орк", "Гном", "✖️ Отмена",
    ], button_labels(KEYBOARDS[-1])
    assert button_data(KEYBOARDS[-1], "Полурослик") == "race:6"
    print("[4] Шаг 2: 9 рас инлайн-кнопками")

    # шаг 3: раса выбрана, показываются 6 классов и их стартовое снаряжение
    await sb.step_race(
        FakeCallback(P1, "thorn", button_data(KEYBOARDS[-1], "Дварф"), chat_id=P1), state
    )
    assert await state.get_state() == sb.CharCreation.waiting_class
    step3 = SENT[-1]
    assert "Шаг 3/3" in step3
    assert "Воин: HP 28 | Длинный меч (урон 5) | Кольчужная рубаха (броня 2) | Щит (+1)" in step3
    assert "Маг: HP 18 | Боевой посох (урон чарами 6) | Мантия (броня 0) | без щита" in step3
    assert button_labels(KEYBOARDS[-1]) == [
        "Воин", "Плут", "Маг", "Следопыт", "Инквизитор", "Церковник", "✖️ Отмена",
    ], button_labels(KEYBOARDS[-1])
    print("[5] Шаг 3: 6 классов инлайн-кнопками + стартовое снаряжение в подсказке")

    # шаг 4: класс выбран — комплект выдан, герой сохранён, карточка показана
    await sb.step_class(
        FakeCallback(P1, "thorn", button_data(KEYBOARDS[-1], "Воин"), chat_id=P1), state
    )
    assert await state.get_state() is None
    final = SENT[-1]
    assert "Персонаж успешно создан!" in final and "/join" in final, final
    assert "Стартовое снаряжение" in final
    assert "Длинный меч (фиксированный урон: 5)" in final, final
    assert "Кольчужная рубаха (поглощение броней: 2, щит +1)" in final, final
    assert "Здоровье: 32/32 HP" in final, final          # 28 HP воина + 4 HP дварфа
    assert "Каменная стойкость дварфа" in final, final
    assert "🩹 Травмы: Нет" in final, final
    assert ANSWERS, "callback.answer() должен подтверждать нажатие кнопки"
    p1 = await sb.get_profile_player(P1)
    assert p1["chat_id"] == P1 and p1["char_name"] == "Торн", dict(p1)
    assert p1["race"] == "Дварф" and p1["char_class"] == "Воин", dict(p1)
    assert (p1["weapon_name"], p1["weapon_dmg"]) == ("Длинный меч", 5), dict(p1)
    assert (p1["armor_name"], p1["armor_def"], p1["shield_def"]) == ("Кольчужная рубаха", 2, 1)
    assert (p1["hp"], p1["max_hp"], p1["level"], p1["xp"], p1["next_level_xp"]) == (32, 32, 1, 0, 200)
    assert p1["active_abilities"] and "Второе дыхание" in p1["active_abilities"]
    print(f"[6] Шаг 4: герой сохранён в личке — {p1['char_name']} ({p1['race']} {p1['char_class']}), "
          f"HP {p1['hp']}/{p1['max_hp']}, урон {p1['weapon_dmg']}, "
          f"броня {p1['armor_def']}, щит {p1['shield_def']}")

    # --- [7] остальные 5 классов через тот же мастер + расовые бонусы ------------
    created = {
        P2: ("kela", "Кела", "Эльф", "Следопыт"),
        P3: ("grom", "Гром", "Тифлинг", "Плут"),
        P4: ("mag", "Магнус", "Человек", "Маг"),
        P5: ("inq", "Дункан", "Орк", "Инквизитор"),
        P6: ("priest", "Одо", "Гном", "Церковник"),
    }
    for user_id, (username, char_name, race, char_class) in created.items():
        await create_hero(user_id, username, char_name, race, char_class)
        if char_class == "Маг":
            assert "атака чарами" in SENT[-1], SENT[-1]
        if race == "Эльф":
            assert "Меткий стрелок эльфа" in SENT[-1], SENT[-1]
        if race == "Орк":
            assert "Смертоносная сила орка" in SENT[-1], SENT[-1]

    expected_stats: dict[int, tuple[int, int, int, int]] = {
        P2: (24, 5, 1, 0),   # Эльф: +1 к урону «Охотничьего лука» (4 → 5)
        P3: (22, 3, 1, 0),   # Плут: парные кинжалы 3, щита нет
        P4: (18, 6, 0, 0),   # Маг: урон чарами 6, Мантия 0
        P5: (26, 6, 3, 0),   # Орк: +1 к фиксированному урону (5 → 6)
        P6: (22, 3, 1, 0),   # Церковник: освящённый посох 3, ряса с кольчужкой 1
    }
    for user_id, spec in expected_stats.items():
        row = await sb.get_profile_player(user_id)
        got = (row["max_hp"], row["weapon_dmg"], row["armor_def"], row["shield_def"])
        assert got == spec, (row["char_name"], got, spec)
    print("[7] Комплекты выданы автоматически, расовые бонусы применены:")
    for user_id in (P1, *created):
        row = await sb.get_profile_player(user_id)
        print(f"    {row['char_name']:>8} | {row['race']:<20} {row['char_class']:<11} "
              f"HP {row['max_hp']:>2} | {row['weapon_name']} ({row['weapon_dmg']}) | "
              f"{row['armor_name']} ({row['armor_def']}) | щит {row['shield_def']}")

    # --- [8] /cancel, устаревшая кнопка и повторное создание героя ----------------
    state = fsm(P4)
    await sb.cmd_create(make_message(P4, "mag", "/create", private=True), state)
    assert await state.get_state() == sb.CharCreation.waiting_name
    assert "Прежний герой «Магнус»" in SENT[-1], SENT[-1]
    await sb.cmd_cancel(make_message(P4, "mag", "/cancel", private=True), state)
    assert await state.get_state() is None and "отменено" in SENT[-1]

    await sb.cmd_create(make_message(P4, "mag", "/create", private=True), state)
    await sb.step_name(make_message(P4, "mag", "Магнус II", private=True), state)
    await sb.step_race(FakeCallback(P4, "mag", "race:99", chat_id=P4), state)
    assert await state.get_state() is None and "устарела" in SENT[-1], SENT[-1]

    # кнопка «Создать героя заново» и замена героя в личке
    await sb.cb_restart(FakeCallback(P4, "mag", sb.RESTART_CB, chat_id=P4), state)
    assert await state.get_state() == sb.CharCreation.waiting_name
    await sb.step_name(make_message(P4, "mag", "Магнус II", private=True), state)
    await sb.step_race(
        FakeCallback(P4, "mag", button_data(KEYBOARDS[-1], "Человек"), chat_id=P4), state
    )
    await sb.step_class(
        FakeCallback(P4, "mag", button_data(KEYBOARDS[-1], "Маг"), chat_id=P4), state
    )
    profiles = await sb.db_fetchall(
        "SELECT * FROM players WHERE user_id = ? AND chat_id = ?", (P4, P4)
    )
    assert len(profiles) == 1 and profiles[0]["char_name"] == "Магнус II", profiles
    print(f"[8] /cancel, устаревшая кнопка и пересоздание героя: в личке {profiles[0]['char_name']}")

    # --- [9] /join в игровом чате БЕЗ аргументов ---------------------------------
    SENT.clear()
    KEYBOARDS.clear()
    await sb.cmd_join(
        make_message(P1, "thorn", "/join"),
        CommandObject(prefix="/", command="join", args=None),
    )
    assert "вступает в отряд" in SENT[0], SENT[0]
    assert "Торн" in SENT[0] and "Длинный меч" in SENT[0], SENT[0]
    row = await sb.get_player(GROUP_ID, P1)
    assert row is not None and row["chat_id"] == GROUP_ID, row
    assert (row["weapon_name"], row["weapon_dmg"], row["max_hp"], row["shield_def"]) == (
        "Длинный меч", 5, 32, 1,
    ), row
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.parse_turn_order(campaign["turn_order_json"]) == [P1], campaign["turn_order_json"]
    assert sb.as_int(campaign["current_turn_user_id"]) == P1, campaign
    print(f"[9] /join без аргументов: {row['char_name']} перенесён из лички в очередь "
          f"{sb.parse_turn_order(campaign['turn_order_json'])}")

    # второй герой и повторный /join того же игрока
    await sb.cmd_join(
        make_message(P2, "kela", "/join"),
        CommandObject(prefix="/", command="join", args=None),
    )
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.parse_turn_order(campaign["turn_order_json"]) == [P1, P2], campaign["turn_order_json"]
    assert sb.as_int(campaign["current_turn_user_id"]) == P1
    SENT.clear()
    await sb.cmd_join(
        make_message(P1, "thorn", "/join"),
        CommandObject(prefix="/", command="join", args=None),
    )
    assert "уже в отряде" in SENT[0], SENT[0]
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.parse_turn_order(campaign["turn_order_json"]) == [P1, P2], campaign["turn_order_json"]
    print("[10] Повторный /join: герой не дублируется в очереди")

    # --- [11] /join без созданного героя: кнопка-ссылка на личку ------------------
    SENT.clear()
    KEYBOARDS.clear()
    await sb.cmd_join(
        make_message(STRANGER, "stranger", "/join"),
        CommandObject(prefix="/", command="join", args=None),
    )
    assert sb.NO_HERO_IN_GROUP in SENT[0], SENT[0]
    assert f"https://t.me/{BOT_USERNAME}?start=create" in button_urls(KEYBOARDS[0]), KEYBOARDS[0]
    assert await sb.get_player(GROUP_ID, STRANGER) is None
    print(f"[11] /join без героя: подсказка + кнопка {button_urls(KEYBOARDS[0])[0]}")

    # /join в личке и /start в группе не запускают мастер
    SENT.clear()
    await sb.cmd_join(
        make_message(P1, "thorn", "/join", private=True),
        CommandObject(prefix="/", command="join", args=None),
    )
    assert "в личных сообщениях" in SENT[0], SENT[0]
    SENT.clear()
    KEYBOARDS.clear()
    await sb.cmd_start(make_message(P1, "thorn", "/start"), fsm(P1))
    assert "Как создать героя" in SENT[0], SENT[0]
    assert f"https://t.me/{BOT_USERNAME}?start=create" in button_urls(KEYBOARDS[0]), KEYBOARDS[0]
    SENT.clear()
    await sb.cmd_create(make_message(P1, "thorn", "/create"), fsm(P1))
    assert sb.NO_HERO_IN_GROUP in SENT[0], SENT[0]
    # /party и /reset_story в личке не трогают ни профиль, ни кампанию чата
    SENT.clear()
    await sb.cmd_party(make_message(P1, "thorn", "/party", private=True))
    assert "личный кабинет" in SENT[0] and "/create" in SENT[0], SENT[0]
    SENT.clear()
    await sb.cmd_reset_story(make_message(P1, "thorn", "/reset_story", private=True))
    assert await sb.get_profile_player(P1) is not None, "личный кабинет в личке не сбрасывается"
    assert len(await sb.list_players(GROUP_ID)) == 2, "отряд чата не тронут"
    print("[12] Групповые /start и /create отправляют создавать героя в личку")

    # --- [13] ход мастера через DeepSeek (заглушка) -------------------------------
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.as_int(campaign["current_turn_user_id"]) == P1, campaign
    fake = FakeDeepSeek()
    sb.deepseek = fake
    SENT.clear()
    p1_row = await sb.get_player(GROUP_ID, P1)
    await sb.run_turn(
        make_message(P1, "thorn", "Рублю гнолла мечом", reply_to_bot=True),
        p1_row,
        "Рублю гнолла мечом",
    )
    campaign = await sb.get_campaign(GROUP_ID)
    p2_row = await sb.get_player(GROUP_ID, P2)
    assert fake.calls == 1
    assert sb.as_int(campaign["current_turn_user_id"]) == P2, campaign
    assert p2_row["hp"] == int(p2_row["max_hp"]) - 7, dict(p2_row)
    assert p2_row["xp"] == 40 and p2_row["level"] == 1, dict(p2_row)
    assert sb.player_injuries(p2_row) == ["Отрублена левая нога"], dict(p2_row)
    assert "Кинжал гнолла" in sb.inventory_items(p2_row["inventory"]), dict(p2_row)
    assert "STATE_UPDATE" not in SENT[0] and "<!--" not in SENT[0]
    assert "Ход переходит к" in SENT[0] and "Статус отряда" in SENT[0]
    assert "🩹 <b>Травмы:</b>" in SENT[0], SENT[0]
    assert "получена травма «Отрублена левая нога»" in SENT[0], SENT[0]
    print(f"[13] Ход П1: мастер ответил, очередь ушла к П2 "
          f"(Кела HP {p2_row['hp']}/{p2_row['max_hp']}, XP {p2_row['xp']}, "
          f"травмы {sb.player_injuries(p2_row)})")

    # ход вне очереди перехватывается без вызова API
    SENT.clear()
    before = fake.calls
    await sb.on_group_message(make_message(P1, "thorn", "@story_bot_test снова бью!"))
    assert fake.calls == before, "мастер не должен вызываться вне очереди"
    prefixes = refusal_prefixes()
    matched = next((prefix for prefix in prefixes if SENT and SENT[0].startswith(prefix)), "")
    assert matched, f"ответ должен быть одним из шаблонов отказа {prefixes}: {SENT}"
    assert "@kela" in SENT[0], SENT
    print(f"[14] Перехват вне очереди (без вызова API): {SENT[0][:90]}…")

    # игрок вне отряда
    SENT.clear()
    KEYBOARDS.clear()
    await sb.on_group_message(make_message(STRANGER, "stranger", "@story_bot_test привет"))
    assert SENT and "не в отряде" in SENT[0], SENT
    assert sb.NO_HERO_IN_GROUP in SENT[0], SENT[0]
    assert button_urls(KEYBOARDS[0]), KEYBOARDS[0]
    print("[15] Незнакомцу в группе предложены личка (/create) и /join")

    # ход Келы + левел-ап Торна через STATE_UPDATE
    fake.target = P1
    fake.xp_diff = 900
    SENT.clear()
    await sb.on_group_message(make_message(P2, "kela", "@story_bot_test Стреляю из лука!"))
    campaign = await sb.get_campaign(GROUP_ID)
    p1_after = await sb.get_player(GROUP_ID, P1)
    assert fake.calls == 2
    assert sb.as_int(campaign["current_turn_user_id"]) == P1, campaign
    assert p1_after["level"] >= 3 and p1_after["hp"] == p1_after["max_hp"], dict(p1_after)
    assert "достигает" in SENT[0] and "уровн" in SENT[0], SENT[0][-500:]
    print(f"[16] Левел-ап: {p1_after['char_name']} ур.{p1_after['level']}, "
          f"HP {p1_after['hp']}/{p1_after['max_hp']}, XP {p1_after['xp']}")

    # --- [17] виджеты и /party ----------------------------------------------------
    players = await sb.list_players(GROUP_ID)
    order = sb.parse_turn_order(campaign["turn_order_json"])
    overview = sb.party_overview_text(players, order, sb.as_int(campaign["current_turn_user_id"]))
    assert "Длинный меч" in overview and "Очередь хода" in overview, overview
    assert overview.count("🩹 Травмы: Отрублена левая нога") == len(players), overview
    widget = sb.format_status_widget(players, sb.as_int(campaign["current_turn_user_id"]))
    assert "Статус отряда" in widget and "Сейчас ход" in widget, widget
    SENT.clear()
    await sb.cmd_party(make_message(P1, "thorn", "/party"))
    assert "Отряд" in SENT[0], SENT[0]
    print(f"[17] /party и виджет статуса: {len(players)} героя(ев), очередь {order}")
    print("     " + overview.split("\n")[0])

    # --- [18] /skip ---------------------------------------------------------------
    SENT.clear()
    await sb.cmd_skip(make_message(P1, "thorn", "/skip"))
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.as_int(campaign["current_turn_user_id"]) == P2, campaign
    print(f"[18] /skip: активный ход → П2; ответ: {SENT[0][:60]}…")

    # --- [19] /join с аргументами всё равно берёт героя из лички -------------------
    SENT.clear()
    await sb.cmd_join(
        make_message(P3, "grom", "/join Торн, Дварф, Воин, Меч"),
        CommandObject(prefix="/", command="join", args="Торн, Дварф, Воин, Меч"),
    )
    assert "вступает в отряд" in SENT[0], SENT[0]
    assert "Аргументы больше не нужны" in SENT[0], SENT[0]
    grom = await sb.get_player(GROUP_ID, P3)
    assert grom["char_name"] == "Гром" and grom["weapon_dmg"] == 3, dict(grom)
    card = sb.format_character_card(grom)
    assert "Парные кинжалы" in card and "Кожаный доспех" in card and "без щита" in card, card
    campaign = await sb.get_campaign(GROUP_ID)
    assert sb.parse_turn_order(campaign["turn_order_json"]) == [P1, P2, P3], campaign["turn_order_json"]
    print(f"[19] /join с аргументами: герой всё равно из лички ({grom['char_name']}, "
          f"урон {grom['weapon_dmg']}, броня {grom['armor_def']}), "
          f"очередь {sb.parse_turn_order(campaign['turn_order_json'])}")

    # --- [20] сжатие истории -------------------------------------------------------
    sb.turns_since_summary[GROUP_ID] = sb.SUMMARY_EVERY_TURNS - 1
    await sb.maybe_summarize(GROUP_ID)
    campaign = await sb.get_campaign(GROUP_ID)
    assert campaign["story_summary"], campaign
    print(f"[20] story_summary: {campaign['story_summary']!r}")

    # --- [21] терпимый парсинг STATE_UPDATE и левел-ап из строки БД ---------------
    text, payload = sb.extract_state_update(
        'Мастер пишет текст.\n<!--STATE_UPDATE: {"changes": [{"user_id": 501, "xp_diff": 900}]}-->'
    )
    assert text == "Мастер пишет текст." and payload["changes"][0]["xp_diff"] == 900
    tolerant, tolerant_payload = sb.extract_state_update(
        'Ход. <!--STATE_UPDATE: {"changes": [{"user_id": 501, "xp_diff": 900}]} — начислено -->'
    )
    assert tolerant == "Ход." and tolerant_payload["changes"][0]["xp_diff"] == 900
    broken, broken_payload = sb.extract_state_update("Ход. <!--STATE_UPDATE: не json совсем-->")
    assert broken == "Ход." and broken_payload == {}
    row = dict(await sb.get_player(GROUP_ID, P3))       # Гром ещё не получал опыт
    assert row["level"] == 1 and row["xp"] == 0 and row["next_level_xp"] == 200, row
    ups = sb.grant_xp(row, 900)
    assert row["level"] == 3 and row["xp"] == 400 and row["next_level_xp"] == 500, row
    assert row["hp"] == row["max_hp"], row
    assert len(ups) == 2 and "достигает" in ups[-1], ups
    print(f"[21] Терпимый парсинг тега + левел-ап: {len(ups)} сообщений; ур. {row['level']}, "
          f"HP {row['hp']}/{row['max_hp']}, XP {row['xp']}, след. уровень {row['next_level_xp']}")

    # --- [22] /reset_story: кампания чата чистится, герои в личке остаются --------
    SENT.clear()
    await sb.cmd_reset_story(make_message(P1, "thorn", "/reset_story"))
    assert await sb.list_players(GROUP_ID) == []
    campaign = await sb.get_campaign(GROUP_ID)
    assert not sb.parse_turn_order(campaign["turn_order_json"]), campaign
    assert await sb.get_profile_player(P1) is not None, "герой в личке должен пережить сброс"
    print("[22] /reset_story: отряд чата пуст, герой в личке бота на месте")

    # --- [23] травмы и эффекты: схема, миграция старой БД, хелперы, инъекция --------
    columns = {row["name"] for row in await sb.db_fetchall("PRAGMA table_info(players)")}
    assert {"injuries", "active_buffs"} <= columns, sorted(columns)
    assert {"injuries", "active_buffs"} <= set(sb.PLAYER_EDITABLE_FIELDS)
    assert sb.dump_list_field(["а", "б"]) == '["а", "б"]', sb.dump_list_field(["а", "б"])
    assert sb.list_field('["Отрублена левая нога", "Слепота"]') == ["Отрублена левая нога", "Слепота"]
    assert sb.list_field("Слепота") == ["Слепота"]
    assert sb.list_field("") == [] and sb.list_field(None) == [] and sb.list_field("[]") == []
    assert sb.as_text_list("Слепота; Отрублена рука") == ["Слепота", "Отрублена рука"]
    assert sb.as_text_list(["а", "а", "б"]) == ["а", "б"]
    assert sb.add_list_values("[]", ["а", "б", "а"]) == '["а", "б"]'
    assert sb.remove_list_values('["Отрублена левая нога", "Слепота"]', "слепота") == '["Отрублена левая нога"]'
    assert sb.remove_list_values('["Глубокий ожог лица"]', "ожог лица") == "[]"
    assert sb.inventory_items("Меч; Щит") == ["Меч", "Щит"]
    assert sb.inventory_text(["Меч", "Щит"]) == "Меч; Щит"
    profile = await sb.get_profile_player(P1)
    assert profile["injuries"] == "[]" and profile["active_buffs"] == "[]", dict(profile)
    card = sb.build_active_card(profile)
    assert "[ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ]:" in card, card
    assert "- Тяжёлые травмы: Нет" in card and "- Действующие эффекты: Нет" in card, card
    assert "ПРАВИЛО МАСТЕРА: Любые действия героя строго ограничены его травмами!" in card, card
    # миграция старой БД без колонок: ALTER TABLE добавляет их вместе со значением '[]'
    legacy_path = BASE / "_smoke_legacy.db"
    legacy_path.unlink(missing_ok=True)
    legacy = await aiosqlite.connect(str(legacy_path))
    try:
        legacy.row_factory = aiosqlite.Row
        await legacy.execute(
            "CREATE TABLE players (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, char_name TEXT)"
        )
        await legacy.execute(
            "INSERT INTO players (chat_id, user_id, char_name) VALUES (1, 1, 'Старый герой')"
        )
        await legacy.commit()
        added = await sb.migrate_player_columns(legacy)
        await legacy.commit()
        assert added == ["injuries", "active_buffs"], added
        cursor = await legacy.execute("SELECT char_name, injuries, active_buffs FROM players")
        migrated = await cursor.fetchone()
        assert tuple(migrated) == ("Старый герой", "[]", "[]"), tuple(migrated)
        assert await sb.migrate_player_columns(legacy) == [], "повторная миграция ничего не добавляет"
    finally:
        await legacy.close()
    legacy_path.unlink(missing_ok=True)
    print("[23] Травмы/эффекты: колонки в схеме, миграция старой БД, хелперы и инъекция в карточку")

    # --- [24] STATE_UPDATE: add/remove травм, предметов и эффектов ------------------
    profile_p5 = await sb.get_profile_player(P5)
    inq, _ = await sb.join_chat_with_profile(profile_p5, GROUP_ID, "inq")
    by_id = {int(row["user_id"]): row for row in await sb.list_players(GROUP_ID)}
    outcome = await sb.apply_state_changes(
        GROUP_ID,
        {
            "changes": [
                {
                    "user_id": P5,
                    "hp_diff": -12,
                    "add_injuries": ["Отрублена левая нога", "Глубокий ожог лица"],
                    "add_items": ["Кинжал гнолла", "Чёрный камень"],
                    "add_buffs": ["Яд болотной гадюки (3 хода)"],
                }
            ]
        },
        by_id,
    )
    fresh = await sb.get_player(GROUP_ID, P5)
    assert sb.player_injuries(fresh) == ["Отрублена левая нога", "Глубокий ожог лица"], dict(fresh)
    assert sb.player_effects(fresh) == ["Яд болотной гадюки (3 хода)"], dict(fresh)
    assert "Кинжал гнолла" in sb.inventory_items(fresh["inventory"]), dict(fresh)
    assert int(fresh["hp"]) == int(inq["hp"]) - 12, (fresh["hp"], inq["hp"])
    assert len(outcome.injuries) == 2 and "получена травма" in outcome.injuries[0], outcome.injuries
    report = sb.format_damage_report(outcome)
    assert "🩹 <b>Травмы:</b>" in report and "Отрублена левая нога" in report, report
    assert "🩹 Травмы: Отрублена левая нога, Глубокий ожог лица" in sb.format_character_card(fresh)
    # лечение: травма (по вхождению), эффект и предмет снимаются; легаси-ключ работает
    healed = await sb.apply_state_changes(
        GROUP_ID,
        {
            "changes": [
                {
                    "user_id": P5,
                    "remove_injuries": ["ожог лица"],
                    "remove_buffs": ["Яд болотной гадюки (3 хода)"],
                    "inventory_remove": "Кинжал гнолла",
                }
            ]
        },
        {int(row["user_id"]): row for row in await sb.list_players(GROUP_ID)},
    )
    fresh = await sb.get_player(GROUP_ID, P5)
    assert sb.player_injuries(fresh) == ["Отрублена левая нога"], dict(fresh)
    assert sb.player_effects(fresh) == [], dict(fresh)
    assert "Кинжал гнолла" not in sb.inventory_items(fresh["inventory"]), dict(fresh)
    assert healed.injuries == [] and healed.damaged == [], healed
    print(f"[24] STATE_UPDATE: травмы {sb.player_injuries(fresh)}, "
          f"эффекты {sb.player_effects(fresh)}, предметы {sb.inventory_items(fresh['inventory'])}")

    # --- [25] MASTER_SYSTEM_PROMPT: свобода выбора, города, 18+, протокол ----------
    prompt = sb.MASTER_SYSTEM_PROMPT
    for fragment in (
        "ПОЛНАЯ СВОБОДА (NO RAILS)",
        "НИКОГДА не тащишь",
        "грабить трактиры",
        "Оплот Чистоты",
        "Город Ложной Утопии",
        "We Happy Few",
        "«Сладость»",
        "МАТУРНОСТЬ И ВЫРАЗИТЕЛЬНОСТЬ (18+)",
        "гримдарк",
        "ЖЁСТКИЙ УЧЁТ ТРАВМ",
        "[ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ]",
        '"add_injuries"',
        '"remove_injuries"',
        '"add_items"',
        '"remove_items"',
    ):
        assert fragment in prompt, fragment
    assert "[АКТИВНЫЙ ИГРОК В ЭТОТ ХОД]" in prompt and "{user_id}" in prompt
    print("[25] MASTER_SYSTEM_PROMPT: свобода выбора, два города, 18+ и новый протокол STATE_UPDATE")

    # --- [26] параметры запроса к DeepSeek: 0.65 / 900 токенов, сводка 0.4 / 700 ---
    assert sb.DEEPSEEK_TEMPERATURE == 0.65, sb.DEEPSEEK_TEMPERATURE
    client = sb.DeepSeekClient("smoke-key")
    original_http = client._client
    http = FakeHttp()
    client._client = http
    try:
        answer = await client.chat([{"role": "system", "content": "стиль"}])
        assert answer == "Ответ мастера.", answer
        url, body = http.bodies[0]
        assert url == "/chat/completions", url
        assert body["model"] == sb.DEEPSEEK_MODEL == "deepseek-chat", body["model"]
        assert body["temperature"] == 0.65 and body["max_tokens"] == 900, body
        assert body["stream"] is False, body
        await client.summarize("было", ["ход П1: рублю гнолла"])
        summary_body = http.bodies[1][1]
        assert summary_body["temperature"] == 0.4 and summary_body["max_tokens"] == 700, summary_body
    finally:
        client._client = original_http
        await client.aclose()
    print("[26] DeepSeek: мастер temperature 0.65 / 900 токенов, сводка 0.4 / 700 токенов")

    await sb.close_db()
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ✅")


if __name__ == "__main__":
    asyncio.run(main())



