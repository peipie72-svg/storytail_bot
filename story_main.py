"""story_main.py — мультиплеерный сторителлинг-бот для Telegram.

Стек: aiogram 3 + aiosqlite + DeepSeek API (chat/completions через httpx).

Ключевые принципы:
  * НИКАКИХ КУБИКОВ И РАНДОМА. Урон/броня/способности фиксированы, попадание
    при атаке гарантировано.
  * Жёсткий учёт листа персонажа: карточка героя читается из SQLite при каждом
    сообщении и инъецируется в системный контекст DeepSeek.
  * Строгая очередь ходов: ход вне очереди перехватывается кодом без вызова API.
  * Прогрессия опыта по фиксированной шкале 200/300/500/800/1300/2100/3400/5500/10000.

 * Регистрация героя — пошаговый мастер В ЛИЧНЫХ СООБЩЕНИЯХ бота (/create или
   /start): имя → раса (кнопки) → класс (кнопки) → выдача стартового комплекта
   класса из STARTING_KITS. В игровом чате команда /join БЕЗ аргументов просто
   переносит готового героя из лички в очередь хода (turn_order_json).

ВАЖНО (настройка бота): чтобы бот видел сообщения игроков и мог перехватывать
ходы вне очереди, в @BotFather должен быть ВЫКЛЮЧЕН Privacy Mode
(/mybots -> Bot Settings -> Group Privacy -> Turn off). Иначе бот получает
только команды, реплаи и упоминания.

Запуск: python story_main.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import aiosqlite
import httpx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    User,
)

# ==============================================================================
# 1. КОНФИГУРАЦИЯ
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent

# Ключи берём из .env рядом со скриптом, затем — стандартный поиск dotenv.
load_dotenv(BASE_DIR / ".env")
load_dotenv()

BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip()
DEEPSEEK_API_KEY: str = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
DEEPSEEK_BASE_URL: str = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip().rstrip("/")
DEEPSEEK_MODEL: str = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
DEEPSEEK_TIMEOUT: float = float(os.getenv("DEEPSEEK_TIMEOUT", "120"))
DEEPSEEK_TEMPERATURE: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.65"))
DEEPSEEK_MAX_TOKENS: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "900"))

# Необязательный прокси только для Telegram (например, socks5://127.0.0.1:1080 или http://…).
TELEGRAM_PROXY: str = (os.getenv("TELEGRAM_PROXY") or "").strip()

DB_PATH: Path = Path(os.getenv("STORY_DB_PATH") or (BASE_DIR / "story_game.db"))
LOG_LEVEL: str = (os.getenv("LOG_LEVEL") or "INFO").upper()

# Необязательные глобальные админы бота: ADMIN_IDS=123456,789012
GLOBAL_ADMIN_IDS: frozenset[int] = frozenset(
    int(part) for part in re.findall(r"\d+", os.getenv("ADMIN_IDS", "") or "")
)

# Шкала опыта: сколько НОВОГО опыта требуется для перехода на следующий уровень.
DEFAULT_XP_THRESHOLDS: dict[int, int] = {
    1: 200,
    2: 300,
    3: 500,
    4: 800,
    5: 1300,
    6: 2100,
    7: 3400,
    8: 5500,
    9: 10000,
}
MAX_LEVEL: int = 10

# Эталонный размер справочников: в race.txt ровно 9 рас, в class.txt ровно 6 классов.
EXPECTED_RACE_COUNT: int = 9
EXPECTED_CLASS_COUNT: int = 6

TURN_TAG_TEMPLATE = "⏳ Ход переходит к: {mention} (твой черёд заявить действие!)"
REFUSAL_TEMPLATES: tuple[str, ...] = (
    "Попридержи коней, {mention}! Твой ход уже был. Дай другим показать себя, сейчас очередь {current}.",
    "Не лезь под руку, {mention}, отдышись! Сейчас действует {current}.",
    "Героизм вне очереди не принимается, {mention}. Жди своего черёда, сейчас ход {current}.",
)
REFUSAL_COOLDOWN_SECONDS = 20.0  # защита от спама ироничными отказами
ACTION_HISTORY_LIMIT = 16        # сколько сообщений истории держим в памяти на чат
SUMMARY_EVERY_TURNS = 12         # раз в N ходов сжимаем историю в story_summary
TELEGRAM_MESSAGE_LIMIT = 3600    # безопасный лимит длины одного сообщения

DEFAULT_INVENTORY = "Походный набор: фляга, верёвка 10 м, сухпай, факел"

# ==============================================================================
# 2. ЛОГИРОВАНИЕ И ОШИБКИ
# ==============================================================================


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


log = logging.getLogger("story_bot")


class StoryBotError(Exception):
    """Базовая ошибка бота."""


class CatalogError(StoryBotError):
    """Проблема со справочниками (weapon/armor/race/class .txt)."""


class DeepSeekError(StoryBotError):
    """Проблема при обращении к DeepSeek API."""


class RegistrationError(StoryBotError):
    """Некорректные данные при регистрации персонажа."""


# ==============================================================================
# 3. МЕЛКИЕ ХЕЛПЕРЫ
# ==============================================================================


def esc(value: Any) -> str:
    """Экранирование любого динамического текста для parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""), quote=False)


def normalize(value: str) -> str:
    """Нормализация названий для нечёткого сверения со справочниками."""
    text = (value or "").lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def mention_for(user_id: int, username: Optional[str] = None, char_name: Optional[str] = None) -> str:
    """@username, если он есть, иначе безопасная ссылка-упоминание по user_id."""
    if username:
        return "@" + esc(username)
    label = char_name or f"игрок {user_id}"
    return f'<a href="tg://user?id={int(user_id)}">{esc(label)}</a>'


def raw_mention(user_id: int, username: Optional[str] = None, char_name: Optional[str] = None) -> str:
    """Тот же игрок, но без HTML — для использования внутри промпта DeepSeek."""
    if username:
        return "@" + username
    return char_name or f"игрок {user_id}"


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст по строкам так, чтобы каждая часть влезла в лимит."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        piece = paragraph
        while len(piece) > limit:  # аварийный случай: одна гигантская строка
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece[:limit])
            piece = piece[limit:]
        candidate = piece if not current else f"{current}\n{piece}"
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()] or [text[:limit]]


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Аккуратно приводит значение из JSON к int."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


# ==============================================================================
# 4. СПРАВОЧНИКИ: ПАРСИНГ weapon.txt / armor.txt / race.txt / class.txt
# ==============================================================================


@dataclass(frozen=True)
class Weapon:
    name: str
    dmg: int
    versatile_dmg: Optional[int]
    damage_type: str
    props: tuple[str, ...]
    aliases: tuple[str, ...]

    @property
    def two_handed(self) -> bool:
        return any("двуручн" in prop.lower() for prop in self.props)

    @property
    def ranged(self) -> bool:
        return any("дистанция" in prop.lower() for prop in self.props)


@dataclass(frozen=True)
class Armor:
    name: str
    defense: int
    note: str
    is_shield: bool
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Race:
    name: str
    description: str
    traits: tuple[str, ...]
    aliases: tuple[str, ...]

    @property
    def trait_text(self) -> str:
        if self.traits:
            return " ".join(self.traits)
        return self.description


@dataclass(frozen=True)
class LevelPerk:
    level: int
    title: str
    text: str
    hp_bonus: int

    def as_line(self) -> str:
        return f"Ур. {self.level} — «{self.title}»: {self.text}"


@dataclass(frozen=True)
class CharClass:
    name: str
    role: str
    base_hp: int
    damage_hint: str
    armor_hint: str
    perks: dict[int, LevelPerk]
    aliases: tuple[str, ...]

    def perk(self, level: int) -> Optional[LevelPerk]:
        return self.perks.get(level)


@dataclass
class Catalog:
    """Разобранные справочники. Ключи словарей — нормализованные названия/алиасы."""

    weapons: dict[str, Weapon]
    armors: dict[str, Armor]
    races: dict[str, Race]
    classes: dict[str, CharClass]
    shields: dict[str, Armor]
    xp_thresholds: dict[int, int]

    def find_weapon(self, query: str) -> Optional[Weapon]:
        return _resolve(self.weapons, query)

    def find_armor(self, query: str) -> Optional[Armor]:
        return _resolve(self.armors, query)

    def find_race(self, query: str) -> Optional[Race]:
        return _resolve(self.races, query)

    def find_class(self, query: str) -> Optional[CharClass]:
        return _resolve(self.classes, query)

    # Словари выше ключуются алиасами, поэтому уникальные записи берём отсюда:
    # len(catalog().all_races) — число рас, len(catalog().all_classes) — число классов.
    @property
    def all_weapons(self) -> tuple[Weapon, ...]:
        return _unique(self.weapons)

    @property
    def all_armors(self) -> tuple[Armor, ...]:
        return _unique(self.armors)

    @property
    def all_shields(self) -> tuple[Armor, ...]:
        return _unique(self.shields)

    @property
    def all_races(self) -> tuple[Race, ...]:
        return _unique(self.races)

    @property
    def all_classes(self) -> tuple[CharClass, ...]:
        return _unique(self.classes)


def _unique(catalog_map: dict[str, Any]) -> tuple[Any, ...]:
    """Уникальные значения справочника в порядке появления (без дублей по алиасам)."""
    seen: dict[int, Any] = {}
    for value in catalog_map.values():
        seen.setdefault(id(value), value)
    return tuple(seen.values())


def _alias_variants(name: str) -> set[str]:
    """Варианты названия: полное, части через '/', содержимое скобок, без скобок."""
    variants: set[str] = set()
    cleaned = re.sub(r"\s+", " ", (name or "").strip())

    def add(value: str) -> None:
        key = normalize(value)
        if key:
            variants.add(key)

    add(cleaned)
    add(cleaned.split(" (")[0])
    for part in cleaned.split("/"):
        add(part)
        add(re.sub(r"\(.*?\)", "", part))

    for inner in re.findall(r"\((.*?)\)", cleaned):
        add(inner)
        add(inner.replace("/", " "))
    return variants


def _resolve(catalog: dict[str, Any], query: str) -> Optional[Any]:
    """Нечёткий, но детерминированный поиск: точное совпадение -> вхождение подстроки."""
    key = normalize(query)
    if not key:
        return None
    if key in catalog:
        return catalog[key]

    contained = [candidate for candidate in catalog if key and (key in candidate or candidate in key)]
    if not contained:
        return None
    # предпочитаем самое длинное совпадение — оно наиболее специфично
    contained.sort(key=len, reverse=True)
    return catalog[contained[0]]


def _iter_data_lines(block: str) -> list[str]:
    out: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        out.append(stripped)
    return out


def parse_weapons(text: str) -> dict[str, Weapon]:
    weapons: dict[str, Weapon] = {}
    for line in _iter_data_lines(text):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        match = re.search(r"Урон:\s*(\d+)(?:\s*\(двуручный хват:\s*(\d+)\))?", parts[1], re.IGNORECASE)
        if not match:
            continue
        name = parts[0]
        weapon = Weapon(
            name=name,
            dmg=int(match.group(1)),
            versatile_dmg=int(match.group(2)) if match.group(2) else None,
            damage_type=parts[2] if len(parts) > 2 else "",
            props=tuple(part.strip() for part in parts[3].split(",")) if len(parts) > 3 else (),
            aliases=tuple(sorted(_alias_variants(name))),
        )
        for alias in weapon.aliases:
            weapons.setdefault(alias, weapon)
    return weapons


def parse_armors(text: str) -> tuple[dict[str, Armor], dict[str, Armor]]:
    armors: dict[str, Armor] = {}
    shields: dict[str, Armor] = {}
    in_shield_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_shield_section = "щит" in stripped.lower()
            continue
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in stripped.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        match = re.search(r"Защита:\s*(\+?\d+)", parts[1], re.IGNORECASE)
        if not match:
            continue
        name = parts[0]
        is_shield = in_shield_section or "щит" in name.lower()
        armor = Armor(
            name=name,
            defense=int(match.group(1)),
            note=" | ".join(parts[2:]),
            is_shield=is_shield,
            aliases=tuple(sorted(_alias_variants(name))),
        )
        target = shields if is_shield else armors
        for alias in armor.aliases:
            target.setdefault(alias, armor)
    return armors, shields


def _title_case(name: str) -> str:
    parts = [part for part in re.split(r"\s+", name.strip()) if part]
    return " ".join(part.lower().capitalize() for part in parts)


# Заголовок блока расы: «[1. ЧЕЛОВЕК (HUMAN)]» — номер внутри скобок обязателен.
RACE_HEADER_RE = re.compile(r"\[\s*\d+\.\s*(?P<title>[^\[\]]+?)\s*\]")
# Заголовок блока класса: «[КЛАСС 1: ВОИН (WARRIOR)]».
CLASS_HEADER_RE = re.compile(r"\[\s*КЛАСС\s*\d+\s*:\s*(?P<title>[^\[\]]+?)\s*\]")


def parse_races(text: str) -> dict[str, Race]:
    """Разбирает race.txt строго по блокам «[номер. НАЗВАНИЕ (АНГЛ)]».

    Новую расу создаёт ТОЛЬКО строка-заголовок с номером внутри скобок. Любые
    другие строки (комментарии, «Описание:», черты «• …», пустые строки) относятся
    к текущей расе. В словаре ключами идут алиасы, поэтому число рас считай как
    ``len(catalog().all_races)``.
    """
    races: dict[str, Race] = {}
    current: Optional[dict[str, Any]] = None
    matched = 0
    seen_names: set[str] = set()

    def flush() -> None:
        if not current:
            return
        key = normalize(current["name"])
        if key in seen_names:
            log.warning("race.txt: раса «%s» встречается повторно — оставляю первую", current["name"])
            return
        seen_names.add(key)
        race = Race(
            name=current["name"],
            description=current["description"],
            traits=tuple(current["traits"]),
            aliases=tuple(sorted(current["aliases"])),
        )
        for alias in race.aliases:
            races.setdefault(alias, race)

    for line in text.splitlines():
        stripped = line.strip()
        header = RACE_HEADER_RE.fullmatch(stripped)
        if header:
            flush()
            matched += 1
            title = header.group("title").strip()
            english = ""
            found = re.search(r"\((.*?)\)", title)
            if found:
                english = found.group(1).strip()
            russian = re.sub(r"\(.*?\)", "", title).strip()
            ru_names = [part.strip() for part in russian.split("/") if part.strip()]
            pretty = " / ".join(_title_case(name) for name in ru_names)
            aliases: set[str] = set()
            for name in ru_names:
                aliases |= _alias_variants(name)
                aliases.add(normalize(_title_case(name)))
            aliases |= _alias_variants(pretty)
            for name in english.split("/"):
                aliases.add(normalize(name))
            current = {"name": pretty, "description": "", "traits": [], "aliases": aliases}
            continue

        if current is None:
            continue
        if stripped.lower().startswith("описание:"):
            current["description"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("•") or stripped.startswith("-"):
            current["traits"].append(stripped.lstrip("•-").strip())

    flush()
    if not matched:
        raise CatalogError("В race.txt не найден ни один блок вида «[1. НАЗВАНИЕ (АНГЛ)]».")
    unique = len({id(race) for race in races.values()})
    if unique != matched:
        log.warning(
            "race.txt: заголовков %d, уникальных рас %d — проверь дубли названий.", matched, unique
        )
    return races


def parse_classes(text: str) -> dict[str, CharClass]:
    """Разбирает class.txt строго по блокам «[КЛАСС N: НАЗВАНИЕ (АНГЛ)]».

    Всё, что находится выше первого блока класса (шкала прогрессии XP, заголовки
    и комментарии), игнорируется — прогрессия читается отдельно в
    :func:`parse_xp_thresholds`. В словаре ключами идут алиасы, поэтому число
    классов считай как ``len(catalog().all_classes)``.
    """
    classes: dict[str, CharClass] = {}
    current: Optional[dict[str, Any]] = None
    matched = 0
    seen_names: set[str] = set()

    def flush() -> None:
        if not current:
            return
        key = normalize(current["name"])
        if key in seen_names:
            log.warning("class.txt: класс «%s» встречается повторно — оставляю первый", current["name"])
            return
        seen_names.add(key)
        char_class = CharClass(
            name=current["name"],
            role=current["role"],
            base_hp=current["base_hp"],
            damage_hint=current["damage_hint"],
            armor_hint=current["armor_hint"],
            perks=current["perks"],
            aliases=tuple(sorted(current["aliases"])),
        )
        for alias in char_class.aliases:
            classes.setdefault(alias, char_class)

    # Строки до первого заголовка класса (шкала прогрессии XP, комментарии)
    # игнорируются: current ещё None, а «Роль:/База (Ур. 1):/Уровень N:» относятся
    # только к текущему блоку.
    for line in text.splitlines():
        stripped = line.strip()
        header = CLASS_HEADER_RE.fullmatch(stripped)
        if header:
            flush()
            matched += 1
            title = header.group("title").strip()
            english = ""
            found = re.search(r"\((.*?)\)", title)
            if found:
                english = found.group(1).strip()
            russian = re.sub(r"\(.*?\)", "", title).strip()
            pretty = _title_case(russian)
            aliases: set[str] = set(_alias_variants(russian))
            aliases.add(normalize(pretty))
            for name in english.split("/"):
                aliases.add(normalize(name))
            current = {
                "name": pretty,
                "role": "",
                "base_hp": 20,
                "damage_hint": "",
                "armor_hint": "",
                "perks": {},
                "aliases": aliases,
            }
            continue

        if current is None:
            continue
        if stripped.lower().startswith("роль:"):
            current["role"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.lower().startswith("база"):
            hp_match = re.search(r"(\d+)\s*HP", stripped, re.IGNORECASE)
            if hp_match:
                current["base_hp"] = int(hp_match.group(1))
            dmg_match = re.search(r"Урон:\s*([^|]+)", stripped)
            if dmg_match:
                current["damage_hint"] = dmg_match.group(1).strip()
            armor_match = re.search(r"Броня:\s*([^|]+)", stripped)
            if armor_match:
                current["armor_hint"] = armor_match.group(1).strip()
            continue

        perk_match = re.search(r"Уровень\s*(\d+)\s*:\s*«([^»]+)»\s*[—–-]?\s*(.*)$", stripped)
        if perk_match:
            level = int(perk_match.group(1))
            title = perk_match.group(2).strip()
            body = perk_match.group(3).strip()
            hp_match = re.search(r"\(\+\s*(\d+)\s*к Max HP\)", body)
            hp_bonus = int(hp_match.group(1)) if hp_match else 0
            body = re.sub(r"\s*\(\+\s*\d+\s*к Max HP\)\s*$", "", body).strip()
            current["perks"][level] = LevelPerk(level=level, title=title, text=body, hp_bonus=hp_bonus)

    flush()
    if not matched:
        raise CatalogError("В class.txt не найден ни один блок вида «[КЛАСС 1: НАЗВАНИЕ (АНГЛ)]».")
    unique = len({id(char_class) for char_class in classes.values()})
    if unique != matched:
        log.warning(
            "class.txt: заголовков %d, уникальных классов %d — проверь дубли названий.", matched, unique
        )
    return classes


def parse_xp_thresholds(text: str) -> dict[int, int]:
    thresholds = dict(DEFAULT_XP_THRESHOLDS)
    for match in re.finditer(r"^\s*(\d+)\s*:\s*(\d+)\s*,", text, re.MULTILINE):
        level, xp = int(match.group(1)), int(match.group(2))
        if 1 <= level < MAX_LEVEL:
            thresholds[level] = xp
    return thresholds


# Стартовые комплекты классов (оружие/броня/щит/HP) заданы в STARTING_KITS —
# см. раздел 6 «СОЗДАНИЕ ПЕРСОНАЖА».


def load_catalog(base_dir: Path = BASE_DIR) -> Catalog:
    """Читает и разбирает все четыре справочника из папки проекта."""
    global CATALOG
    files = {
        "weapon": base_dir / "weapon.txt",
        "armor": base_dir / "armor.txt",
        "race": base_dir / "race.txt",
        "class": base_dir / "class.txt",
    }
    texts: dict[str, str] = {}
    for key, path in files.items():
        if not path.exists():
            raise CatalogError(f"Не найден справочник {path.name} в {base_dir}")
        texts[key] = path.read_text(encoding="utf-8")

    armors, shields = parse_armors(texts["armor"])

    built = Catalog(
        weapons=parse_weapons(texts["weapon"]),
        armors=armors,
        races=parse_races(texts["race"]),
        classes=parse_classes(texts["class"]),
        shields=shields,
        xp_thresholds=parse_xp_thresholds(texts["class"]),
    )
    log.info(
        "Справочники загружены: оружие=%d, броня=%d, щиты=%d, расы=%d, классы=%d, шкала XP=%s",
        len(built.all_weapons),
        len(built.all_armors),
        len(built.all_shields),
        len(built.all_races),
        len(built.all_classes),
        {level: built.xp_thresholds[level] for level in sorted(built.xp_thresholds)},
    )
    if len(built.all_races) != EXPECTED_RACE_COUNT:
        log.warning(
            "race.txt: ожидалось %d рас, разобрано %d — проверь заголовки «[N. НАЗВАНИЕ (АНГЛ)]».",
            EXPECTED_RACE_COUNT,
            len(built.all_races),
        )
    if len(built.all_classes) != EXPECTED_CLASS_COUNT:
        log.warning(
            "class.txt: ожидалось %d классов, разобрано %d — проверь заголовки «[КЛАСС N: НАЗВАНИЕ (АНГЛ)]».",
            EXPECTED_CLASS_COUNT,
            len(built.all_classes),
        )
    # Стартовые комплекты классов должны совпадать с class.txt (иначе — предупреждение).
    validate_starting_kits(built)
    CATALOG = built
    return built


CATALOG: Optional[Catalog] = None


def catalog() -> Catalog:
    if CATALOG is None:
        raise CatalogError("Справочники ещё не загружены (см. load_catalog)")
    return CATALOG


# ==============================================================================
# 5. БАЗА ДАННЫХ (aiosqlite)
# ==============================================================================

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS players (
        chat_id          INTEGER NOT NULL,
        user_id          INTEGER NOT NULL,
        username         TEXT,
        char_name        TEXT,
        race             TEXT,
        racial_trait     TEXT,
        char_class       TEXT,
        level            INTEGER DEFAULT 1,
        hp               INTEGER,
        max_hp           INTEGER,
        weapon_name      TEXT,
        weapon_dmg       INTEGER,
        armor_name       TEXT,
        armor_def        INTEGER,
        shield_def       INTEGER DEFAULT 0,
        inventory        TEXT,
        xp               INTEGER DEFAULT 0,
        next_level_xp    INTEGER DEFAULT 200,
        active_abilities TEXT,
        injuries         TEXT DEFAULT '[]',
        active_buffs     TEXT DEFAULT '[]',
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS campaigns (
        chat_id              INTEGER PRIMARY KEY,
        turn_order_json      TEXT DEFAULT '[]',
        current_turn_user_id INTEGER DEFAULT NULL,
        story_summary        TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_players_chat ON players (chat_id)",
)

PLAYER_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "username", "char_name", "race", "racial_trait", "char_class", "level",
        "hp", "max_hp", "weapon_name", "weapon_dmg", "armor_name", "armor_def",
        "shield_def", "inventory", "xp", "next_level_xp", "active_abilities",
        "injuries", "active_buffs",
    }
)

# Колонки, появившиеся в новых версиях: добиваем схему уже существующей БД.
PLAYER_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("injuries", "TEXT DEFAULT '[]'"),
    ("active_buffs", "TEXT DEFAULT '[]'"),
)

EMPTY_LIST_JSON: str = "[]"

# Значения по умолчанию для полей, которых может не быть в переданном листе.
PLAYER_FIELD_DEFAULTS: dict[str, Any] = {
    "injuries": EMPTY_LIST_JSON,
    "active_buffs": EMPTY_LIST_JSON,
}

_db_connection: Optional[aiosqlite.Connection] = None
_db_write_lock = asyncio.Lock()
_chat_action_locks: dict[int, asyncio.Lock] = {}


def as_text_list(raw: Any) -> list[str]:
    """Приводит значение (строку, список) к списку непустых строк без дублей."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values: list[Any] = list(raw)
    elif isinstance(raw, str):
        values = [part for part in re.split(r"[;\n]+", raw) if part.strip()] or [raw]
    else:
        values = [raw]
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip().strip('"').strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def list_field(raw: Any) -> list[str]:
    """Читает список из TEXT-колонки: JSON-массив, «а; б» или одиночное значение."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        source: list[Any] = list(raw)
    else:
        text = str(raw).strip()
        if not text or text == EMPTY_LIST_JSON:
            return []
        source = [text]
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                source = list(parsed)
    items: list[str] = []
    for value in source:
        cleaned = str(value).strip().strip('"').strip()
        if cleaned and cleaned not in items:
            items.append(cleaned)
    return items


def dump_list_field(items: Sequence[str]) -> str:
    """Сериализует список обратно в TEXT-колонку (JSON-массив)."""
    return json.dumps(list(items), ensure_ascii=False)


def add_list_values(raw: Any, additions: Any) -> str:
    """Дописывает значения в TEXT-колонку списка (без дублей)."""
    items = list_field(raw)
    for value in as_text_list(additions):
        if value not in items:
            items.append(value)
    return dump_list_field(items)


def remove_list_values(raw: Any, removals: Any) -> str:
    """Убирает значения из TEXT-колонки списка: точное совпадение, иначе вхождение."""
    items = list_field(raw)
    for value in as_text_list(removals):
        needle = value.lower()
        if any(item.lower() == needle for item in items):
            items = [item for item in items if item.lower() != needle]
        else:
            items = [item for item in items if needle not in item.lower()]
    return dump_list_field(items)


def inventory_items(raw: Any) -> list[str]:
    """Предметы инвентаря из TEXT-колонки: «а; б» или JSON-массив."""
    if isinstance(raw, (list, tuple, set)):
        source: list[Any] = list(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        source = re.split(r"[;\n]+", text)
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                source = list(parsed)
    items: list[str] = []
    for value in source:
        cleaned = str(value).strip().strip('"').strip()
        if cleaned and cleaned not in items:
            items.append(cleaned)
    return items


def inventory_text(items: Sequence[str]) -> str:
    """Собирает инвентарь обратно в строку «а; б» — формат листа персонажа."""
    return "; ".join(items)


def player_injuries(player: dict[str, Any]) -> list[str]:
    """Тяжёлые травмы героя из листа персонажа."""
    return list_field(player.get("injuries"))


def player_effects(player: dict[str, Any]) -> list[str]:
    """Действующие эффекты, магия и яды из листа персонажа."""
    return list_field(player.get("active_buffs"))


def list_or_none(items: Sequence[str]) -> str:
    """«Нет» для пустого списка — чтобы мастер не читал пустую строку."""
    return ", ".join(items) if items else "Нет"


async def init_db(path: Path = DB_PATH) -> None:
    """Открывает соединение, включает WAL и создаёт схему."""
    global _db_connection
    connection = await aiosqlite.connect(str(path))
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA synchronous=NORMAL")
    await connection.execute("PRAGMA busy_timeout=5000")
    for statement in SCHEMA_STATEMENTS:
        await connection.execute(statement)
    await migrate_player_columns(connection)
    await connection.commit()
    _db_connection = connection
    log.info("База данных готова: %s", path)


async def migrate_player_columns(connection: aiosqlite.Connection) -> list[str]:
    """Догоняет схему старой БД: ALTER TABLE для колонок из PLAYER_COLUMN_MIGRATIONS."""
    cursor = await connection.execute("PRAGMA table_info(players)")
    rows = await cursor.fetchall()
    await cursor.close()
    existing = {str(row["name"]) for row in rows}
    added: list[str] = []
    for column, ddl in PLAYER_COLUMN_MIGRATIONS:
        if column in existing:
            continue
        await connection.execute(f"ALTER TABLE players ADD COLUMN {column} {ddl}")
        added.append(column)
        log.info("Схема players дополнена колонкой %s", column)
    return added


async def close_db() -> None:
    global _db_connection
    if _db_connection is not None:
        await _db_connection.close()
        _db_connection = None
        log.info("Соединение с базой данных закрыто")


def _db() -> aiosqlite.Connection:
    if _db_connection is None:
        raise StoryBotError("База данных не инициализирована — вызовите init_db()")
    return _db_connection


def chat_action_lock(chat_id: int) -> asyncio.Lock:
    """Лок на чат: два действия игроков не обрабатываются одновременно."""
    lock = _chat_action_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_action_locks[chat_id] = lock
    return lock


async def db_execute(sql: str, params: Sequence[Any] = ()) -> None:
    async with _db_write_lock:
        connection = _db()
        await connection.execute(sql, tuple(params))
        await connection.commit()


async def db_fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
    connection = _db()
    async with connection.execute(sql, tuple(params)) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def db_fetchall(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    connection = _db()
    async with connection.execute(sql, tuple(params)) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


def parse_turn_order(raw: Any) -> list[int]:
    """Аккуратно разбирает turn_order_json в список уникальных user_id."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            log.warning("Повреждён turn_order_json, очередь пересобирается: %r", raw)
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    order: list[int] = []
    for item in raw:
        value = as_int(item)
        if value is not None and value not in order:
            order.append(value)
    return order


async def get_campaign(chat_id: int, create: bool = True) -> Optional[dict[str, Any]]:
    campaign = await db_fetchone("SELECT * FROM campaigns WHERE chat_id = ?", (chat_id,))
    if campaign is None and create:
        await db_execute("INSERT OR IGNORE INTO campaigns (chat_id) VALUES (?)", (chat_id,))
        campaign = await db_fetchone("SELECT * FROM campaigns WHERE chat_id = ?", (chat_id,))
    return campaign


async def save_campaign(
    chat_id: int,
    *,
    turn_order: Optional[Sequence[int]] = None,
    current_turn_user_id: Any = "__keep__",
    story_summary: Optional[str] = None,
) -> None:
    """Обновляет кампанию. current_turn_user_id можно явно выставить в None."""
    await get_campaign(chat_id)
    updates: list[str] = []
    params: list[Any] = []
    if turn_order is not None:
        updates.append("turn_order_json = ?")
        params.append(json.dumps(list(turn_order), ensure_ascii=False))
    if not (isinstance(current_turn_user_id, str) and current_turn_user_id == "__keep__"):
        updates.append("current_turn_user_id = ?")
        params.append(current_turn_user_id)
    if story_summary is not None:
        updates.append("story_summary = ?")
        params.append(story_summary)
    if not updates:
        return
    params.append(chat_id)
    await db_execute(f"UPDATE campaigns SET {', '.join(updates)} WHERE chat_id = ?", params)


async def list_players(chat_id: int) -> list[dict[str, Any]]:
    return await db_fetchall("SELECT * FROM players WHERE chat_id = ? ORDER BY rowid ASC", (chat_id,))


async def get_player(chat_id: int, user_id: int) -> Optional[dict[str, Any]]:
    return await db_fetchone(
        "SELECT * FROM players WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
    )


async def insert_player(fields: dict[str, Any]) -> None:
    columns = ["chat_id", "user_id", *sorted(PLAYER_EDITABLE_FIELDS)]
    values = [fields["chat_id"], fields["user_id"]]
    for column in sorted(PLAYER_EDITABLE_FIELDS):
        value = fields.get(column)
        if value is None and column in PLAYER_FIELD_DEFAULTS:
            value = PLAYER_FIELD_DEFAULTS[column]
        values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    await db_execute(f"INSERT OR REPLACE INTO players ({', '.join(columns)}) VALUES ({placeholders})", values)


async def update_player(chat_id: int, user_id: int, **changes: Any) -> None:
    """Обновляет только разрешённые поля листа персонажа."""
    payload = {key: value for key, value in changes.items() if key in PLAYER_EDITABLE_FIELDS}
    if not payload:
        return
    assignments = ", ".join(f"{column} = ?" for column in payload)
    params = [*payload.values(), chat_id, user_id]
    await db_execute(f"UPDATE players SET {assignments} WHERE chat_id = ? AND user_id = ?", params)


async def add_player_to_turn_order(chat_id: int, user_id: int) -> list[int]:
    """Добавляет игрока в очередь хода; если очередь была пуста — делает его активным."""
    campaign = await get_campaign(chat_id)
    order = parse_turn_order(campaign["turn_order_json"] if campaign else [])
    if user_id not in order:
        order.append(user_id)
    current = as_int(campaign["current_turn_user_id"]) if campaign else None
    if current is None or current not in order:
        current = order[0] if order else None
    await save_campaign(chat_id, turn_order=order, current_turn_user_id=current)
    return order


async def reset_chat(chat_id: int) -> None:
    """Полный сброс кампании и отряда текущего чата."""
    await db_execute("DELETE FROM players WHERE chat_id = ?", (chat_id,))
    await db_execute("DELETE FROM campaigns WHERE chat_id = ?", (chat_id,))
    history.pop(chat_id, None)
    refusal_stamps.pop(chat_id, None)
    log.info("Кампания чата %s сброшена", chat_id)


# ==============================================================================
# 6. СОЗДАНИЕ ПЕРСОНАЖА: СТАРТОВЫЕ КОМПЛЕКТЫ КЛАССОВ И БАЛАНС
# ==============================================================================
# Всё детерминировано: ни одного random. Стартовое снаряжение жёстко зафиксиро-
# вано в STARTING_KITS, базовые HP и перки приходят из class.txt, расовые бонусы
# (+4 HP дварфу, +1 урона орку, +1 дальнобойному эльфу) — из race.txt.


@dataclass
class CharacterBuild:
    fields: dict[str, Any]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class StartingKit:
    """Стартовый комплект класса: оружие, броня, щит и HP выдаются автоматически."""

    char_class: str          # каноническое имя класса из class.txt
    weapon_name: str
    weapon_dmg: int          # фиксированный урон (двуручный хват его не меняет)
    damage_kind: str         # «физический» или «магический» (чары)
    weapon_ranged: bool      # дальнобойное оружие — для расового бонуса эльфа
    armor_name: str
    armor_def: int
    shield: bool
    hp: int

    def weapon(self) -> Weapon:
        """Оружие комплекта (weapon.txt не меняем — комплект синтетический)."""
        props: tuple[str, ...] = ("Дистанция (дальний бой)",) if self.weapon_ranged else ()
        return Weapon(
            name=self.weapon_name,
            dmg=self.weapon_dmg,
            versatile_dmg=None,
            damage_type="Магический" if self.damage_kind == "магический" else "Физический",
            props=props,
            aliases=(),
        )

    def armor_item(self) -> Armor:
        return Armor(
            name=self.armor_name,
            defense=self.armor_def,
            note="стартовый комплект класса",
            is_shield=False,
            aliases=(),
        )

    def shield_item(self) -> Optional[Armor]:
        if not self.shield:
            return None
        return Armor(
            name="Щит",
            defense=1,
            note="стартовый комплект класса",
            is_shield=True,
            aliases=(),
        )


# Ключи — нормализованные имена классов из class.txt.
STARTING_KITS: dict[str, StartingKit] = {
    normalize("Воин"): StartingKit(
        char_class="Воин",
        weapon_name="Длинный меч", weapon_dmg=5, damage_kind="физический", weapon_ranged=False,
        armor_name="Кольчужная рубаха", armor_def=2, shield=True, hp=28,
    ),
    normalize("Плут"): StartingKit(
        char_class="Плут",
        weapon_name="Парные кинжалы", weapon_dmg=3, damage_kind="физический", weapon_ranged=False,
        armor_name="Кожаный доспех", armor_def=1, shield=False, hp=22,
    ),
    normalize("Маг"): StartingKit(
        char_class="Маг",
        weapon_name="Боевой посох", weapon_dmg=6, damage_kind="магический", weapon_ranged=False,
        armor_name="Мантия", armor_def=0, shield=False, hp=18,
    ),
    normalize("Следопыт"): StartingKit(
        char_class="Следопыт",
        weapon_name="Охотничий лук", weapon_dmg=4, damage_kind="физический", weapon_ranged=True,
        armor_name="Кожаная куртка", armor_def=1, shield=False, hp=24,
    ),
    normalize("Инквизитор"): StartingKit(
        char_class="Инквизитор",
        weapon_name="Боевой молот", weapon_dmg=5, damage_kind="физический", weapon_ranged=False,
        armor_name="Латный нагрудник", armor_def=3, shield=False, hp=26,
    ),
    normalize("Церковник"): StartingKit(
        char_class="Церковник",
        weapon_name="Освященный посох", weapon_dmg=3, damage_kind="физический", weapon_ranged=False,
        armor_name="Ряса с кольчужкой", armor_def=1, shield=False, hp=22,
    ),
}


def start_kit_for(char_class: CharClass) -> StartingKit:
    """Стартовый комплект класса из STARTING_KITS."""
    kit = STARTING_KITS.get(normalize(char_class.name))
    if kit is None:
        raise RegistrationError(
            f"Для класса «{char_class.name}» не зафиксирован стартовый комплект в STARTING_KITS."
        )
    return kit


def kit_summary(kit: StartingKit) -> str:
    """Строка комплекта: «Воин: HP 28 | Длинный меч (урон 5) | Кольчужная рубаха (броня 2) | Щит (+1)»."""
    damage = (
        f"урон чарами {kit.weapon_dmg}"
        if kit.damage_kind == "магический"
        else f"урон {kit.weapon_dmg}"
    )
    parts = [
        f"{kit.char_class}: HP {kit.hp}",
        f"{kit.weapon_name} ({damage})",
        f"{kit.armor_name} (броня {kit.armor_def})",
        "Щит (+1)" if kit.shield else "без щита",
    ]
    return " | ".join(parts)


def class_kits(catalog_ref: Optional[Catalog] = None) -> list[StartingKit]:
    """Комплекты в порядке классов из class.txt (клавиатура и подсказки мастера)."""
    cat = catalog_ref or catalog()
    kits: list[StartingKit] = []
    for char_class in cat.all_classes:
        kit = STARTING_KITS.get(normalize(char_class.name))
        if kit is not None:
            kits.append(kit)
    return kits


def validate_starting_kits(catalog_ref: Optional[Catalog] = None) -> None:
    """Сверяет STARTING_KITS с class.txt (вызывается при загрузке справочников)."""
    cat = catalog_ref or catalog()
    known = {normalize(char_class.name) for char_class in cat.all_classes}
    for key, kit in STARTING_KITS.items():
        if key not in known:
            log.warning("STARTING_KITS: класс «%s» отсутствует в class.txt", kit.char_class)
    for char_class in cat.all_classes:
        kit = STARTING_KITS.get(normalize(char_class.name))
        if kit is None:
            log.warning("STARTING_KITS: для класса «%s» нет стартового комплекта", char_class.name)
            continue
        if kit.hp != char_class.base_hp:
            log.warning(
                "STARTING_KITS: стартовые HP класса «%s» = %d, а в class.txt — %d. Проверь баланс.",
                kit.char_class,
                kit.hp,
                char_class.base_hp,
            )


def build_character(
    chat_id: int,
    user_id: int,
    username: Optional[str],
    char_name: str,
    race: Race,
    char_class: CharClass,
    weapon: Optional[Weapon] = None,
    armor: Optional[Armor] = None,
    shield: Optional[Armor] = None,
    catalog_ref: Optional[Catalog] = None,
    kit: Optional[StartingKit] = None,
) -> CharacterBuild:
    """Считает стартовый лист персонажа.

    Режим ``kit`` — мастер регистрации из личных сообщений: оружие, броня, щит и
    HP берутся из стартового комплекта класса. Без ``kit`` предметы передаются
    явно (справочный режим с двуручным хватом и совместимостью щита).
    """
    cat = catalog_ref or catalog()
    notes: list[str] = []

    if kit is not None:
        weapon = kit.weapon()
        armor = kit.armor_item()
        shield = kit.shield_item()
    if weapon is None or armor is None:
        raise RegistrationError(
            "Для сборки героя нужно оружие и броня (или стартовый комплект класса)."
        )

    # --- Оружие: определяем, держим ли его двумя руками ---------------------
    has_versatile = weapon.versatile_dmg is not None
    two_handed_only = weapon.two_handed and not has_versatile
    shield_def = 0

    if shield is not None and two_handed_only:
        notes.append(f"⚠️ {weapon.name} — двуручное оружие, щит несовместим. Идём без щита.")
    elif shield is not None:
        shield_def = shield.defense

    if kit is not None:
        # Урон и HP комплекта фиксированы ТЗ: двуручный хват их не повышает.
        weapon_dmg = kit.weapon_dmg
        max_hp = kit.hp
        notes.append(kit_summary(kit))
        if kit.damage_kind == "магический":
            notes.append(
                f"{kit.weapon_name}: атака чарами — физическая броня цели такой урон не поглощает."
            )
    elif has_versatile and shield_def == 0:
        weapon_dmg = weapon.versatile_dmg or weapon.dmg
        max_hp = char_class.base_hp
        notes.append(f"Двуручный хват «{weapon.name}»: урон поднят до {weapon_dmg}.")
    else:
        weapon_dmg = weapon.dmg
        max_hp = char_class.base_hp

    # --- Расовые бонусы (race.txt) ------------------------------------------
    if race.name == "Орк":
        weapon_dmg += 1
        notes.append("Смертоносная сила орка: +1 к фиксированному урону.")
    if race.name == "Дварф":
        max_hp += 4
        notes.append("Каменная стойкость дварфа: +4 к максимуму здоровья.")
    if race.name == "Эльф" and weapon.ranged:
        weapon_dmg += 1
        notes.append("Меткий стрелок эльфа: +1 к урону дальнобойного оружия.")

    perk_first = char_class.perk(1)
    abilities = perk_first.as_line() if perk_first else f"{char_class.name}: базовые умения уровня 1."
    next_level_xp = cat.xp_thresholds.get(1, DEFAULT_XP_THRESHOLDS[1])

    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "char_name": char_name,
        "race": race.name,
        "racial_trait": race.trait_text,
        "char_class": char_class.name,
        "level": 1,
        "hp": max_hp,
        "max_hp": max_hp,
        "weapon_name": weapon.name,
        "weapon_dmg": weapon_dmg,
        "armor_name": armor.name,
        "armor_def": armor.defense,
        "shield_def": shield_def,
        "inventory": DEFAULT_INVENTORY,
        "xp": 0,
        "next_level_xp": next_level_xp,
        "active_abilities": abilities,
        "injuries": EMPTY_LIST_JSON,
        "active_buffs": EMPTY_LIST_JSON,
    }
    return CharacterBuild(fields=fields, notes=tuple(notes))


def available_list(catalog_map: dict[str, Any], limit: int = 12) -> str:
    """Человекочитаемый список уникальных названий из справочника."""
    names: list[str] = []
    for value in catalog_map.values():
        name = getattr(value, "name", None)
        if name and name not in names:
            names.append(name)
    shown = names[:limit]
    tail = f" и ещё {len(names) - limit}" if len(names) > limit else ""
    return ", ".join(shown) + tail


async def get_profile_player(user_id: int) -> Optional[dict[str, Any]]:
    """Лист героя, созданного в личке бота: chat_id приватного чата равен user_id."""
    return await get_player(user_id, user_id)


async def save_profile_player(fields: dict[str, Any]) -> None:
    """Сохраняет лист героя как «профиль» в личке бота (chat_id = user_id)."""
    payload = dict(fields)
    payload["chat_id"] = int(payload["user_id"])
    await insert_player(payload)


def profile_to_chat(
    profile: dict[str, Any], chat_id: int, username: Optional[str]
) -> dict[str, Any]:
    """Копия листа из лички для игрового чата (перенос героя командой /join)."""
    fields: dict[str, Any] = {"chat_id": chat_id, "user_id": int(profile["user_id"])}
    for column in sorted(PLAYER_EDITABLE_FIELDS):
        fields[column] = profile.get(column)
    if username:
        fields["username"] = username
    return fields


async def register_profile_player(
    user_id: int,
    username: Optional[str],
    char_name: str,
    race_query: str,
    class_query: str,
    catalog_ref: Optional[Catalog] = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Создаёт героя в личке бота по стартовому комплекту класса.

    Финальный шаг мастера /create: раса и класс берутся из справочников,
    снаряжение/HP/урон — из STARTING_KITS плюс расовые бонусы.
    """
    cat = catalog_ref or catalog()

    race = cat.find_race(race_query)
    if race is None:
        raise RegistrationError(
            f"Раса «{race_query}» не найдена в race.txt.\nДоступно: {available_list(cat.races)}"
        )
    char_class = cat.find_class(class_query)
    if char_class is None:
        raise RegistrationError(
            f"Класс «{class_query}» не найден в class.txt.\nДоступно: {available_list(cat.classes)}"
        )
    kit = start_kit_for(char_class)

    build = build_character(
        chat_id=user_id,          # профиль живёт в личке, поэтому chat_id == user_id
        user_id=user_id,
        username=username,
        char_name=char_name,
        race=race,
        char_class=char_class,
        catalog_ref=cat,
        kit=kit,
    )
    await save_profile_player(build.fields)
    saved = await get_profile_player(user_id) or build.fields
    log.info(
        "Герой создан в личке: user=%s «%s» (%s %s), оружие=%s (%d), броня=%d, щит=%d, hp=%d/%d",
        user_id, char_name, race.name, char_class.name,
        saved.get("weapon_name"), int(saved.get("weapon_dmg") or 0),
        int(saved.get("armor_def") or 0), int(saved.get("shield_def") or 0),
        int(saved.get("hp") or 0), int(saved.get("max_hp") or 0),
    )
    return saved, build.notes


async def join_chat_with_profile(
    profile: dict[str, Any], chat_id: int, username: Optional[str]
) -> tuple[dict[str, Any], list[int]]:
    """Переносит героя из лички в игровой чат и ставит его в очередь хода."""
    fields = profile_to_chat(profile, chat_id, username)
    user_id = int(fields["user_id"])
    await insert_player(fields)
    order = await add_player_to_turn_order(chat_id, user_id)
    saved = await get_player(chat_id, user_id) or fields
    log.info(
        "Игрок %s подключён к чату %s героем «%s» (%s %s), очередь: %s",
        user_id, chat_id, saved.get("char_name"), saved.get("race"),
        saved.get("char_class"), order,
    )
    return saved, order


# ==============================================================================
# 7. ОБРАБОТКА ОТВЕТА МАСТЕРА: STATE_UPDATE, XP, LEVEL UP
# ==============================================================================

STATE_UPDATE_RE = re.compile(r"<!--\s*STATE_UPDATE:\s*(.*?)\s*-->", re.DOTALL | re.IGNORECASE)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def parse_state_payload(raw_json: str) -> dict[str, Any]:
    """Терпимо разбирает JSON из тега: лишние символы после объекта не ломают нас."""
    candidate = (raw_json or "").strip().strip("`").strip()
    if not candidate:
        return {}
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
        log.warning("STATE_UPDATE не является объектом: %r", parsed)
        return {}
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            log.warning("Не удалось разобрать STATE_UPDATE (%s): %r", error, candidate[:300])
            return {}
        if isinstance(parsed, dict):
            return parsed
    log.warning("STATE_UPDATE не распознан: %r", candidate[:300])
    return {}


def extract_state_update(raw: str) -> tuple[str, dict[str, Any]]:
    """Вырезает служебный тег STATE_UPDATE и возвращает (текст, payload)."""
    text = raw or ""
    payload: dict[str, Any] = {}
    match = STATE_UPDATE_RE.search(text)
    if match:
        payload = parse_state_payload(match.group(1))
        text = text[: match.start()] + text[match.end() :]
    # подчищаем любые остатки служебных комментариев
    text = HTML_COMMENT_RE.sub("", text)
    return text.strip(), payload


def grant_xp(player: dict[str, Any], xp_diff: int) -> list[str]:
    """Начисляет опыт и применяет уровни. Мутирует переданный лист персонажа."""
    cat = catalog()
    messages: list[str] = []
    player["xp"] = max(0, int(player.get("xp") or 0) + xp_diff)
    level = int(player.get("level") or 1)
    next_xp = int(player.get("next_level_xp") or cat.xp_thresholds.get(level, 0))

    char_class = cat.find_class(player.get("char_class") or "")
    while level < MAX_LEVEL and next_xp > 0 and player["xp"] >= next_xp:
        player["xp"] -= next_xp
        level += 1
        perk = char_class.perk(level) if char_class else None
        hp_bonus = perk.hp_bonus if perk else 0
        player["max_hp"] = int(player.get("max_hp") or 1) + hp_bonus
        player["hp"] = player["max_hp"]
        if perk:
            abilities = (player.get("active_abilities") or "").strip()
            player["active_abilities"] = f"{abilities}\n{perk.as_line()}".strip()
            perk_note = f"Новое умение: «{perk.title}» — {perk.text}"
        else:
            perk_note = "Перк этого уровня не найден в class.txt — уровень применён без умения."
        messages.append(
            f"🎉 <b>{esc(player.get('char_name'))}</b> достигает <b>{level} уровня</b>! "
            f"Max HP: {player['max_hp']}"
            + (f" (+{hp_bonus})" if hp_bonus else "")
            + f". {esc(perk_note)} Здоровье восстановлено полностью ({player['hp']}/{player['max_hp']} HP)."
        )
        # Шаг шкалы опыта растёт с уровнем: 200 → 300 → 500 → 800 → …
        next_xp = int(cat.xp_thresholds.get(level) or DEFAULT_XP_THRESHOLDS.get(level, 200))

    if level >= MAX_LEVEL:
        next_xp = 0
    else:
        next_xp = int(cat.xp_thresholds.get(level) or DEFAULT_XP_THRESHOLDS.get(level, 200))
    player["level"] = level
    player["next_level_xp"] = next_xp
    return messages


@dataclass
class AppliedChanges:
    level_ups: list[str]
    touched: list[int]
    damaged: list[str]
    defeated: list[str]
    injuries: list[str]


async def apply_state_changes(
    chat_id: int, payload: dict[str, Any], players_by_id: dict[int, dict[str, Any]]
) -> AppliedChanges:
    """Применяет изменения из STATE_UPDATE к базе данных игроков чата."""
    outcome = AppliedChanges(level_ups=[], touched=[], damaged=[], defeated=[], injuries=[])

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        return outcome

    for change in raw_changes:
        if not isinstance(change, dict):
            continue
        user_id = as_int(change.get("user_id"))
        if user_id is None or user_id not in players_by_id:
            log.warning("STATE_UPDATE: неизвестный пользователь %r в чате %s", change.get("user_id"), chat_id)
            continue

        player = players_by_id[user_id]
        snapshot = dict(player)
        label = f"{player.get('char_name')} ({raw_mention(user_id, player.get('username'), player.get('char_name'))})"

        hp_diff = as_int(change.get("hp_diff")) or 0
        if hp_diff:
            hp_diff = clamp(hp_diff, -999, 999)
            max_hp = max(1, int(player.get("max_hp") or 1))
            new_hp = clamp(int(player.get("hp") or 0) + hp_diff, 0, max_hp)
            if new_hp != int(player.get("hp") or 0):
                player["hp"] = new_hp
                if hp_diff < 0:
                    outcome.damaged.append(f"{label}: {hp_diff} HP → {new_hp}/{max_hp}")
                else:
                    outcome.damaged.append(f"{label}: +{hp_diff} HP → {new_hp}/{max_hp}")
            if new_hp == 0:
                outcome.defeated.append(label)

        xp_diff = as_int(change.get("xp_diff")) or 0
        if xp_diff > 0:
            outcome.level_ups.extend(grant_xp(player, min(xp_diff, 10000)))

        # --- Предметы: add_items / remove_items (+ легаси inventory_add / inventory_remove) ---
        additions = [*as_text_list(change.get("add_items")), *as_text_list(change.get("inventory_add"))]
        removals = [*as_text_list(change.get("remove_items")), *as_text_list(change.get("inventory_remove"))]
        if additions or removals:
            items = inventory_items(player.get("inventory"))
            for value in additions:
                if value not in items:
                    items.append(value)
            for value in removals:
                needle = value.lower()
                if any(item.lower() == needle for item in items):
                    items = [item for item in items if item.lower() != needle]
                else:
                    items = [item for item in items if needle not in item.lower()]
            player["inventory"] = inventory_text(items)

        # --- Тяжёлые травмы: add_injuries / remove_injuries ---
        added_injuries = as_text_list(change.get("add_injuries"))
        removed_injuries = as_text_list(change.get("remove_injuries"))
        if added_injuries or removed_injuries:
            before = player_injuries(player)
            updated = add_list_values(
                remove_list_values(player.get("injuries"), removed_injuries), added_injuries
            )
            player["injuries"] = updated
            for injury in list_field(updated):
                if injury not in before:
                    outcome.injuries.append(f"{label}: получена травма «{injury}»")

        # --- Действующие эффекты, магия и яды: add_buffs / remove_buffs ---
        added_buffs = as_text_list(change.get("add_buffs"))
        removed_buffs = as_text_list(change.get("remove_buffs"))
        if added_buffs or removed_buffs:
            player["active_buffs"] = add_list_values(
                remove_list_values(player.get("active_buffs"), removed_buffs), added_buffs
            )

        diff = {
            column: player.get(column)
            for column in PLAYER_EDITABLE_FIELDS
            if player.get(column) != snapshot.get(column)
        }
        if diff:
            await update_player(chat_id, user_id, **diff)
            outcome.touched.append(user_id)

    return outcome


# ==============================================================================
# 8. ОЧЕРЕДЬ ХОДОВ, ИСТОРИЯ ДИАЛОГА И ФОРМАТИРОВАНИЕ
# ==============================================================================

history: dict[int, "deque[dict[str, str]]"] = {}     # chat_id -> последние сообщения
refusal_stamps: dict[int, dict[int, float]] = {}      # антиспам ироничных отказов


def chat_history(chat_id: int) -> deque[dict[str, str]]:
    bucket = history.get(chat_id)
    if bucket is None:
        bucket = deque(maxlen=ACTION_HISTORY_LIMIT)
        history[chat_id] = bucket
    return bucket


def remember(chat_id: int, role: str, content: str) -> None:
    chat_history(chat_id).append({"role": role, "content": content})


def refusal_allowed(chat_id: int, user_id: int) -> bool:
    """Не спамим отказами чаще, чем раз в REFUSAL_COOLDOWN_SECONDS."""
    stamps = refusal_stamps.setdefault(chat_id, {})
    now = time.monotonic()
    last = stamps.get(user_id, 0.0)
    if now - last < REFUSAL_COOLDOWN_SECONDS:
        return False
    stamps[user_id] = now
    return True


def find_player(players: Sequence[dict[str, Any]], user_id: Optional[int]) -> Optional[dict[str, Any]]:
    if user_id is None:
        return None
    for player in players:
        if int(player["user_id"]) == int(user_id):
            return player
    return None


def next_user_in_ring(order: Sequence[int], current: Optional[int]) -> Optional[int]:
    """Следующий игрок по кольцу очереди."""
    if not order:
        return None
    if current is None or current not in order:
        return order[0]
    return order[(order.index(current) + 1) % len(order)]


async def advance_turn(chat_id: int) -> tuple[list[int], Optional[int]]:
    """Сдвигает очередь на следующего игрока. Возвращает (порядок, новый активный)."""
    campaign = await get_campaign(chat_id)
    order = parse_turn_order(campaign["turn_order_json"] if campaign else [])
    current = as_int(campaign["current_turn_user_id"]) if campaign else None
    nxt = next_user_in_ring(order, current)
    await save_campaign(chat_id, turn_order=order, current_turn_user_id=nxt)
    return order, nxt


def xp_progress(player: dict[str, Any]) -> str:
    """Строка опыта: 40/200 XP, либо отметка максимального уровня."""
    next_xp = int(player.get("next_level_xp") or 0)
    xp = int(player.get("xp") or 0)
    if next_xp <= 0:
        return f"{xp} XP (МАКС. УР.)"
    return f"{xp}/{next_xp} XP"


def format_status_widget(
    players: Sequence[dict[str, Any]], current_user_id: Optional[int] = None
) -> str:
    """Виджет статуса отряда, который прикрепляется к каждому ответу мастера."""
    lines = ["📊 <b>[Статус отряда]</b>:"]
    for player in players:
        user_id = int(player["user_id"])
        hp = int(player.get("hp") or 0)
        max_hp = int(player.get("max_hp") or 0)
        marker = " 💀" if hp <= 0 else ""
        turn_marker = " ▶" if current_user_id == user_id else ""
        lines.append(
            f"• {mention_for(user_id, player.get('username'), player.get('char_name'))} "
            f"({esc(player.get('char_name'))}): {hp}/{max_hp} HP | "
            f"{xp_progress(player)} (Ур. {int(player.get('level') or 1)}){marker}{turn_marker}"
        )
    current = find_player(players, current_user_id)
    if current is not None:
        lines.append(
            f"▶ Сейчас ход: {mention_for(int(current['user_id']), current.get('username'), current.get('char_name'))}"
        )
    return "\n".join(lines)


def format_character_card(player: dict[str, Any]) -> str:
    """Полная карточка персонажа для /party и подтверждения регистрации."""
    user_id = int(player["user_id"])
    shield_def = int(player.get("shield_def") or 0)
    shield_note = f", щит +{shield_def}" if shield_def else ", без щита"
    abilities = esc(player.get("active_abilities") or "—")
    ability_lines = "\n".join(
        f"   ✦ {line.strip()}" for line in abilities.split("\n") if line.strip()
    )
    injuries = ", ".join(player_injuries(player)) or "Нет"
    return (
        f"<b>{esc(player.get('char_name'))}</b> — {esc(player.get('race'))} / "
        f"{esc(player.get('char_class'))} (Ур. {int(player.get('level') or 1)})\n"
        f"👤 Игрок: {mention_for(user_id, player.get('username'), player.get('char_name'))}\n"
        f"❤️ Здоровье: {int(player.get('hp') or 0)}/{int(player.get('max_hp') or 0)} HP\n"
        f"📈 Опыт: {xp_progress(player)}\n"
        f"🩹 Травмы: {esc(injuries)}\n"
        f"🗡 Оружие: {esc(player.get('weapon_name'))} "
        f"(фиксированный урон: {int(player.get('weapon_dmg') or 0)})\n"
        f"🛡 Защита: {esc(player.get('armor_name'))} "
        f"(поглощение броней: {int(player.get('armor_def') or 0)}{shield_note})\n"
        f"🧬 Расовые черты: {esc(player.get('racial_trait'))}\n"
        f"✨ Способности класса:\n{ability_lines or '   ✦ —'}\n"
        f"🎒 Инвентарь: {esc(player.get('inventory'))}"
    )


def format_turn_order(
    players: Sequence[dict[str, Any]], order: Sequence[int], current_user_id: Optional[int]
) -> str:
    """Человекочитаемая очередь хода."""
    if not order:
        return "Очередь хода пуста — ждём героев через /join."
    parts: list[str] = []
    for index, user_id in enumerate(order, start=1):
        player = find_player(players, user_id)
        if player is None:
            parts.append(f"{index}. <i>игрок {user_id} (вне отряда)</i>")
            continue
        marker = "▶ " if user_id == current_user_id else ""
        parts.append(
            f"{marker}{index}. {mention_for(user_id, player.get('username'), player.get('char_name'))} "
            f"— {esc(player.get('char_name'))}"
        )
    return "\n".join(parts)


def format_damage_report(outcome: AppliedChanges) -> str:
    """Короткий отчёт об изменениях состояния после хода мастера."""
    lines: list[str] = []
    if outcome.damaged:
        lines.append("🩸 <b>Изменения состояния:</b>")
        lines.extend(f"• {esc(item)}" for item in outcome.damaged)
    if outcome.injuries:
        lines.append("🩹 <b>Травмы:</b>")
        lines.extend(f"• {esc(item)}" for item in outcome.injuries)
    if outcome.level_ups:
        lines.extend(outcome.level_ups)
    if outcome.defeated:
        lines.append("💀 Без сознания: " + ", ".join(esc(item) for item in outcome.defeated))
    return "\n".join(lines)


def party_overview_text(
    players: Sequence[dict[str, Any]], order: Sequence[int], current_user_id: Optional[int]
) -> str:
    """Текст команды /party: карточки всех участников + очередь хода."""
    if not players:
        return (
            "Отряд пока пуст. Каждый герой создаётся в личке бота (/create), "
            "а в игре входит командой /join."
        )
    blocks = [format_character_card(player) for player in players]
    return "\n\n".join(
        [
            f"🧭 <b>Отряд ({len(players)}):</b>",
            *blocks,
            "🔄 <b>Очередь хода:</b>",
            format_turn_order(players, order, current_user_id),
        ]
    )


# ==============================================================================
# 9. ПРОМПТЫ ДЛЯ МАСТЕРА И КЛИЕНТ DeepSeek
# ==============================================================================

MASTER_SYSTEM_PROMPT = """Ты — Мастер (гейм-мастер) мультиплеерной тёмной фэнтези-кампании в Telegram.
Ход по очереди действует РОВНО ОДИН герой — тот, чья карточка помечена как
[АКТИВНЫЙ ИГРОК В ЭТОТ ХОД]. Другие герои в этот ход только присутствуют в сцене.

ЖЁСТКИЕ ПРАВИЛА МИРА (нарушать нельзя):
1. НИКАКИХ КУБИКОВ И СЛУЧАЙНОСТИ. Не проси броски, не пиши «возможно», «может быть», «шанс 50/50».
   Результат действия детерминирован, описывай его уверенно и конкретно.
2. Попадание при атаке гарантировано: если герой атакует — он попадает.
3. Итоговый урон = max(1, урон_атаки - поглощение_брони_цели - щит_цели).
   Магический урон (огонь, кислота, молния, психика) физическую броню игнорирует.
4. Разрешено использовать ТОЛЬКО оружие, броню, предметы и способности из карточки.
   Запрещено вводить новые предметы, заклинания и способности.
5. Не принимай решения за игроков и не описывай действия других героев от их имени.
6. Герой с 0 HP — падает без сознания, а не погибает окончательно.
7. Пиши живо и кинематографично: 3–6 абзацев прозы на русском языке, без списков и
   служебных пояснений в основном тексте. Обращайся к действующему герою по имени.

ПОЛНАЯ СВОБОДА (NO RAILS):
8. В начале кампании перед отрядом ставится глобальная цель, но ты НИКОГДА не тащишь
   игроков за собой силой. Ушли в противоположную сторону, решили грабить трактиры,
   жечь деревни или просто исследовать леса — мир органично реагирует на их выбор.
   Никаких искусственных ограничений, «непроходимых стен» и наказующих рельс.
9. Цель кампании живёт вместе с волей отряда: пошли другим путём — цели, слухи и угрозы
   мира перестраиваются вокруг их решений. Отказ от цели не ломает игру, а меняет её.

ДВА ГОРОДА, КОТОРЫЕ ОБЯЗАТЕЛЬНО ЕСТЬ В ЭТОМ МИРЕ:
10. Оплот Чистоты — людской город фанатиков-нацистов. Все не-люди (эльфы, орки, нежить,
    тифлинги, полурослики, гномы, дварфы-чужаки) здесь презираются: их клеймят, сажают
    в клетки на площадях, продают и казнят. Законы жестоки, стража подозрительна, а
    слово чужака не стоит ничего.
11. Город Ложной Утопии (в духе We Happy Few) — снаружи идеальный, стерильный город
    вечной радости и праздника: улыбки, конфетти, «Сладость» в каждом доме. В реальности
    всех жителей насильно опаивают дурманящим галлюциногеном «Сладость»; трезвых и
    отказавшихся пить считают опасными психами и жестоко «лечат». Описывай этот ужас
    изнутри, не сглаживая его.

МАТУРНОСТЬ И ВЫРАЗИТЕЛЬНОСТЬ (18+):
12. Жанр — суровый реалистичный гримдарк. Разрешено и поощряется детальное описание
    боевых увечий, крови, грязи, пыток и морального уродства мира.
13. Взрослый и сексуализированный контекст допустим, если он логичен по ситуации.
    Не цензурируй и не сглаживай острые углы, не подменяй «опасное» многоточиями.

ЖЁСТКИЙ УЧЁТ ТРАВМ:
14. Действия героя строго ограничены травмами из блока [ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ].
    Без ноги персонаж хромает, замедлен и не может бежать; без руки не удерживает щит
    или двуручник; слепота и травмы глаз лишают дальнего боя и прицеливания; тяжёлые
    ожоги и переломы мешают колдовать и лазать. Травмы не исчезают сами собой — только
    лечение, отдых, магия или явное сюжетное решение Мастера снимают их.

СЛУЖЕБНЫЙ ТЕГ В КОНЦЕ ОТВЕТА (если параметры менялись):

<!--STATE_UPDATE: {"changes": [{"user_id": {user_id}, "hp_diff": -3, "xp_diff": 40, "add_injuries": ["Отрублена левая нога"], "remove_injuries": [], "add_items": ["Кинжал гнолла"], "remove_items": []}]}-->

В changes перечисляй только реально изменившихся персонажей:
  * "user_id" — id игрока из карточки/сводки отряда;
  * "hp_diff" — изменение здоровья (урон отрицательный, лечение положительный);
  * "xp_diff" — начисленный опыт (обычно 30–60 за значимое действие);
  * "add_items" / "remove_items" — списки полученных и потерянных предметов
    (одиночный предмет можно передать строкой);
  * "add_injuries" / "remove_injuries" — списки тяжёлых травм: полученных и излеченных;
  * "add_buffs" / "remove_buffs" — списки действующих эффектов, магии и ядов;
  * при необходимости "inventory_add" / "inventory_remove" — одиночный предмет строкой.
Если изменений нет — тег не добавляй. Больше никаких тегов и комментариев в ответе быть не должно.
"""


def build_active_card(player: dict[str, Any]) -> str:
    """Карточка активного игрока в точном формате ТЗ."""
    username = raw_mention(int(player["user_id"]), player.get("username"), player.get("char_name"))
    return (
        "[АКТИВНЫЙ ИГРОК В ЭТОТ ХОД]:\n"
        f"- Персонаж: {player.get('char_name')} ({username}) | Раса: {player.get('race')} | "
        f"Класс: {player.get('char_class')} (Ур. {int(player.get('level') or 1)})\n"
        f"- Здоровье: {int(player.get('hp') or 0)}/{int(player.get('max_hp') or 0)} HP\n"
        f"- Оружие: {player.get('weapon_name')} (Фиксированный урон: {int(player.get('weapon_dmg') or 0)})\n"
        f"- Защита: {player.get('armor_name')} (Поглощение броней: {int(player.get('armor_def') or 0)}, "
        f"Щит: {int(player.get('shield_def') or 0)})\n"
        f"- Расовые черты: {player.get('racial_trait')}\n"
        f"- Способности класса: {player.get('active_abilities')}\n"
        f"- Инвентарь: {player.get('inventory')}\n"
        "[ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ]:\n"
        f"- Тяжёлые травмы: {list_or_none(player_injuries(player))}\n"
        f"- Действующие эффекты: {list_or_none(player_effects(player))}\n"
        "ПРАВИЛО МАСТЕРА: Любые действия героя строго ограничены его травмами! "
        "Без ноги персонаж хромает и замедлен; без руки не может держать щит или двуручник; "
        "слепота лишает дальнего боя.\n"
        f"- user_id для тега STATE_UPDATE: {int(player['user_id'])}"
    )


def build_party_summary(players: Sequence[dict[str, Any]], active_user_id: int) -> str:
    """Сводка отряда: остальные участники чата и их HP."""
    lines: list[str] = []
    for player in players:
        user_id = int(player["user_id"])
        if user_id == active_user_id:
            continue
        username = raw_mention(user_id, player.get("username"), player.get("char_name"))
        lines.append(
            f"- {player.get('char_name')} ({username}) — {player.get('race')} {player.get('char_class')} "
            f"(Ур. {int(player.get('level') or 1)}): {int(player.get('hp') or 0)}/"
            f"{int(player.get('max_hp') or 0)} HP, урон {int(player.get('weapon_dmg') or 0)}, "
            f"броня {int(player.get('armor_def') or 0)}"
        )
    return "\n".join(lines) if lines else "- В отряде больше никого нет."


def build_system_messages(
    chat_id: int,
    player: dict[str, Any],
    players: Sequence[dict[str, Any]],
    campaign: Optional[dict[str, Any]],
) -> list[dict[str, str]]:
    """Системный контекст: правила мастера + карточка героя + сводка отряда."""
    messages = [{"role": "system", "content": MASTER_SYSTEM_PROMPT}]

    player_context = (
        f"{build_active_card(player)}\n\n"
        f"[СВОДКА ОТРЯДА]:\n{build_party_summary(players, int(player['user_id']))}\n\n"
        "[ПРАВИЛА МАСТЕРА]:\n"
        "- Описывай результат действия детерминированно. Урон фиксирован.\n"
        "- Запрещено использовать предметы или магию, которых нет в карточке.\n"
        "- Физическая броня поглощает урон: Итоговый урон = max(1, урон_атаки - броня - щит).\n"
        "- Травмы из блока [ФИЗИЧЕСКОЕ СОСТОЯНИЕ И ТРАВМЫ] нельзя игнорировать: они жёстко\n"
        "  ограничивают действия героя до полного излечения.\n"
        "- В самом конце ответа ОБЯЗАТЕЛЬНО верни служебный JSON-тег (если параметры менялись):\n"
        '<!--STATE_UPDATE: {"changes": [{"user_id": '
        f"{int(player['user_id'])}, \"hp_diff\": -3, \"xp_diff\": 40, "
        "\"add_injuries\": [\"Отрублена левая нога\"], \"remove_injuries\": [], "
        "\"add_items\": [\"Кинжал гнолла\"], \"remove_items\": []}}]}}-->"
    )
    messages.append({"role": "system", "content": player_context})

    summary = (campaign or {}).get("story_summary") or ""
    if summary.strip():
        messages.append({"role": "system", "content": f"[СВОДКА ИСТОРИИ КАМПАНИИ]:\n{summary.strip()}"})

    messages.extend(chat_history(chat_id))
    return messages


class DeepSeekClient:
    """Асинхронный клиент DeepSeek chat/completions с повторами при сбоях."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEEPSEEK_MODEL,
        timeout: float = DEEPSEEK_TIMEOUT,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=20.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        attempts: int = 3,
    ) -> str:
        """Отправляет запрос к API и возвращает текст ответа мастера."""
        body = {
            "model": self._model,
            "messages": list(messages),
            "temperature": DEEPSEEK_TEMPERATURE if temperature is None else temperature,
            "max_tokens": DEEPSEEK_MAX_TOKENS if max_tokens is None else max_tokens,
            "stream": False,
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = await self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as error:
                last_error = DeepSeekError("DeepSeek не ответил вовремя (таймаут).")
                log.warning("Таймаут DeepSeek (попытка %s/%s)", attempt, attempts)
                await asyncio.sleep(1.5 * attempt)
                continue
            except httpx.HTTPError as error:
                last_error = DeepSeekError(f"Сетевая ошибка DeepSeek: {error}")
                log.warning("Сетевая ошибка DeepSeek (попытка %s/%s): %s", attempt, attempts, error)
                await asyncio.sleep(1.5 * attempt)
                continue
            finally:
                pass

            if response.status_code >= 500:
                last_error = DeepSeekError(f"DeepSeek вернул ошибку {response.status_code}.")
                log.warning("DeepSeek %s (попытка %s/%s)", response.status_code, attempt, attempts)
                await asyncio.sleep(1.5 * attempt)
                continue

            if response.status_code in (401, 403):
                raise DeepSeekError("DeepSeek отклонил ключ DEEPSEEK_API_KEY (401/403).")
            if response.status_code == 402:
                raise DeepSeekError("На балансе DeepSeek закончились средства (402).")
            if response.status_code == 429:
                last_error = DeepSeekError("DeepSeek ограничил частоту запросов (429).")
                await asyncio.sleep(2.0 * attempt)
                continue

            if response.status_code != 200:
                raise DeepSeekError(
                    f"DeepSeek вернул код {response.status_code}: {response.text[:300]}"
                )

            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                raise DeepSeekError(f"Неожиданный формат ответа DeepSeek: {error}") from error

            text = (content or "").strip()
            if not text:
                last_error = DeepSeekError("DeepSeek вернул пустой ответ.")
                await asyncio.sleep(1.0 * attempt)
                continue
            return text

        raise last_error or DeepSeekError("Не удалось получить ответ от DeepSeek.")

    async def summarize(self, previous_summary: str, turns: Sequence[str]) -> str:
        """Сжимает историю ходов в краткую сводку кампании."""
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты — секретарь кампании. Сожми события в плотную сводку не длиннее 1200 знаков. "
                    "Сохрани: локацию, цель отряда, ключевых NPC, важное снаряжение, раны и договорённости. "
                    "Пиши в прошедшем времени, без обращения к игрокам и без служебных тегов."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Прежняя сводка:\n{previous_summary or '—'}\n\n"
                    f"Новые события:\n" + "\n".join(f"- {turn}" for turn in turns)
                ),
            },
        ]
        summary = await self.chat(messages, temperature=0.4, max_tokens=700, attempts=2)
        return summary[:4000]


deepseek: Optional[DeepSeekClient] = None


def deepseek_client() -> DeepSeekClient:
    if deepseek is None:
        raise DeepSeekError("Клиент DeepSeek не инициализирован")
    return deepseek


# ==============================================================================
# 10. ХЕНДЛЕРЫ
# ==============================================================================

router = Router(name="story_bot")
BOT_INFO: dict[str, Any] = {"id": 0, "username": None}


def bot_id() -> int:
    return int(BOT_INFO.get("id") or 0)


def bot_username() -> Optional[str]:
    return BOT_INFO.get("username")


def is_group_chat(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def strip_bot_mentions(text: str) -> str:
    username = bot_username()
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip(" ,:\n\t")


def addressed_to_bot(message: Message) -> bool:
    """Сообщение адресовано боту: тег, реплай на бота или text_mention."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.id == bot_id():
        return True
    username = bot_username()
    lowered = message_text(message).lower()
    if username and f"@{username.lower()}" in lowered:
        return True
    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user is not None and entity.user.id == bot_id():
            return True
    return False


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Администратор чата или глобальный админ из ADMIN_IDS."""
    if user_id in GLOBAL_ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as error:
        log.debug("Не удалось проверить права %s в %s: %s", user_id, chat_id, error)
        return False
    return str(getattr(member, "status", "")) in {"ChatMemberStatus.CREATOR", "ChatMemberStatus.ADMINISTRATOR",
                                                  "creator", "administrator"}


async def safe_send(bot: Bot, chat_id: int, text: str, reply_to: Optional[int] = None) -> bool:
    """Отправляет HTML-сообщение (при необходимости режет на части)."""
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        try:
            if index == 0 and reply_to is not None:
                try:
                    await bot.send_message(
                        chat_id,
                        chunk,
                        reply_parameters=ReplyParameters(
                            message_id=reply_to, allow_sending_without_reply=True
                        ),
                    )
                    continue
                except TelegramBadRequest as error:
                    log.debug("Реплай не удался (%s), отправляем обычным сообщением", error)
            await bot.send_message(chat_id, chunk)
        except TelegramForbiddenError:
            log.warning("Нет прав писать в чат %s", chat_id)
            return False
        except TelegramBadRequest as error:
            log.error("Telegram отклонил сообщение в чат %s: %s", chat_id, error)
            try:
                await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", chunk))
            except TelegramAPIError:
                return False
        except TelegramAPIError as error:
            log.error("Ошибка отправки в чат %s: %s", chat_id, error)
            return False
    return True


async def safe_send_inline(
    bot: Bot,
    chat_id: int,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
    reply_to: Optional[int] = None,
) -> bool:
    """Отправляет HTML-сообщение одним куском с инлайн-клавиатурой (клики мастера)."""
    kwargs: dict[str, Any] = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    if keyboard is not None:
        kwargs["reply_markup"] = keyboard
    if reply_to is not None:
        kwargs["reply_parameters"] = ReplyParameters(
            message_id=reply_to, allow_sending_without_reply=True
        )
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        log.warning("Нет прав писать в чат %s", chat_id)
        return False
    except TelegramBadRequest as error:
        log.error("Telegram отклонил сообщение с клавиатурой в чат %s: %s", chat_id, error)
        try:
            await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text))
        except TelegramAPIError:
            return False
    except TelegramAPIError as error:
        log.error("Ошибка отправки в чат %s: %s", chat_id, error)
        return False
    return True


HELP_TEXT = (
    "🎲 <b>Сторителлинг-бот: детерминированная система без кубиков</b>\n\n"
    "<b>Как создать героя:</b>\n"
    "1. Напиши боту в ЛИЧНЫХ сообщениях /create (или /start) — пошаговый мастер спросит имя, "
    "покажет кнопки 9 рас и 6 классов и выдаст стартовое снаряжение.\n"
    "2. Добавь бота в игровой чат и напиши там /join — герой встанет в очередь хода.\n\n"
    "<b>Команды:</b>\n"
    "/create — создать героя в личке (мастер: имя → раса → класс)\n"
    "/join — вступить в отряд игрового чата (без аргументов: герой берётся из лички)\n"
    "/party — полные карточки отряда и очередь хода\n"
    "/skip — передать ход следующему (админ или текущий игрок)\n"
    "/reset_story — сбросить кампанию чата (только админы)\n"
    "/cancel — прервать создание героя\n"
    "/help — эта справка\n\n"
    "<b>Как играть:</b> когда ваш черёд — пишите боту действие обычным сообщением "
    "(или ответом на сообщение бота). Ходы вне очереди бот перехватывает и не тратит впустую.\n"
    "Попадание гарантировано, урон фиксирован, броня поглощает: "
    "<code>урон = max(1, атака - броня - щит)</code>."
)


def is_private_chat(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """В личке — запуск мастера создания героя, в группе — справка и ссылка на личку."""
    if is_private_chat(message):
        await begin_creation(message.bot, message.chat.id, message.from_user, state)
        return
    await safe_send_inline(
        message.bot,
        message.chat.id,
        f"{HELP_TEXT}\n\n🧙 Новый герой создаётся в личке бота: нажми кнопку ниже "
        "или напиши мне в личные сообщения /create.",
        keyboard_deeplink(),
        reply_to=message.message_id,
    )


@router.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext) -> None:
    """Создание героя: в личке — мастер, в группе — ссылка на личные сообщения."""
    if is_private_chat(message):
        await begin_creation(message.bot, message.chat.id, message.from_user, state)
        return
    await safe_send_inline(
        message.bot,
        message.chat.id,
        f"{NO_HERO_IN_GROUP}\n\n🧙 Открой личные сообщения с ботом и напиши /create: "
        "мастер пошагово соберёт героя, а потом возвращайся сюда с /join.",
        keyboard_deeplink(),
        reply_to=message.message_id,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await safe_send(message.bot, message.chat.id, HELP_TEXT, reply_to=message.message_id)


# ==============================================================================
# 10.1 ПОШАГОВЫЙ МАСТЕР СОЗДАНИЯ ГЕРОЯ В ЛИЧНЫХ СООБЩЕНИЯХ (Aiogram FSM)
# ==============================================================================
# Шаг 1: имя → шаг 2: раса (инлайн-кнопки) → шаг 3: класс (инлайн-кнопки) →
# итоговая карточка и автоматическая выдача стартового комплекта класса.

MAX_CHAR_NAME_LENGTH = 32

RACE_CB_PREFIX = "race:"
CLASS_CB_PREFIX = "cls:"
CANCEL_CB = "create:cancel"
RESTART_CB = "create:restart"

NO_HERO_IN_GROUP = "⚠️ Сначала создайте персонажа в личных сообщениях с ботом!"
CREATE_HINT = (
    "🧙 Здесь, в личных сообщениях, создаётся герой: напиши /create (или /start). "
    "А в игровом чате напиши /join — герой встанет в очередь хода."
)
CREATED_TEXT = (
    "✅ <b>Персонаж успешно создан!</b> Теперь добавьте бота в игровой чат "
    "или напишите там /join для участия в приключении."
)


class CharCreation(StatesGroup):
    """Состояния мастера создания героя (работает только в личных сообщениях)."""

    waiting_name = State()
    waiting_race = State()
    waiting_class = State()


def clean_char_name(raw: str) -> str:
    """Валидирует имя героя, введённое в личке."""
    name = re.sub(r"\s+", " ", (raw or "").strip().strip("«»\"'").strip())
    if name.startswith("/"):
        raise RegistrationError("Имя не может быть командой. Напиши обычное имя, например «Торн».")
    if not (1 <= len(name) <= MAX_CHAR_NAME_LENGTH):
        raise RegistrationError(
            f"Имя героя должно быть от 1 до {MAX_CHAR_NAME_LENGTH} символов."
        )
    return name


def creation_deeplink() -> Optional[str]:
    """Ссылка t.me/bot_username?start=create — открывает мастера в личке."""
    username = bot_username()
    if not username:
        return None
    return f"https://t.me/{username}?start=create"


def keyboard_deeplink() -> Optional[InlineKeyboardMarkup]:
    """Кнопка-ссылка «создать героя в личке» (для игровых чатов)."""
    link = creation_deeplink()
    if link is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧙 Создать героя в личных сообщениях", url=link)]
        ]
    )


def _keyboard_rows(
    labels: Sequence[tuple[str, str]], per_row: int = 2
) -> list[list[InlineKeyboardButton]]:
    """Раскладывает кнопки (текст, callback_data) по рядам."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for text, data in labels:
        row.append(InlineKeyboardButton(text=text, callback_data=data))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def keyboard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отменить создание", callback_data=CANCEL_CB)]
        ]
    )


def race_button_label(race: Race) -> str:
    """Короткое имя расы для кнопки: «Полурослик / Хафлинг» → «Полурослик»."""
    return race.name.split(" / ")[0]


def keyboard_races() -> InlineKeyboardMarkup:
    """9 рас из race.txt, по две в ряд."""
    labels = [
        (race_button_label(race), f"{RACE_CB_PREFIX}{index}")
        for index, race in enumerate(catalog().all_races)
    ]
    labels.append(("✖️ Отмена", CANCEL_CB))
    return InlineKeyboardMarkup(inline_keyboard=_keyboard_rows(labels, per_row=2))


def keyboard_classes() -> InlineKeyboardMarkup:
    """6 классов из class.txt, по две в ряд."""
    labels = [
        (char_class.name, f"{CLASS_CB_PREFIX}{index}")
        for index, char_class in enumerate(catalog().all_classes)
    ]
    labels.append(("✖️ Отмена", CANCEL_CB))
    return InlineKeyboardMarkup(inline_keyboard=_keyboard_rows(labels, per_row=2))


def keyboard_after_creation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧙 Создать героя заново", callback_data=RESTART_CB)]
        ]
    )


def callback_chat_id(callback: CallbackQuery) -> Optional[int]:
    """chat_id сообщения, к которому привязана инлайн-кнопка."""
    chat = getattr(callback.message, "chat", None)
    return int(chat.id) if chat is not None else None


def race_step_text(char_name: str) -> str:
    """Шаг 2: текст с просьбой выбрать расу."""
    return (
        f"🧙 Шаг 2/3 — <b>{esc(char_name)}</b>, выбери расу:\n\n"
        f"<i>Доступно {len(catalog().all_races)} рас из race.txt. Расовые бонусы "
        "(Дварф +4 HP, Орк +1 к урону, Эльф +1 к дальнему бою) встанут в карточку.</i>"
    )


def class_step_text(char_name: str, race: Race) -> str:
    """Шаг 3: выбор класса + стартовое снаряжение каждого класса."""
    lines = [
        f"🧙 Шаг 3/3 — выбери класс для <b>{esc(char_name)}</b> ({esc(race.name)}):",
        "",
        "<b>Стартовое снаряжение классов:</b>",
    ]
    lines.extend(f"• {esc(kit_summary(kit))}" for kit in class_kits())
    lines.extend(["", "<i>Комплект и стартовые статы выдаются автоматически.</i>"])
    return "\n".join(lines)


async def begin_creation(bot: Bot, chat_id: int, user: Optional[User], state: FSMContext) -> None:
    """Шаг 1 мастера: сбрасывает состояние и просит имя персонажа."""
    if user is None or user.is_bot:
        return
    await state.clear()
    existing = await get_profile_player(user.id)
    await state.set_state(CharCreation.waiting_name)
    await state.update_data(user_id=user.id, char_name=None, race=None)

    lines = [
        "🧙 <b>Создание персонажа</b> — шаг 1/3",
        "",
        f"Напиши имя героя одним сообщением (до {MAX_CHAR_NAME_LENGTH} символов).",
    ]
    if existing is not None:
        lines.append(f"\n♻️ Прежний герой «{esc(existing.get('char_name'))}» будет заменён новым.")
    lines.append("\nПрервать: /cancel")
    await safe_send_inline(bot, chat_id, "\n".join(lines), keyboard_cancel())


@router.message(CharCreation.waiting_name, F.chat.type == ChatType.PRIVATE)
async def step_name(message: Message, state: FSMContext) -> None:
    """Шаг 1: получаем имя героя и показываем кнопки рас."""
    if message.from_user is None or message.from_user.is_bot:
        return
    try:
        char_name = clean_char_name(message_text(message))
    except RegistrationError as error:
        await safe_send_inline(
            message.bot,
            message.chat.id,
            f"⚠️ {error}",
            keyboard_cancel(),
            reply_to=message.message_id,
        )
        return
    await state.update_data(char_name=char_name)
    await state.set_state(CharCreation.waiting_race)
    await safe_send_inline(
        message.bot,
        message.chat.id,
        race_step_text(char_name),
        keyboard_races(),
        reply_to=message.message_id,
    )


@router.callback_query(CharCreation.waiting_race, F.data.startswith(RACE_CB_PREFIX))
async def step_race(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 2: раса выбрана — показываем кнопки классов."""
    await callback.answer()
    chat_id = callback_chat_id(callback)
    if chat_id is None:
        return
    races = catalog().all_races
    index = as_int((callback.data or "")[len(RACE_CB_PREFIX):])
    if index is None or not 0 <= index < len(races):
        await state.clear()
        await safe_send_inline(
            callback.bot, chat_id, "⚠️ Кнопка устарела — запусти создание героя заново: /create", None
        )
        return
    race = races[index]
    data = await state.get_data()
    char_name = str(data.get("char_name") or "герой")
    await state.update_data(race=race.name)
    await state.set_state(CharCreation.waiting_class)
    await safe_send_inline(
        callback.bot, chat_id, class_step_text(char_name, race), keyboard_classes()
    )


@router.callback_query(CharCreation.waiting_class, F.data.startswith(CLASS_CB_PREFIX))
async def step_class(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 3: класс выбран — выдаём комплект, сохраняем героя и показываем карточку."""
    await callback.answer()
    chat_id = callback_chat_id(callback)
    if chat_id is None:
        return
    classes = catalog().all_classes
    index = as_int((callback.data or "")[len(CLASS_CB_PREFIX):])
    if index is None or not 0 <= index < len(classes):
        await state.clear()
        await safe_send_inline(
            callback.bot, chat_id, "⚠️ Кнопка устарела — запусти создание героя заново: /create", None
        )
        return

    user = callback.from_user
    data = await state.get_data()
    char_name = data.get("char_name")
    race_query = data.get("race")
    if user is None or not char_name or not race_query:
        await state.clear()
        await safe_send_inline(
            callback.bot,
            chat_id,
            "⚠️ Сессия создания потерялась — начни заново: /create",
            keyboard_cancel(),
        )
        return

    char_class = classes[index]
    try:
        player, notes = await register_profile_player(
            user_id=user.id,
            username=user.username,
            char_name=str(char_name),
            race_query=str(race_query),
            class_query=char_class.name,
        )
    except RegistrationError as error:
        await state.clear()
        await safe_send_inline(
            callback.bot, chat_id, f"⚠️ {error}", keyboard_after_creation()
        )
        return
    except StoryBotError as error:
        log.exception("Сбой создания героя в личке у %s", user.id)
        await state.clear()
        await safe_send_inline(
            callback.bot,
            chat_id,
            f"💥 Не смог создать героя: {esc(error)}",
            keyboard_after_creation(),
        )
        return

    await state.clear()
    blocks = [
        "🎒 <b>Стартовое снаряжение:</b>",
        esc(kit_summary(start_kit_for(char_class))),
        "",
        format_character_card(player),
    ]
    if notes:
        blocks.extend(["", "⚙️ <b>Баланс по справочникам:</b>"])
        blocks.extend(f"• {esc(note)}" for note in notes)
    blocks.extend(["", CREATED_TEXT])
    await safe_send_inline(
        callback.bot, chat_id, "\n".join(blocks), keyboard_after_creation()
    )


@router.callback_query(F.data == CANCEL_CB)
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Отменить создание»."""
    await callback.answer("Создание отменено")
    await state.clear()
    chat_id = callback_chat_id(callback)
    if chat_id is None:
        return
    await safe_send_inline(
        callback.bot, chat_id, "✖️ Создание героя отменено. Запустить заново: /create", None
    )


@router.callback_query(F.data == RESTART_CB)
async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Создать героя заново»."""
    await callback.answer()
    chat_id = callback_chat_id(callback)
    if chat_id is None:
        return
    await begin_creation(callback.bot, chat_id, callback.from_user, state)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает создание героя в личке."""
    active = await state.get_state() is not None
    await state.clear()
    text = (
        "✖️ Создание героя отменено. Запустить заново: /create"
        if active
        else "Сейчас ничего не создаётся. Начать в личке: /create"
    )
    await safe_send(message.bot, message.chat.id, text, reply_to=message.message_id)

@router.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject) -> None:
    """/join в игровом чате БЕЗ аргументов: герой берётся из личных сообщений бота."""
    user = message.from_user
    if user is None or user.is_bot:
        return
    chat_id = message.chat.id

    if not is_group_chat(message):
        await safe_send(message.bot, chat_id, CREATE_HINT, reply_to=message.message_id)
        return

    existing = await get_player(chat_id, user.id)
    if existing is not None:
        await add_player_to_turn_order(chat_id, user.id)
        await safe_send(
            message.bot,
            chat_id,
            f"Ты уже в отряде, {mention_for(user.id, user.username, existing.get('char_name'))}. "
            "Твой лист:\n\n"
            f"{format_character_card(existing)}\n\n"
            "Полный состав смотри командой /party. Пересоздать героя можно в личке бота: /create.",
            reply_to=message.message_id,
        )
        return

    profile = await get_profile_player(user.id)
    if profile is None:
        await safe_send_inline(
            message.bot,
            chat_id,
            f"{NO_HERO_IN_GROUP}\n\n🧙 Создай героя в личных сообщениях с ботом: нажми кнопку "
            "ниже или напиши мне /create, а затем вернись сюда и напиши /join.",
            keyboard_deeplink(),
            reply_to=message.message_id,
        )
        return

    try:
        player, order = await join_chat_with_profile(profile, chat_id, user.username)
    except StoryBotError as error:
        log.exception("Сбой подключения героя в чате %s", chat_id)
        await safe_send(
            message.bot,
            chat_id,
            f"💥 Не смог подключить героя: {esc(error)}",
            reply_to=message.message_id,
        )
        return

    players = await list_players(chat_id)
    campaign = await get_campaign(chat_id)
    current = as_int(campaign["current_turn_user_id"]) if campaign else None

    blocks = [
        f"✅ {mention_for(user.id, user.username, player.get('char_name'))} вступает в отряд!",
        "",
        format_character_card(player),
    ]
    if (command.args or "").strip():
        blocks.extend(
            [
                "",
                "ℹ️ Аргументы больше не нужны: /join сам берёт твоего героя из личных сообщений бота.",
            ]
        )
    blocks.extend(
        [
            "",
            "🔄 <b>Очередь хода:</b>",
            format_turn_order(players, order, current),
            "",
        ]
    )

    if current == user.id:
        blocks.append(
            TURN_TAG_TEMPLATE.format(
                mention=mention_for(user.id, user.username, player.get("char_name")),
                current=mention_for(user.id, user.username, player.get("char_name")),
            )
        )
    else:
        current_player = find_player(players, current)
        if current_player is not None:
            blocks.append(
                "⏳ Твой ход начнётся после: "
                + mention_for(
                    int(current_player["user_id"]),
                    current_player.get("username"),
                    current_player.get("char_name"),
                )
            )
        else:
            blocks.append(f"⏳ {esc(turn_player_hint())}")

    await safe_send(message.bot, chat_id, "\n".join(blocks), reply_to=message.message_id)


def turn_player_hint() -> str:
    return "Сейчас действует другой герой — дождись своей очереди."


@router.message(Command("party"))
async def cmd_party(message: Message) -> None:
    chat_id = message.chat.id
    if not is_group_chat(message):
        await safe_send(
            message.bot,
            chat_id,
            "🧙 В личке живёт личный кабинет героя: создать или пересоздать его — /create.\n"
            "Карточки отряда и очередь хода смотри в игровом чате командой /party.",
            reply_to=message.message_id,
        )
        return
    players = await list_players(chat_id)
    campaign = await get_campaign(chat_id)
    order = parse_turn_order(campaign["turn_order_json"] if campaign else None)
    current = as_int(campaign["current_turn_user_id"]) if campaign else None
    await safe_send(
        message.bot,
        chat_id,
        party_overview_text(players, order, current),
        reply_to=message.message_id,
    )


@router.message(Command("skip"))
async def cmd_skip(message: Message) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return

    campaign = await get_campaign(chat_id)
    players = await list_players(chat_id)
    order = parse_turn_order(campaign["turn_order_json"] if campaign else None)
    current = as_int(campaign["current_turn_user_id"]) if campaign else None

    if not players:
        await safe_send(
            message.bot, chat_id, "Отряд пуст — некого пропускать.", reply_to=message.message_id
        )
        return
    if current is None:
        await safe_send(
            message.bot,
            chat_id,
            "Ход и так свободен. Напиши действие, а я передам слово дальше.",
            reply_to=message.message_id,
        )
        return

    allowed = user.id == current or await is_admin(message.bot, chat_id, user.id)
    if not allowed:
        await safe_send(
            message.bot,
            chat_id,
            "Передавать чужой ход может только его владелец или админ чата.",
            reply_to=message.message_id,
        )
        return

    async with chat_lock(chat_id):
        campaign = await get_campaign(chat_id)
        current = as_int(campaign["current_turn_user_id"]) if campaign else None
        order = parse_turn_order(campaign["turn_order_json"] if campaign else None)
        if current is None:
            return
        order, nxt = await advance_turn(chat_id)
        players = await list_players(chat_id)
        skipped = find_player(players, current)
        nxt_player = find_player(players, nxt)
        lines = [
            "⏭ Ход пропущен: "
            + (
                mention_for(current, skipped.get("username"), skipped.get("char_name"))
                if skipped is not None
                else f"игрок {current}"
            )
            + ".",
            "",
            format_status_widget(players, nxt),
        ]
        if nxt_player is not None:
            lines.extend(
                [
                    "",
                    TURN_TAG_TEMPLATE.format(
                        mention=mention_for(
                            int(nxt_player["user_id"]),
                            nxt_player.get("username"),
                            nxt_player.get("char_name"),
                        ),
                        current=mention_for(
                            int(nxt_player["user_id"]),
                            nxt_player.get("username"),
                            nxt_player.get("char_name"),
                        ),
                    ),
                ]
            )
        else:
            lines.extend(["", "Очередь пуста — пригласи игроков командой /join."])
        await safe_send(message.bot, chat_id, "\n".join(lines), reply_to=message.message_id)


@router.message(Command("reset_story"))
async def cmd_reset_story(message: Message) -> None:
    chat_id = message.chat.id
    user = message.from_user
    if user is None:
        return
    if not is_group_chat(message):
        await safe_send(
            message.bot,
            chat_id,
            "🧹 Сброс кампании работает в игровом чате. Здесь, в личке, герой только "
            "создаётся и пересоздаётся: /create.",
            reply_to=message.message_id,
        )
        return
    if not await is_admin(message.bot, chat_id, user.id):
        await safe_send(
            message.bot,
            chat_id,
            "Полный сброс кампании доступен только администраторам чата.",
            reply_to=message.message_id,
        )
        return

    async with chat_lock(chat_id):
        await reset_chat(chat_id)
        history.pop(chat_id, None)
        refusal_stamps.pop(chat_id, None)
        turns_since_summary.pop(chat_id, None)

    await safe_send(
        message.bot,
        chat_id,
        "🧹 Кампания сброшена: листы героев в этом чате, очередь хода и сводка истории стёрты.\n"
        f"Начинаем заново — {mention_for(user.id, user.username)}!\n"
        "🧙 Герой создаётся в личке бота (/create), а в чат входит командой /join.",
        reply_to=message.message_id,
    )


# ==============================================================================
# 11. ОСНОВНОЙ ЦИКЛ ХОДА: ПЕРЕХВАТ ОЧЕРЕДИ, ВЫЗОВ МАСТЕРА, ПРОГРЕСС
# ==============================================================================

turns_since_summary: dict[int, int] = {}


def chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_action_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_action_locks[chat_id] = lock
    return lock


def pick_refusal() -> str:
    """Ироничный отказ. Рандом нужен только для текста реплики, не для механик."""
    return random.choice(REFUSAL_TEMPLATES)


async def refuse_out_of_turn(
    bot: Bot, chat_id: int, player: dict[str, Any], current_player: Optional[dict[str, Any]]
) -> None:
    """Перехват хода вне очереди: без вызова DeepSeek, один раз в 20 секунд на игрока."""
    user_id = int(player["user_id"])
    if not refusal_allowed(chat_id, user_id):
        log.debug("Отказ в чате %s игроку %s подавлен кулдауном", chat_id, user_id)
        return
    current_mention = (
        mention_for(
            int(current_player["user_id"]),
            current_player.get("username"),
            current_player.get("char_name"),
        )
        if current_player is not None
        else "другой герой"
    )
    text = pick_refusal().format(
        mention=mention_for(user_id, player.get("username"), player.get("char_name")),
        current=current_mention,
    )
    await safe_send(bot, chat_id, text)


async def ensure_active_turn(chat_id: int, campaign: Optional[dict[str, Any]]) -> Optional[int]:
    """Если очередь есть, а активный ход не выставлен — поднимает первого в очереди."""
    order = parse_turn_order(campaign["turn_order_json"] if campaign else None)
    current = as_int(campaign["current_turn_user_id"]) if campaign else None
    if not order:
        return None
    if current is None or current not in order:
        current = order[0]
        await save_campaign(chat_id, turn_order=order, current_turn_user_id=current)
    return current


async def run_turn(message: Message, player: dict[str, Any], action: str) -> None:
    """Основной ход: история → DeepSeek → тег STATE_UPDATE → смена очереди → виджет."""
    user = message.from_user
    if user is None:
        return
    chat_id = message.chat.id
    bot = message.bot
    counter = 0

    async with chat_lock(chat_id):
        players = await list_players(chat_id)
        campaign = await get_campaign(chat_id)
        players_by_id = {int(item["user_id"]): item for item in players}
        current = as_int(campaign["current_turn_user_id"]) if campaign else None

        if current != user.id:
            log.info("Ход в чате %s ушёл, пока игрок %s писал действие", chat_id, user.id)
            await refuse_out_of_turn(bot, chat_id, player, find_player(players, current))
            return

        active = players_by_id.get(user.id, player)
        remember(
            chat_id,
            "user",
            f"{active.get('char_name')} ({active.get('race')} {active.get('char_class')}): {action}",
        )

        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramAPIError as error:
            log.debug("Не удалось показать «печатает» в чате %s: %s", chat_id, error)

        messages = build_system_messages(chat_id, active, players, campaign)
        try:
            raw_answer = await deepseek_client().chat(messages)
        except DeepSeekError as error:
            log.warning("Мастер не ответил в чате %s: %s", chat_id, error)
            remember(chat_id, "assistant", "«Мастер не отозвался» — сцена не сдвинулась.")
            await safe_send(
                bot,
                chat_id,
                "💥 Мастер не смог ответить: "
                f"{esc(error)}\nХод не потрачен — напиши действие ещё раз.",
                reply_to=message.message_id,
            )
            return
        except StoryBotError as error:
            log.exception("Сбой клиента DeepSeek в чате %s", chat_id)
            await safe_send(
                bot,
                chat_id,
                f"💥 Внутренняя ошибка мастера: {esc(error)}",
                reply_to=message.message_id,
            )
            return

        story, payload = extract_state_update(raw_answer)
        if not story:
            story = "Мастер молча кивает — сцене нужно более конкретное действие."
        remember(chat_id, "assistant", story)

        outcome = await apply_state_changes(chat_id, payload, players_by_id)
        order, nxt = await advance_turn(chat_id)

        counter = turns_since_summary.get(chat_id, 0) + 1
        turns_since_summary[chat_id] = counter

        fresh_players = await list_players(chat_id)
        next_player = find_player(fresh_players, nxt)

        footer: list[str] = []
        report = format_damage_report(outcome)
        if report:
            footer.append(report)
        footer.append(format_status_widget(fresh_players, nxt))
        if next_player is not None:
            next_mention = mention_for(
                int(next_player["user_id"]),
                next_player.get("username"),
                next_player.get("char_name"),
            )
            footer.append(TURN_TAG_TEMPLATE.format(mention=next_mention, current=next_mention))
        else:
            footer.append("Очередь пуста — герои закончились, зови новых через /join.")

        reply = f"{story}\n\n{'—' * 12}\n\n" + "\n\n".join(footer)
        await safe_send(bot, chat_id, reply, reply_to=message.message_id)

    if counter >= SUMMARY_EVERY_TURNS:
        await maybe_summarize(chat_id)


async def maybe_summarize(chat_id: int) -> None:
    """Раз в SUMMARY_EVERY_TURNS ходов ужимает историю в story_summary кампании."""
    turns_since_summary[chat_id] = 0
    entries = [entry.get("content", "") for entry in chat_history(chat_id) if entry.get("content")]
    if not entries:
        return
    campaign = await get_campaign(chat_id)
    previous = (campaign or {}).get("story_summary") or ""
    try:
        summary = await deepseek_client().summarize(previous, entries)
    except StoryBotError as error:
        log.warning("Не удалось сжать историю чата %s: %s", chat_id, error)
        return
    await save_campaign(chat_id, story_summary=summary)
    log.info("История чата %s сжата в сводку (%s знаков)", chat_id, len(summary))


MAX_ACTION_LENGTH = 1200


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message) -> None:
    """Перехват сообщений в группе: строгий ход по очереди и вызов Мастера."""
    user = message.from_user
    if user is None or user.is_bot:
        return

    raw_text = message_text(message)
    if not raw_text:
        return

    chat_id = message.chat.id
    addressed = addressed_to_bot(message)
    looks_like_command = raw_text.startswith("/")

    player = await get_player(chat_id, user.id)
    campaign = await get_campaign(chat_id)
    current = await ensure_active_turn(chat_id, campaign)
    players = await list_players(chat_id)

    if player is None:
        if addressed or looks_like_command:
            await safe_send_inline(
                message.bot,
                chat_id,
                f"{NO_HERO_IN_GROUP}\n"
                "🧙 Ты пока не в отряде — открой личные сообщения с ботом, напиши /create "
                "(мастер соберёт героя), а затем вернись сюда и напиши /join.",
                keyboard_deeplink(),
                reply_to=message.message_id,
            )
        return

    if current != user.id:
        # Ход вне очереди — перехватываем в коде, API не тратим.
        if addressed:
            await refuse_out_of_turn(message.bot, chat_id, player, find_player(players, current))
        return

    if not addressed:
        if refusal_allowed(chat_id, user.id):
            await safe_send(
                message.bot,
                chat_id,
                "🎯 Сейчас твой черёд, "
                f"{mention_for(user.id, user.username, player.get('char_name'))}! "
                "Опиши действие ответом на сообщение бота или с тегом бота — и Мастер продолжит сцену.",
                reply_to=message.message_id,
            )
        return

    action = strip_bot_mentions(raw_text)
    if not action:
        await safe_send(
            message.bot,
            chat_id,
            "Опиши действие текстом — я передам его Мастеру.",
            reply_to=message.message_id,
        )
        return

    if len(action) > MAX_ACTION_LENGTH:
        action = action[:MAX_ACTION_LENGTH].rstrip() + "…"
    await run_turn(message, player, action)


@router.message(F.chat.type == ChatType.PRIVATE)
async def on_private_message(message: Message) -> None:
    """Прочие сообщения в личке: подсказка про мастера создания героя.

    Сообщения шагов /create перехватываются раньше (они зарегистрированы выше),
    сюда попадает только свободный текст в личке вне мастера.
    """
    if message.from_user is None or message.from_user.is_bot:
        return
    await safe_send(
        message.bot,
        message.chat.id,
        "🧙 Я собираю героев пошаговым мастером: напиши /create (или /start) — "
        "спрошу имя, покажу кнопки рас и классов.\n"
        "В игровом чате герой входит командой /join. Справка: /help",
        reply_to=message.message_id,
    )


# ==============================================================================
# 12. BOOTSTRAP
# ==============================================================================

DISCLAIMER = (
    "⚠️ Privacy Mode бота должен быть ВЫКЛЮЧЕН в @BotFather "
    "(/mybots → Bot Settings → Group Privacy → Turn off), "
    "иначе бот не увидит обычные сообщения и не сможет перехватывать ходы."
)


async def on_startup(bot: Bot) -> None:
    global deepseek
    configure_logging()
    load_catalog()
    await init_db()
    deepseek = DeepSeekClient(DEEPSEEK_API_KEY)
    me = await bot.get_me()
    BOT_INFO["id"] = me.id
    BOT_INFO["username"] = me.username
    log.info("Бот запущен: @%s (id=%s)", me.username, me.id)
    log.info("Модель: %s | БД: %s", DEEPSEEK_MODEL, DB_PATH)
    if TELEGRAM_PROXY:
        log.info("Telegram через прокси: %s", TELEGRAM_PROXY)
    log.info(DISCLAIMER)


async def on_shutdown(bot: Bot) -> None:
    global deepseek
    if deepseek is not None:
        await deepseek.aclose()
        deepseek = None
    await close_db()
    log.info("Бот остановлен, ресурсы освобождены.")


async def run_bot() -> None:
    configure_logging()

    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан. Добавь его в %s", BASE_DIR / ".env")
        return
    if not DEEPSEEK_API_KEY:
        log.error("DEEPSEEK_API_KEY не задан. Добавь его в %s", BASE_DIR / ".env")
        return

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил BOT_TOKEN — проверь токен в .env")
        await bot.session.close()
        return
    except TelegramAPIError as error:
        log.warning("Не удалось сбросить вебхук: %s", error)

    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем (Ctrl+C).")
    except Exception:
        log.exception("Фатальная ошибка при запуске бота")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

