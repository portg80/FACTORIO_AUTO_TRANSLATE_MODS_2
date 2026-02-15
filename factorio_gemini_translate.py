"""
factorio_gemini_translate.py

Перевод локализаций модов Factorio (EN→RU) через Google Gemini Developer API
(библиотека google-genai) с инструментом Google Search (grounding).

Зачем:
- У вас OpenAI API упёрся в insufficient_quota. У Gemini есть бесплатный tier с лимитами
  (requests/day, requests/minute и т.п.) и отдельной квотой для "Grounding with Google Search".
  См. офиц. pricing/limits: https://ai.google.dev/gemini-api/docs/pricing и
  https://ai.google.dev/gemini-api/docs/rate-limits

Установка:
    pip install -U google-genai

Ключ:
- Рекомендуется через env: GEMINI_API_KEY или GOOGLE_API_KEY (GOOGLE_API_KEY имеет приоритет).
  Док: https://ai.google.dev/gemini-api/docs/api-key

Пример:
    from factorio_gemini_translate import translate_mod_locales_inplace, ModSpec
    translate_mod_locales_inplace(
        mod_dir="mods/unpacked/AsteroidBelt_1.2.10",
        mod=ModSpec(title="AsteroidBelt", slug="AsteroidBelt"),
        model="gemini-2.5-flash",
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import os
import re
import time

from google import genai
from google.genai import types
import API_KEYS

# --- Rate limit (requests per minute) ---
MAX_RPM = 5  # можешь менять
_MIN_INTERVAL_SEC = 60.0 / MAX_RPM
_last_request_ts = 0.0
API_KEY_GEMINI = API_KEYS.API_KEY_GEMINI
# -----------------------------
# PROMPTS (те же правила, что и для OpenAI)
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

ФАЙЛОВЫЕ МАРКЕРЫ BUNDLE (ОЧЕНЬ ВАЖНО):
- Во входном тексте есть строки-маркеры, начинающиеся с:
  "; ===FILE: "  и  "; ===END FILE: "
- Эти строки НЕЛЬЗЯ изменять ни на один символ: не переводить, не добавлять/убирать пробелы,
  не переносить, не удалять, не дублировать.
- Они нужны, чтобы потом разрезать ответ обратно на отдельные файлы.


КОММЕНТАРИИ:
- В строках вида key=value ;comment нужно переводить ТОЛЬКО часть value ДО первого символа ';'.
  Всё начиная с ';' (включая ';' и последующий текст) оставить как есть.
- Строки, начинающиеся с ';' или '#', а также пустые строки — оставить как есть (ничего не переводить).

СМЕШАННЫЙ RU+EN (после merge):
- Если значение уже по-русски (есть кириллица) — обычно оставляй как есть.
- Но если в таком значении встречаются английские фрагменты, аккуратно переведи эти фрагменты и
  сделай итоговую строку естественной по-русски, НЕ теряя смысл и НЕ удаляя пользовательские пометки.

ПЛЕЙСХОЛДЕРЫ / ТЕГИ / МАРКЕРЫ — НЕ ТРОГАТЬ:
- __1__, __2__, __3__ …
- %s, %d, %.2f и т.п.
- {0}, {name}, ${var} и т.п.
- [img=...], [color=...], [font=...], [item=...], [entity=...], [technology=...], [fluid=...], [gps=...]
- Любые другие конструкции в квадратных скобках, если они выглядят как теги/разметка.

ОДНОРОДНОСТЬ ТЕРМИНОВ (КРИТИЧЕСКИ ВАЖНО):
- В рамках одного мода один и тот же игровой термин должен переводиться ОДИНАКОВО.
- Если в моде уже есть существующие русские строки — подстраивайся под них.

СТИЛЬ:
- [*-name] — это названия: короткие, “игровые”.
- [*-description] — это описания: нормальные фразы/предложения на русском.

КОНЕЧНЫЙ ВЫВОД:
- Верни только финальный текст .cfg (в той же структуре и порядке строк, как на входе).
""".strip()


MOD_CONTEXT_PROMPT_TEMPLATE_RU = r"""
Контекст для конкретного мода (можешь использовать Google Search tool, если нужно для терминов/тематики):
- Искомый мод: "{title}"
- Slug (если задан): "{slug}"
- Автор (если задан): "{author}"
- Версия мода (если задана): "{mod_version}"
- Версия Factorio (если задана): "{factorio_version}"

ПРАВИЛО ПОИСКА:
- Если slug задан, считай, что страница мода: https://mods.factorio.com/mod/{slug}
- Если slug не задан, найди на mods.factorio.com точный мод по названию и (если есть) автору/версии.
- Основной источник: mods.factorio.com
- Цель поиска — понять тематику и принятые названия (items/tech/entities) для точного перевода.

НЕ ВЫВОДИ результаты поиска или ссылки — используй их только для улучшения перевода.
""".strip()


@dataclass
class ModSpec:
    title: str
    slug: Optional[str] = None
    author: Optional[str] = None
    mod_version: Optional[str] = None
    factorio_version: Optional[str] = None


# -----------------------------
# CFG helpers (как в OpenAI модуле)
# -----------------------------
FILE_MARKER_RE = re.compile(r"^\s*;\s*===FILE:\s*(.+?)\s*===\s*$")

def _join_cfg_files(files: Dict[str, str]) -> str:
    parts: List[str] = []
    for name, text in files.items():
        parts.append(f"; ===FILE: {name} ===")
        parts.append(text.rstrip("\n"))
        parts.append(f"; ===END FILE: {name} ===")
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def _split_cfg_files(bundle_text: str) -> Dict[str, str]:
    lines = bundle_text.splitlines()
    out: Dict[str, List[str]] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in lines:
        m = FILE_MARKER_RE.match(line)
        if m:
            if current is not None:
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
    if current is not None:
        out[current] = buf
    return {name: "\n".join(content).rstrip("\n") + "\n" for name, content in out.items()}


def _extract_cfg_keys(text: str) -> List[Tuple[str, str]]:
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
    in_keys = _extract_cfg_keys(input_text)
    out_keys = _extract_cfg_keys(output_text)
    if len(out_keys) < len(in_keys):
        missing = [k for k in in_keys if k not in out_keys][:50]
        return False, f"Недостающие ключи (первые 50): {missing}"
    missing = [k for k in in_keys if k not in out_keys]
    if missing:
        return False, f"Недостающие ключи: {missing[:50]}"
    return True, "OK"

def _strip_code_fences(text: str) -> str:
    """
    Убирает ```...``` если модель завернула ответ в code fence.
    Возвращает текст с финальным \n.
    """
    t = text.strip()
    if t.startswith("```"):
        # срезаем первую строку ``` или ```lang
        t = t.split("\n", 1)[1] if "\n" in t else ""
        # срезаем последнюю ```
        t2 = t.rstrip()
        if t2.endswith("```"):
            t2 = t2[:-3]
        t = t2
    return t.strip("\n") + "\n"


def _dump_debug(debug_dir: str, name: str, content: str) -> None:
    os.makedirs(debug_dir, exist_ok=True)
    path = os.path.join(debug_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _has_all_markers(bundle_text: str, expected_files: List[str]) -> bool:
    """
    Проверяем наличие точных маркеров для каждого файла.
    """
    for fn in expected_files:
        if f"; ===FILE: {fn} ===" not in bundle_text:
            return False
        if f"; ===END FILE: {fn} ===" not in bundle_text:
            return False
    return True



def build_mod_prompt(mod: ModSpec) -> str:
    return MOD_CONTEXT_PROMPT_TEMPLATE_RU.format(
        title=mod.title,
        slug=mod.slug or "",
        author=mod.author or "",
        mod_version=mod.mod_version or "",
        factorio_version=mod.factorio_version or "",
    )


# -----------------------------
# Gemini call
# -----------------------------
# --- Rate limit (requests per minute) ---
def _rate_limit_wait():
    """
    Гарантирует, что запросы к Gemini идут не чаще MAX_RPM в минуту.
    """
    global _last_request_ts, _MIN_INTERVAL_SEC
    now = time.time()
    elapsed = now - _last_request_ts
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_request_ts = time.time()

def translate_cfg_bundle(
    cfg_bundle_text: str,
    mod: ModSpec,
    *,
    model: str = "gemini-2.5-flash",
    temperature: Optional[float] = None,
    max_retries: int = 2,
    sleep_on_429_sec: float = 3.0,
    debug: bool = False,
    debug_dir: str = "debug_gemini",
) -> str:
    """
    Переводит склеенный CFG-текст и возвращает переведённый склеенный текст.

    Включаем инструмент Google Search (grounding), чтобы модель могла уточнять термины.
    Добавлена отладка: дамп входа/выхода, печать первых символов.
    Добавлен контроль маркеров FILE/END FILE и более жёсткий ретрай.
    """
    client = genai.Client(api_key=API_KEY_GEMINI)  # если хочешь ключ в коде: genai.Client(api_key="...")

    # Инструмент Google Search (grounding)
    tools = [types.Tool(google_search=types.GoogleSearch())]

    base_system = BASE_TRANSLATOR_PROMPT_RU + "\n\n" + build_mod_prompt(mod)

    last_err: Optional[str] = None

    # Для проверки маркеров — извлекаем ожидаемые имена из входного bundle
    expected_files: List[str] = []
    for line in cfg_bundle_text.splitlines():
        m = FILE_MARKER_RE.match(line)
        if m:
            expected_files.append(m.group(1))

    for attempt in range(max_retries + 1):
        try:
            cfg = types.GenerateContentConfig(
                system_instruction=base_system,
                tools=tools,
            )
            if temperature is not None:
                # temperature может поддерживаться/не поддерживаться, но обычно ок
                cfg.temperature = temperature  # type: ignore[attr-defined]

            _rate_limit_wait() # ждем чтобы было N запросов в минуту
            resp = client.models.generate_content(
                model=model,
                contents="===ВХОДНЫЕ CFG (переведи и верни целиком)===\n" + cfg_bundle_text,
                config=cfg,
            )

            raw = resp.text or ""
            out_text = _strip_code_fences(raw)

            if debug:
                _dump_debug(debug_dir, "00_input_bundle.cfg", cfg_bundle_text)
                _dump_debug(debug_dir, f"01_output_raw_attempt{attempt}.txt", raw)
                _dump_debug(debug_dir, f"02_output_clean_attempt{attempt}.cfg", out_text)

                print("\n[DEBUG] Gemini RAW (первые 2000 символов):")
                print(raw[:2000])
                print("\n[DEBUG] Gemini CLEAN (первые 2000 символов):")
                print(out_text[:2000])

            # 1) Проверка ключей
            ok, msg = _validate_same_keys(cfg_bundle_text, out_text)
            if not ok:
                last_err = msg
                # усиливаем системную инструкцию
                base_system = (
                    BASE_TRANSLATOR_PROMPT_RU + "\n\n" + build_mod_prompt(mod) + "\n\n"
                    "ВАЖНО: В прошлом ответе были ошибки структуры.\n"
                    "Ты ОБЯЗАН вернуть ВСЕ строки и ВСЕ ключи в том же порядке.\n"
                    "НЕ ИЗМЕНЯЙ маркеры '; ===FILE: ... ===' и '; ===END FILE: ... ===' ни на один символ.\n"
                    f"Проблема валидации ключей: {msg}"
                )
                continue

            # 2) Проверка маркеров (критично для разрезания на файлы)
            if expected_files and not _has_all_markers(out_text, expected_files):
                last_err = "Маркеры FILE/END FILE отсутствуют или изменены."
                base_system = (
                    BASE_TRANSLATOR_PROMPT_RU + "\n\n" + build_mod_prompt(mod) + "\n\n"
                    "КРИТИЧНО: Ты сломал файловые маркеры bundle.\n"
                    "В следующем ответе верни текст так, чтобы строки:\n"
                    "  '; ===FILE: <имя> ==='\n"
                    "  '; ===END FILE: <имя> ==='\n"
                    "остались ТОЧНО такими же, без изменений.\n"
                    "Не удаляй их и не переводить слова FILE/END.\n"
                    "Верни всё целиком, без ```.\n"
                )
                continue

            return out_text

        except Exception as e:
            last_err = str(e)
            if debug:
                _dump_debug(debug_dir, f"99_exception_attempt{attempt}.txt", last_err)

            # Если это 429 (лимиты) — можно подождать и повторить
            if ("429" in last_err or "RESOURCE_EXHAUSTED" in last_err) and attempt < max_retries:
                time.sleep(sleep_on_429_sec)
                continue
            raise

    raise RuntimeError(f"Не удалось получить корректный перевод после ретраев. Последняя ошибка: {last_err}")


# -----------------------------
# I/O helpers
# -----------------------------
def load_cfg_files_from_dir(locale_dir: str) -> Dict[str, str]:
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

def _translation_log_path(mod_dir: str) -> str:
    """
    Лог храним рядом с проектом: mods/translated_mods_log.jsonl
    mod_dir у вас вида mods/unpacked/<MODNAME>
    """
    # поднимаемся на уровень mods/
    mods_dir = os.path.abspath(os.path.join(mod_dir, "..", ".."))
    return os.path.join(mods_dir, "translated_mods_log.jsonl")


def _find_archive_for_mod(mod_dir: str, mod_name: str) -> str:
    """
    Ищем <mods>/<mod_name>.zip
    Возвращаем имя файла архива, если найден, иначе mod_name+".zip"
    """
    mods_dir = os.path.abspath(os.path.join(mod_dir, "..", ".."))
    want = mod_name + ".zip"
    try:
        for fn in os.listdir(mods_dir):
            if fn.lower().endswith(".zip") and os.path.splitext(fn)[0] == mod_name:
                return fn
    except Exception:
        pass
    return want


def append_translation_log(
    *,
    mod_dir: str,
    mod_name: str,
    mod: "ModSpec",
    model: str,
) -> None:
    """
    Пишем запись в JSONL лог. Вызывать ТОЛЬКО после успешного перевода.
    """
    import json
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        ts = datetime.now(ZoneInfo("Europe/Amsterdam")).isoformat(timespec="seconds")
    except Exception:
        ts = datetime.now().isoformat(timespec="seconds")

    entry = {
        "ts": ts,
        "archive": _find_archive_for_mod(mod_dir, mod_name),
        "mod_dir": mod_name,          # имя папки внутри mods/unpacked
        "title": mod.title,
        "slug": mod.slug,
        "author": mod.author,
        "mod_version": mod.mod_version,
        "factorio_version": mod.factorio_version,
        "model": model,
    }

    log_path = _translation_log_path(mod_dir)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def translate_mod_locales_inplace(
    *,
    mod_dir: str,
    mod: ModSpec,
    dst_lang: str = "ru",
    model: str = "gemini-2.5-flash",
    max_chars_single_call: int = 140_000,
    debug: bool = False,
    debug_dir: str = "debug_gemini",
    log_on_success: bool = True,
) -> None:
    """
    Переводит локализации мода 'на месте' в папке <mod_dir>/<dst_lang>/*.cfg.
    Предполагается, что вы уже сделали merge (RU+EN), и надо добить EN.

    Если log_on_success=True — при успешном завершении пишет запись в mods/translated_mods_log.jsonl
    """
    dst_dir = os.path.join(mod_dir, dst_lang)
    if not os.path.isdir(dst_dir):
        raise FileNotFoundError(f"Нет директории перевода: {dst_dir}")

    files = load_cfg_files_from_dir(dst_dir)
    if not files:
        return

    bundle = _join_cfg_files(files)
    expected_files = list(files.keys())

    if len(bundle) <= max_chars_single_call:
        translated_bundle = translate_cfg_bundle(
            bundle,
            mod,
            model=model,
            debug=debug,
            debug_dir=debug_dir,
        )

        if not _has_all_markers(translated_bundle, expected_files):
            raise RuntimeError("Модель вернула ответ без корректных FILE/END FILE маркеров. Проверь debug-дампы.")

        translated_files = _split_cfg_files(translated_bundle)
        if not translated_files or set(translated_files.keys()) != set(files.keys()):
            raise RuntimeError("Не удалось корректно разрезать ответ модели по файлам. Проверь debug-дампы.")

        write_cfg_files_to_dir(dst_dir, translated_files)

        # ✅ ЛОГ только если всё прошло успешно
        if log_on_success:
            mod_name = os.path.basename(mod_dir.rstrip("/\\"))
            append_translation_log(mod_dir=mod_dir, mod_name=mod_name, mod=mod, model=model)
        return

    # fallback: пофайлово (успех только если все файлы успешно переведены)
    for name, text in files.items():
        single_bundle = _join_cfg_files({name: text})
        translated_bundle = translate_cfg_bundle(
            single_bundle,
            mod,
            model=model,
            debug=debug,
            debug_dir=debug_dir,
        )

        if not _has_all_markers(translated_bundle, [name]):
            raise RuntimeError(f"Файл {name}: модель сломала FILE/END FILE маркеры. Проверь debug-дампы.")

        translated_files = _split_cfg_files(translated_bundle)
        if name not in translated_files:
            raise RuntimeError(f"Файл {name} не найден в ответе модели. Проверь debug-дампы.")

        write_cfg_files_to_dir(dst_dir, {name: translated_files[name]})

    # ✅ Все файлы успешно переведены — логируем 1 раз на мод
    if log_on_success:
        mod_name = os.path.basename(mod_dir.rstrip("/\\"))
        append_translation_log(mod_dir=mod_dir, mod_name=mod_name, mod=mod, model=model)

