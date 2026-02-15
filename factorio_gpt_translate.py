"""
factorio_gpt_translate.py

Перевод локализаций модов Factorio (CFG) через OpenAI Responses API с web_search.

Ключевые требования (под ваши слова):
- Переводить ТОЛЬКО значения после '=' (ключи и [section] не трогать).
- Возвращать ПОЛНЫЙ файл(ы) целиком (включая уже-русские строки).
- Сохранять форматирование, порядок строк, пустые строки, комментарии и пометки после ';'.
- Допускать web_search, чтобы модель смотрела описание мода и общепринятые термины.
- Обеспечивать однородность перевода терминов внутри мода.

Зависимости:
    pip install openai

Переменная окружения:
    export OPENAI_API_KEY="..."

Пример:
    from factorio_gpt_translate import translate_mod_locales_inplace, ModSpec
    translate_mod_locales_inplace(
        mod_dir="mods/unpacked/MyMod",
        mod=ModSpec(slug="MyMod", title="My Mod", factorio_version="2.0", mod_version="1.2.3"),
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import os
import re

# --- OpenAI SDK (официальный) ---
from openai import OpenAI
import API_KEYS

# -----------------------------
# PROMPTS
# -----------------------------
BASE_TRANSLATOR_PROMPT_RU = r"""
Ты — профессиональный переводчик локализаций модов Factorio (EN→RU).
Твоя задача: получить на вход один или несколько .cfg файлов локализации (как текст) и вернуть
ТОЛЬКО полный переведённый текст этих файлов, без каких-либо пояснений, без Markdown, без списков,
без цитат, без ссылок, без лишних символов.

ВАЖНОЕ О ФОРМАТЕ .cfg:
- Заголовки разделов вида [technology-name] / [item-name] / [entity-description] и т.п. НЕ переводить.
- Ключи слева от '=' НЕ переводить и не менять (даже регистр, пробелы вокруг ключа, порядок).
- Переводится только значение СПРАВА от '='.

КОММЕНТАРИИ:
- В строках вида key=value ;comment нужно переводить ТОЛЬКО часть value ДО первого символа ';'.
  Всё начиная с ';' (включая ';' и последующий текст) оставить как есть.
- Строки, начинающиеся с ';' или '#', а также пустые строки — оставить как есть (ничего не переводить).

СМЕШАННЫЙ RU+EN (после merge):
- Если значение уже по-русски (есть кириллица) — обычно оставляй как есть.
- Но если в таком значении встречаются английские фрагменты, аккуратно переведи эти фрагменты и
  сделай итоговую строку естественной по-русски, НЕ теряя смысл и НЕ удаляя пользовательские пометки.

ПЛЕЙСХОЛДЕРЫ / ТЕГИ / МАРКЕРЫ — НЕ ТРОГАТЬ:
- __1__, __2__, __3__ … (позиционные плейсхолдеры Factorio)
- %s, %d, %.2f и т.п.
- {0}, {name}, ${var} и т.п.
- [img=...], [color=...], [font=...], [item=...], [entity=...], [technology=...], [fluid=...], [gps=...]
- Любые другие конструкции в квадратных скобках, если они выглядят как теги/разметка.

ОДНОРОДНОСТЬ ТЕРМИНОВ (КРИТИЧЕСКИ ВАЖНО):
- В рамках одного мода один и тот же игровой термин (предмет/рецепт/технология/сущность/эффект)
  должен переводиться ОДИНАКОВО во всех файлах.
- Прежде чем писать итог, выбери единый вариант перевода для повторяющихся терминов (например
  “Looter Chest” → “Сундук мародёра”) и используй его везде дальше.
- Если в моде уже есть существующие русские строки — старайся подстроиться под их стиль и
  терминологию, чтобы не было разнобоя.

СТИЛЬ:
- [technology-name]/[item-name]/[entity-name]/[space-location-name] и т.п. — это названия:
  делай их короткими, “игровыми”, с разумной капитализацией.
- [technology-description]/[item-description]/[entity-description] и т.п. — это описания:
  делай их как нормальные фразы/предложения на русском.

КОНЕЧНЫЙ ВЫВОД:
- Верни только финальный текст .cfg (в той же структуре и порядке строк, как на входе).
- Не добавляй ничего кроме текста файлов.
""".strip()


MOD_CONTEXT_PROMPT_TEMPLATE_RU = r"""
Контекст для конкретного мода (используй web_search, если нужно для терминов/тематики):
- Искомый мод: "{title}".
- Slug (если задан): "{slug}".
- Автор (если задан): "{author}".
- Версия мода (если задана): "{mod_version}".
- Версия Factorio (если задана): "{factorio_version}".

ПРАВИЛО ПОИСКА:
- Если slug задан, считай, что страница мода: https://mods.factorio.com/mod/{slug}
- Если slug не задан, найди на mods.factorio.com точный мод по названию и (если есть) автору/версии,
  чтобы не перепутать с одноимёнными.
- Основной источник: mods.factorio.com (описание мода, скриншоты/гайды, список сущностей/технологий).
- Цель поиска — понять тематику и принятые названия (items/tech/entities) для точного перевода.

НЕ ВЫВОДИ результаты поиска, ссылки или цитаты — используй их только для улучшения перевода.
""".strip()


@dataclass
class ModSpec:
    """Метаданные мода (для уточнения 'нужной сборки')."""
    title: str
    slug: Optional[str] = None
    author: Optional[str] = None
    mod_version: Optional[str] = None
    factorio_version: Optional[str] = None


# -----------------------------
# CFG helpers
# -----------------------------
FILE_MARKER_RE = re.compile(r"^\s*;\s*===FILE:\s*(.+?)\s*===\s*$")

def _join_cfg_files(files: Dict[str, str]) -> str:
    """
    Склеивает несколько cfg в один текст с безопасными маркерами, чтобы потом разрезать обратно.
    Маркеры начинаются с ';' (комментарий), Factorio их игнорирует.
    """
    parts: List[str] = []
    for name, text in files.items():
        parts.append(f"; ===FILE: {name} ===")
        parts.append(text.rstrip("\n"))
        parts.append(f"; ===END FILE: {name} ===")
        parts.append("")  # пустая строка между файлами
    return "\n".join(parts).rstrip("\n") + "\n"


def _split_cfg_files(bundle_text: str) -> Dict[str, str]:
    """
    Разрезает склеенный текст обратно по маркерам ; ===FILE: name === ... ; ===END FILE: name ===
    """
    lines = bundle_text.splitlines()
    out: Dict[str, List[str]] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in lines:
        m = FILE_MARKER_RE.match(line)
        if m:
            # начало нового файла
            if current is not None:
                # незакрытый файл — сохраняем как есть
                out[current] = buf
            current = m.group(1)
            buf = []
            continue
        if current is not None and line.strip().startswith("; ===END FILE:"):
            out[current] = buf
            current = None
            buf = []
            continue
        if current is not None:
            buf.append(line)
    # если остался буфер
    if current is not None:
        out[current] = buf
    # собрать в тексты
    return {name: "\n".join(content).rstrip("\n") + "\n" for name, content in out.items()}


def _extract_cfg_keys(text: str) -> List[Tuple[str, str]]:
    """
    Очень лёгкая проверка: возвращает список (section, key) в порядке появления.
    Игнорирует комментарии и пустые строки.
    """
    section = ""
    keys: List[Tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key:
                keys.append((section, key))
    return keys


def _validate_same_keys(input_text: str, output_text: str) -> Tuple[bool, str]:
    """
    Проверяет, что модель не потеряла ключи/разделы. Не идеальная, но ловит большинство ошибок.
    """
    in_keys = _extract_cfg_keys(input_text)
    out_keys = _extract_cfg_keys(output_text)

    if len(out_keys) < len(in_keys):
        missing = [k for k in in_keys if k not in out_keys][:50]
        return False, f"Недостающие ключи (первые 50): {missing}"

    # порядок/количество могут отличаться из-за маркеров, но ключи должны присутствовать
    missing = [k for k in in_keys if k not in out_keys]
    if missing:
        return False, f"Недостающие ключи: {missing[:50]}"

    return True, "OK"


# -----------------------------
# OpenAI call
# -----------------------------
def build_mod_prompt(mod: ModSpec) -> str:
    return MOD_CONTEXT_PROMPT_TEMPLATE_RU.format(
        title=mod.title,
        slug=mod.slug or "",
        author=mod.author or "",
        mod_version=mod.mod_version or "",
        factorio_version=mod.factorio_version or "",
    )


def translate_cfg_bundle(
    cfg_bundle_text: str,
    mod: ModSpec,
    *,
    model: str = "gpt-5",
    reasoning_effort: str = "low",
    allowed_domains: Optional[List[str]] = None,
    temperature: Optional[float] = None,
    max_retries: int = 2,
) -> str:
    """
    Переводит склеенный текст (один или несколько .cfg) и возвращает склеенный перевод.

    Включаем web_search и (по умолчанию) ограничиваемся mods.factorio.com.
    """
    if allowed_domains is None:
        allowed_domains = ["mods.factorio.com"]

    client = OpenAI(
        api_key=API_KEYS.API_KEY_GEMINI)

    base = BASE_TRANSLATOR_PROMPT_RU
    mod_prompt = build_mod_prompt(mod)

    user_input = (
        base
        + "\n\n"
        + mod_prompt
        + "\n\n"
        + "===ВХОДНЫЕ CFG (переведи и верни целиком)===\n"
        + cfg_bundle_text
    )

    tools = [{
        "type": "web_search",
        "filters": {"allowed_domains": allowed_domains},
    }]

    last_err = None
    for attempt in range(max_retries + 1):
        kwargs = dict(
            model=model,
            reasoning={"effort": reasoning_effort},
            tools=tools,
            tool_choice="auto",
            input=user_input,
        )
        # Некоторые модели Responses API не поддерживают temperature.
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = client.responses.create(**kwargs)
        out_text = resp.output_text

        ok, msg = _validate_same_keys(cfg_bundle_text, out_text)
        if ok:
            return out_text

        last_err = msg
        # Жёсткий "ремонтный" промпт — просим вернуть снова, ничего не теряя
        user_input = (
            base
            + "\n\n"
            + mod_prompt
            + "\n\n"
            + "ВАЖНО: В прошлом ответе были ошибки структуры. "
              "Ты ОБЯЗАН вернуть все строки и все ключи.\n"
            + f"Проблема валидации: {msg}\n\n"
            + "===ВХОДНЫЕ CFG (верни тот же набор ключей и строк, только с переводом)===\n"
            + cfg_bundle_text
        )

    raise RuntimeError(f"Не удалось получить корректный перевод после ретраев. Последняя ошибка: {last_err}")


# -----------------------------
# High-level: translate files on disk
# -----------------------------
def load_cfg_files_from_dir(locale_dir: str) -> Dict[str, str]:
    """
    Загружает все .cfg из директории как {filename: text}.
    """
    files: Dict[str, str] = {}
    for name in sorted(os.listdir(locale_dir)):
        if name.lower().endswith(".cfg"):
            path = os.path.join(locale_dir, name)
            with open(path, "r", encoding="utf-8") as f:
                files[name] = f.read()
    return files


def write_cfg_files_to_dir(locale_dir: str, files: Dict[str, str]) -> None:
    os.makedirs(locale_dir, exist_ok=True)
    for name, text in files.items():
        path = os.path.join(locale_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")


def translate_mod_locales_inplace(
    *,
    mod_dir: str,
    mod: ModSpec,
    src_lang: str = "en",
    dst_lang: str = "ru",
    model: str = "gpt-5",
    reasoning_effort: str = "low",
    allowed_domains: Optional[List[str]] = None,
    max_chars_single_call: int = 140_000,
) -> None:
    """
    Переводит локализации мода 'на месте'.

    Ожидаем структуру как в вашем скрипте:
        <mod_dir>/<dst_lang>/*.cfg

    Важно: переводим именно dst_lang файлы ПОСЛЕ merge (там могут быть RU+EN строки),
    чтобы добить оставшийся EN и вернуть полный файл целиком.
    """
    dst_dir = os.path.join(mod_dir, dst_lang)
    if not os.path.isdir(dst_dir):
        raise FileNotFoundError(f"Нет директории перевода: {dst_dir}")

    files = load_cfg_files_from_dir(dst_dir)
    if not files:
        return

    bundle = _join_cfg_files(files)

    # Если слишком большой пакет — fallback на перевод по файлам с общим контекстом.
    if len(bundle) <= max_chars_single_call:
        translated_bundle = translate_cfg_bundle(
            bundle,
            mod,
            model=model,
            reasoning_effort=reasoning_effort,
            allowed_domains=allowed_domains,
        )
        translated_files = _split_cfg_files(translated_bundle)
        # если вдруг маркеры сломались — не пишем мусор
        if not translated_files or set(translated_files.keys()) != set(files.keys()):
            raise RuntimeError("Не удалось корректно разрезать ответ модели по файлам. Проверьте маркеры.")
        write_cfg_files_to_dir(dst_dir, translated_files)
        return

    # --- fallback: пофайлово, но с одним и тем же мод-подсказом ---
    for name, text in files.items():
        single_bundle = _join_cfg_files({name: text})
        translated_bundle = translate_cfg_bundle(
            single_bundle,
            mod,
            model=model,
            reasoning_effort=reasoning_effort,
            allowed_domains=allowed_domains,
        )
        translated_files = _split_cfg_files(translated_bundle)
        if name not in translated_files:
            raise RuntimeError(f"Файл {name} не найден в ответе модели.")
        write_cfg_files_to_dir(dst_dir, {name: translated_files[name]})
