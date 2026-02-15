import os
import zipfile
import shutil
import re
import json
import urllib.request
import urllib.error

# -----------------------------
# Глобальные настройки языков
SRC_LANG = "en"  # язык источника
DST_LANG = "ru"  # язык перевода
# -----------------------------

MODS_DIR = "mods"  # папка с архивами
UNPACKED_DIR = os.path.join(MODS_DIR, "unpacked")


def extract_locales(zip_path):
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        file_list = zip_ref.namelist()

        if not file_list:
            return

        root_folder = file_list[0].split('/')[0]
        mod_name = os.path.splitext(os.path.basename(zip_path))[0]
        target_dir = os.path.join(UNPACKED_DIR, mod_name)

        os.makedirs(target_dir, exist_ok=True)

        found_src = False
        found_dst = False

        for file in file_list:
            if file.endswith("/"):
                continue

            is_src = f"{root_folder}/locale/{SRC_LANG}/" in file
            is_dst = f"{root_folder}/locale/{DST_LANG}/" in file

            if not (is_src or is_dst):
                continue

            if is_src:
                found_src = True
            if is_dst:
                found_dst = True

            relative_path = file.replace(f"{root_folder}/locale/", "")
            destination_path = os.path.join(target_dir, relative_path)
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)

            # SRC_LANG всегда перезаписываем
            if is_src:
                with zip_ref.open(file) as source, open(destination_path, "wb") as target:
                    target.write(source.read())

            # DST_LANG НЕ перезаписываем если уже существует
            elif is_dst:
                if not os.path.exists(destination_path):
                    with zip_ref.open(file) as source, open(destination_path, "wb") as target:
                        target.write(source.read())
                else:
                    print(f"[SKIP {DST_LANG.upper()}] Preserved existing file: {relative_path}")

        # Если DST_LANG отсутствовал в архиве — создаём из SRC_LANG
        src_dir = os.path.join(target_dir, SRC_LANG)
        dst_dir = os.path.join(target_dir, DST_LANG)
        if found_src and not found_dst:
            if os.path.exists(src_dir) and not os.path.exists(dst_dir):
                shutil.copytree(src_dir, dst_dir)
                print(f"[INFO] {DST_LANG.upper()} folder created from {SRC_LANG.upper()} in {mod_name}")

        if found_src:
            print(f"[OK] Extracted locales from {mod_name}")
        else:
            print(f"[SKIP] No {SRC_LANG.upper()} locale found in {mod_name}")


def parse_ru_lines(lines):
    """
    Парсит строки файла перевода (ru) с учётом возможности закомментированных заголовков разделов.
    Возвращает:
        ru_sections: dict{clean_section: list_of_original_lines} — все строки раздела (включая заголовок)
        ru_keys: dict{clean_section: {key: original_line}} — только строки с ключами (оригинальные)
    """
    ru_sections = {}
    ru_keys = {}
    current_section = None
    current_section_clean = None

    for line in lines:
        stripped = line.lstrip("; \t")
        if stripped.startswith("[") and stripped.endswith("]"):
            # Начало нового раздела (возможно, закомментированного)
            current_section = line  # оригинальная строка заголовка
            current_section_clean = stripped
            ru_sections.setdefault(current_section_clean, [])
            ru_keys.setdefault(current_section_clean, {})
            ru_sections[current_section_clean].append(line)
        elif current_section_clean:
            # Строка внутри текущего раздела
            ru_sections[current_section_clean].append(line)
            if "=" in line:
                line_stripped = line.lstrip("; \t")
                if "=" in line_stripped:
                    key = line_stripped.split("=", 1)[0].strip()
                    ru_keys[current_section_clean][key] = line
    return ru_sections, ru_keys


def merge_locale_files(src_path, dst_path):
    if not os.path.exists(dst_path):
        shutil.copy(src_path, dst_path)
        print(f"[COPIED] {os.path.basename(dst_path)} (did not exist)")
        return

    # Читаем исходный (en) файл
    with open(src_path, "r", encoding="utf-8") as f:
        src_lines = [l.rstrip("\n") for l in f]

    # Читаем перевод (ru) файл
    with open(dst_path, "r", encoding="utf-8") as f:
        dst_lines = [l.rstrip("\n") for l in f]

    # Парсим ru
    ru_sections, ru_keys = parse_ru_lines(dst_lines)

    # Множество разделов en (очищенные заголовки) и ключи en
    en_sections = set()
    en_keys = {}  # clean_section -> set of keys

    # Выходной список строк
    output_lines = []
    current_section_clean = None

    # Вспомогательная функция: добавить устаревшие строки раздела (которых нет в en_keys)
    def add_obsolete_section_lines(section_clean):
        if section_clean not in ru_sections:
            return
        obsolete = []
        for line in ru_sections[section_clean]:
            # Пропускаем сам заголовок (он уже выведен)
            stripped = line.lstrip("; \t")
            if stripped.startswith("[") and stripped.endswith("]"):
                continue

            if "=" in line:
                line_stripped = line.lstrip("; \t")
                if "=" in line_stripped:
                    key = line_stripped.split("=", 1)[0].strip()
                    if key not in en_keys.get(section_clean, set()):
                        # Устаревший ключ: комментируем, если ещё не закомментирован
                        if not line.startswith(";"):
                            line = "; " + line
                        obsolete.append(line)
                else:
                    obsolete.append(line)
            else:
                # Строка без '=' (пустая, комментарий) – оставляем как есть
                obsolete.append(line)

        if obsolete:
            output_lines.extend(obsolete)

    # Проходим по строкам en и строим выходной файл
    i = 0
    while i < len(src_lines):
        line = src_lines[i]
        stripped = line.lstrip("; \t")

        if stripped.startswith("[") and stripped.endswith("]"):
            # Заголовок раздела en
            current_section_clean = stripped
            en_sections.add(current_section_clean)
            en_keys.setdefault(current_section_clean, set())
            output_lines.append(line)  # выводим заголовок как есть (en)

        elif current_section_clean and "=" in line and not line.startswith(";"):
            # Ключ en
            key = line.split("=", 1)[0].strip()
            en_keys[current_section_clean].add(key)

            # Ищем перевод в ru
            if current_section_clean in ru_keys and key in ru_keys[current_section_clean]:
                ru_line = ru_keys[current_section_clean][key]
                # Если строка закомментирована – убираем комментарий
                if ru_line.startswith(";"):
                    ru_line = re.sub(r"^[;\s]+", "", ru_line)
                output_lines.append(ru_line)
            else:
                output_lines.append(line)  # оставляем en строку

        else:
            # Комментарий или пустая строка из en – просто копируем
            output_lines.append(line)

        # Проверяем, не заканчивается ли текущий раздел (следующая строка – новый раздел или конец файла)
        next_is_new_section = False
        if i + 1 < len(src_lines):
            next_line = src_lines[i + 1]
            next_stripped = next_line.lstrip("; \t")
            if next_stripped.startswith("[") and next_stripped.endswith("]"):
                next_is_new_section = True
        else:
            next_is_new_section = True  # конец файла

        if next_is_new_section and current_section_clean:
            # Добавляем устаревшие строки для текущего раздела
            add_obsolete_section_lines(current_section_clean)
            current_section_clean = None  # сбрасываем, чтобы не добавить повторно

        i += 1

    # После обработки всех en, добавляем разделы, которые есть только в ru (отсутствуют в en_sections)
    for section_clean, lines in ru_sections.items():
        if section_clean not in en_sections:
            output_lines.extend(lines)
            output_lines.append("")  # пустая строка для разделения

    # --- ВАЖНО: схлопываем подряд идущие пустые строки, чтобы они не множились при повторных merge ---
    def collapse_blank_runs(lines, max_consecutive=1):
        out = []
        run = 0
        for l in lines:
            if l == "":
                run += 1
                if run <= max_consecutive:
                    out.append(l)
            else:
                run = 0
                out.append(l)
        return out

    output_lines = collapse_blank_runs(output_lines, max_consecutive=1)

    # Удаляем лишние пустые строки в конце
    while output_lines and output_lines[-1] == "":
        output_lines.pop()

    # Записываем результат
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")

    print(f"[MERGED] {os.path.basename(dst_path)}")



def merge_locales_for_mod(mod_name):
    mod_dir = os.path.join(UNPACKED_DIR, mod_name)
    src_dir = os.path.join(mod_dir, SRC_LANG)
    dst_dir = os.path.join(mod_dir, DST_LANG)

    if not os.path.exists(src_dir):
        return

    os.makedirs(dst_dir, exist_ok=True)

    for file in os.listdir(src_dir):
        if file.endswith(".cfg"):
            src_path = os.path.join(src_dir, file)
            dst_path = os.path.join(dst_dir, file)
            merge_locale_files(src_path, dst_path)


def repack_mod(zip_path):
    mod_name = os.path.splitext(os.path.basename(zip_path))[0]
    unpacked_mod_dir = os.path.join(UNPACKED_DIR, mod_name)
    dst_dir = os.path.join(unpacked_mod_dir, DST_LANG)

    if not os.path.exists(dst_dir):
        print(f"[SKIP] No {DST_LANG.upper()} folder to repack in {mod_name}")
        return

    temp_zip_path = zip_path + ".temp"

    with zipfile.ZipFile(zip_path, 'r') as original_zip:
        file_list = original_zip.namelist()

        # --- FIX: ищем реальную корневую папку мода по наличию внутри нее папки locale/ ---
        roots = set()
        for name in file_list:
            if not name or name.endswith("/"):
                continue
            parts = name.split("/", 1)
            if len(parts) >= 2 and parts[0]:
                roots.add(parts[0])

        best_root = None
        best_score = -1
        for root in roots:
            prefix = f"{root}/locale/"
            score = sum(1 for n in file_list if n.startswith(prefix))
            if score > best_score:
                best_score = score
                best_root = root

        # Если ни в одной корневой папке нет locale/, пробуем вариант без корня (редко, но бывает)
        locale_at_top = any(n.startswith("locale/") for n in file_list)
        if best_score <= 0 and locale_at_top:
            best_root = ""  # locale лежит прямо в корне архива

        # Фолбэк на случай совсем нестандартного архива
        if best_root is None:
            best_root = (file_list[0].split("/")[0] if file_list else mod_name)
        # --- конец FIX ---

        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as new_zip:
            # Копируем всё, кроме старых файлов DST_LANG
            for item in original_zip.infolist():
                if f"locale/{DST_LANG}/" in item.filename:
                    continue
                new_zip.writestr(item, original_zip.read(item.filename))

            # Добавляем только обновлённый DST_LANG
            for root, _, files in os.walk(dst_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, unpacked_mod_dir)  # ru/...
                    prefix = f"{best_root}/" if best_root else ""
                    zip_internal_path = f"{prefix}locale/{rel_path.replace(os.sep, '/')}"
                    new_zip.write(full_path, zip_internal_path)

    os.replace(temp_zip_path, zip_path)
    print(f"[REPACKED {DST_LANG.upper()} ONLY] {mod_name}")



# -----------------------------
# Меню
# -----------------------------
def extract_all():
    os.makedirs(UNPACKED_DIR, exist_ok=True)
    for file in os.listdir(MODS_DIR):
        if file.endswith(".zip"):
            zip_path = os.path.join(MODS_DIR, file)
            extract_locales(zip_path)
    print("\n[EXTRACT DONE]")


def merge_all():
    if not os.path.exists(UNPACKED_DIR):
        print("[ERROR] No unpacked folder found. Run extract first.")
        return
    for mod_name in os.listdir(UNPACKED_DIR):
        merge_locales_for_mod(mod_name)
    print("\n[MERGE DONE]")


def repack_all():
    for file in os.listdir(MODS_DIR):
        if file.endswith(".zip"):
            repack_mod(os.path.join(MODS_DIR, file))
    print("\n[REPACK DONE]")


# -----------------------------
# Перевод через OpenAI (web_search)
# Требует файл factorio_gpt_translate.py рядом со скриптом
# -----------------------------
def translate_with_openai_menu():
    """
    Переводит RU-локали (после merge) через OpenAI API, добивая оставшиеся EN строки.
    Важно: сначала выполните пункт 2 (слияние), чтобы в ru/*.cfg были все ключи.
    """
    if not os.path.exists(UNPACKED_DIR):
        print("[ОШИБКА] Нет распакованных модов. Сначала выполните извлечение.")
        return

    mods = [d for d in os.listdir(UNPACKED_DIR)
            if os.path.isdir(os.path.join(UNPACKED_DIR, d))]

    if not mods:
        print("[ИНФО] Нет модов для обработки.")
        return

    print("\n--- Выбор мода для перевода через OpenAI ---")
    print("0 - Назад")
    print("A - Перевести ВСЕ моды")
    for i, mod in enumerate(mods, 1):
        print(f"{i} - {mod}")

    choice = input("Ваш выбор: ").strip()

    # Импортируем здесь, чтобы основной скрипт работал без зависимости, если пункт не используется
    try:
        from factorio_gpt_translate import translate_mod_locales_inplace, ModSpec
    except ImportError:
        print("[ОШИБКА] Не найден factorio_gpt_translate.py. Положите его рядом с main2.py")
        print("        (или установите как модуль Python).")
        return

    def translate_one(mod_name: str):
        mod_dir = os.path.join(UNPACKED_DIR, mod_name)
        ru_dir = os.path.join(mod_dir, DST_LANG)
        if not os.path.exists(ru_dir):
            print(f"[ПРОПУЩЕНО] Нет папки {DST_LANG.upper()} у {mod_name}. Сначала сделайте пункт 2 (слияние).")
            return

        print(f"\n--- {mod_name} ---")
        # Если знаете slug на mods.factorio.com — укажите для точного поиска.
        # Обычно slug совпадает с именем папки/архива, но не всегда.
        slug = input("Slug мода на mods.factorio.com (Enter = оставить пустым): ").strip() or None
        author = input("Автор (Enter = пусто): ").strip() or None
        mod_version = input("Версия мода (Enter = пусто): ").strip() or None
        factorio_version = input("Версия Factorio (Enter = пусто): ").strip() or None

        spec = ModSpec(
            title=mod_name,
            slug=slug,
            author=author,
            mod_version=mod_version,
            factorio_version=factorio_version,
        )

        try:
            translate_mod_locales_inplace(
                mod_dir=mod_dir,
                mod=spec,
                src_lang=SRC_LANG,
                dst_lang=DST_LANG,
            )
            print(f"[OK] Перевод через OpenAI завершён: {mod_name}")
        except Exception as e:
            print(f"[ОШИБКА] {mod_name}: {e}")

    if choice.lower() == "a":
        for mod_name in mods:
            translate_one(mod_name)
        print("\n[OPENAI TRANSLATE DONE]")

    elif choice == "0":
        return

    else:
        try:
            mod_index = int(choice) - 1
            if mod_index < 0 or mod_index >= len(mods):
                print("Неверный номер.")
                return
            translate_one(mods[mod_index])
        except ValueError:
            print("Неверный ввод.")
            return

# -----------------------------
# Добавление заголовков в ru файлы
# -----------------------------
def add_headers_to_ru():
    if not os.path.exists(UNPACKED_DIR):
        print("[ОШИБКА] Нет распакованных модов. Сначала выполните извлечение.")
        return

    mods = [d for d in os.listdir(UNPACKED_DIR)
            if os.path.isdir(os.path.join(UNPACKED_DIR, d))]

    if not mods:
        print("[ИНФО] Нет модов для обработки.")
        return

    for mod_name in mods:
        ru_dir = os.path.join(UNPACKED_DIR, mod_name, DST_LANG)
        if not os.path.exists(ru_dir):
            print(f"[ПРОПУЩЕНО] Нет папки {DST_LANG.upper()} в {mod_name}")
            continue

        cfg_files = [f for f in os.listdir(ru_dir) if f.endswith(".cfg")]
        if not cfg_files:
            print(f"[ПРОПУЩЕНО] Нет .cfg файлов в {mod_name}/{DST_LANG}")
            continue

        for file in cfg_files:
            file_path = os.path.join(ru_dir, file)
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            header_lines = [f"# {mod_name}\n", f"# {file}\n", "\n"]
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(header_lines + lines)

            print(f"[HEADER ADDED] {mod_name}/{file}")

    print("\n[ГОТОВО] Все заголовки добавлены в файлы ru.")

def strip_version_suffix(mod_name: str) -> str:
    """
    Убирает суффикс _1.2.3 если имя папки мода вида Name_1.2.3
    """
    m = re.match(r"^(.*)_(\d+(?:\.\d+)+)$", mod_name)
    return m.group(1) if m else mod_name


def url_exists(url: str, timeout: float = 6.0) -> bool:
    """
    Проверяем существование URL по HTTP статусу.
    200..399 => True, 404 => False, другое => False (или ошибка сети => False)
    """
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            return 200 <= code < 400
    except urllib.error.HTTPError as e:
        # 404 — точно нет. Остальные — считаем "не найдено" (можно расширить при желании)
        return False
    except Exception:
        # сеть/таймаут/ssl — просто не смогли проверить
        return False


def resolve_mod_slug(mod_name: str) -> str | None:
    """
    Пытается подобрать slug для https://mods.factorio.com/mod/<slug>

    Логика:
    - берём base = имя без _версии
    - пробуем base как есть
    - пробуем заменить '_' <-> '-'
    - если ничего не найдено — None (тогда slug не указываем)
    """
    base = strip_version_suffix(mod_name)

    candidates = []
    candidates.append(base)

    # вариации '_' и '-'
    if "_" in base:
        candidates.append(base.replace("_", "-"))
    if "-" in base:
        candidates.append(base.replace("-", "_"))

    # убираем дубли
    seen = set()
    uniq = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)

    for slug in uniq:
        url = f"https://mods.factorio.com/mod/{slug}"
        if url_exists(url):
            return slug

    return None


def translate_with_gemini_menu():
    """
    Переводит RU-локали (после merge) через Gemini API.

    В режиме "A - все моды":
    - пропускает уже переведённые (по translated_mods_log.jsonl)
    - если ловит 429 RESOURCE_EXHAUSTED / quota exceeded:
        ждёт 70 секунд и повторяет ТЕКУЩИЙ мод (не идёт дальше),
        максимум QUOTA_RETRIES раз для одного мода.
    """
    import json
    import time

    QUOTA_WAIT_SEC = 70
    QUOTA_RETRIES = 10  # сколько раз повторять один мод при 429

    def is_quota_error(exc: Exception) -> bool:
        s = str(exc)
        s_low = s.lower()
        # покрываем и текст SDK, и твой лог-формат
        return (
            "429" in s
            or "resource_exhausted" in s_low
            or "quota exceeded" in s_low
            or "generate_content_free_tier_requests" in s_low
        )

    if not os.path.exists(UNPACKED_DIR):
        print("[ОШИБКА] Нет распакованных модов. Сначала выполните извлечение.")
        return

    mods = [d for d in os.listdir(UNPACKED_DIR)
            if os.path.isdir(os.path.join(UNPACKED_DIR, d))]

    if not mods:
        print("[ИНФО] Нет модов для обработки.")
        return

    # --- читаем лог переведённых модов ---
    log_path = os.path.join(MODS_DIR, "translated_mods_log.jsonl")
    already = set()
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    name = obj.get("mod_dir")
                    if name:
                        already.add(str(name))
                except Exception:
                    continue

    print("\n--- Выбор мода для перевода через Gemini ---")
    print("0 - Назад")
    print("A - Перевести ВСЕ моды (пропустит уже переведённые)")
    for i, mod in enumerate(mods, 1):
        mark = " (уже переводился)" if mod in already else ""
        print(f"{i} - {mod}{mark}")

    choice = input("Ваш выбор: ").strip()

    try:
        from factorio_gemini_translate import translate_mod_locales_inplace, ModSpec
    except ImportError:
        print("[ОШИБКА] Не найден factorio_gemini_translate.py. Положите его рядом с main2.py")
        return

    GEMINI_MODEL = "gemini-2.5-flash"

    def translate_one(mod_name: str, non_interactive: bool) -> bool:
        mod_dir = os.path.join(UNPACKED_DIR, mod_name)
        ru_dir = os.path.join(mod_dir, DST_LANG)
        if not os.path.exists(ru_dir):
            print(f"[ПРОПУЩЕНО] Нет папки {DST_LANG.upper()} у {mod_name}. (Возможно, нет локализаций.)")
            return False

        # --- auto-slug (если у тебя есть resolve_mod_slug) ---
        slug = None
        try:
            slug = resolve_mod_slug(mod_name)
        except Exception:
            slug = None

        if slug:
            print(f"[INFO] Auto-slug: {slug}")
        else:
            print("[INFO] Auto-slug не найден, продолжаю без slug.")

        author = None
        mod_version = None
        factorio_version = None

        if not non_interactive:
            manual = input("Slug (Enter = оставить auto/пусто): ").strip()
            if manual:
                slug = manual
            author = input("Автор (Enter = пусто): ").strip() or None
            mod_version = input("Версия мода (Enter = пусто): ").strip() or None
            factorio_version = input("Версия Factorio (Enter = пусто): ").strip() or None

        spec = ModSpec(
            title=mod_name,
            slug=slug,
            author=author,
            mod_version=mod_version,
            factorio_version=factorio_version,
        )

        translate_mod_locales_inplace(
            mod_dir=mod_dir,
            mod=spec,
            dst_lang=DST_LANG,
            model=GEMINI_MODEL,
            debug=True,
            debug_dir=os.path.join(mod_dir, "_debug_gemini"),
        )
        return True

    if choice.lower() == "a":
        skipped = 0
        done = 0
        failed = 0

        for mod_name in mods:
            if mod_name in already:
                print(f"[SKIP] Уже переводился: {mod_name}")
                skipped += 1
                continue

            print(f"\n--- {mod_name} ---")

            # Пытаемся перевести мод; при 429 ждём 70 сек и повторяем этот же мод
            attempt = 0
            while True:
                try:
                    ok = translate_one(mod_name, non_interactive=True)
                    if ok:
                        print(f"[OK] Перевод через Gemini завершён: {mod_name}")
                        done += 1
                    else:
                        failed += 1
                    break  # выходим из while по этому мод-нейму

                except Exception as e:
                    if is_quota_error(e):
                        attempt += 1
                        if attempt > QUOTA_RETRIES:
                            print(f"[ОШИБКА] {mod_name}: квота/лимит не отпустили после {QUOTA_RETRIES} ожиданий. Пропускаю.")
                            failed += 1
                            break

                        print(f"[QUOTA] {mod_name}: лимит/квота. Жду {QUOTA_WAIT_SEC} секунд и пробую снова (попытка {attempt}/{QUOTA_RETRIES})...")
                        time.sleep(QUOTA_WAIT_SEC)
                        continue  # повторяем тот же мод
                    else:
                        print(f"[ОШИБКА] {mod_name}: {e}")
                        failed += 1
                        break

        print(f"\n[GEMINI TRANSLATE DONE] Успешно: {done}, Пропущено: {skipped}, Ошибок: {failed}")
        return

    if choice == "0":
        return

    # Один мод (по номеру)
    try:
        mod_index = int(choice) - 1
        if mod_index < 0 or mod_index >= len(mods):
            print("Неверный номер.")
            return

        mod_name = mods[mod_index]
        if mod_name in already:
            print(f"[INFO] Этот мод уже есть в базе: {mod_name}")
            print("Если хочешь перевести заново — удали его запись из translated_mods_log.jsonl.")
            return

        print(f"\n--- {mod_name} ---")

        attempt = 0
        while True:
            try:
                ok = translate_one(mod_name, non_interactive=False)
                if ok:
                    print(f"[OK] Перевод через Gemini завершён: {mod_name}")
                break
            except Exception as e:
                if is_quota_error(e):
                    attempt += 1
                    if attempt > QUOTA_RETRIES:
                        print(f"[ОШИБКА] {mod_name}: квота/лимит не отпустили после {QUOTA_RETRIES} ожиданий.")
                        break
                    print(f"[QUOTA] {mod_name}: лимит/квота. Жду {QUOTA_WAIT_SEC} секунд и пробую снова (попытка {attempt}/{QUOTA_RETRIES})...")
                    time.sleep(QUOTA_WAIT_SEC)
                    continue
                else:
                    print(f"[ОШИБКА] {mod_name}: {e}")
                    break

    except ValueError:
        print("Неверный ввод.")




# -----------------------------
# Главное меню (на русском)
# -----------------------------
def main():
    while True:
        print("\n===== Инструмент перевода модов Factorio =====")
        print(f"Язык источника: {SRC_LANG.upper()}, Язык перевода: {DST_LANG.upper()}")
        print("1 - Извлечь локализации из архивов")
        print("2 - Слить SRC → DST")
        print("3 - Добавить заголовки к файлам ru")
        print("4 - Интерактивный перевод (Google + редактирование)")
        print("5 - Перевести мод через OpenAI (web_search)")
        print("6 - Перевести мод через Gemini (Google Search)")
        print("0 - Упаковать DST обратно в архивы")
        print("9 - Выход")

        choice = input("Выберите пункт: ").strip()

        if choice == "1":
            extract_all()

        elif choice == "2":
            merge_all()

        elif choice == "3":
            add_headers_to_ru()

        elif choice == "4":
            try:
                from interactive_translate import select_mod_menu
                select_mod_menu()
            except ImportError:
                print("[ОШИБКА] Файл interactive_translate.py не найден.")

        elif choice == "5":
            translate_with_openai_menu()

        elif choice == "6":
            translate_with_gemini_menu()

        elif choice == "0":
            repack_all()

        elif choice == "9":
            print("Выход из программы. До свидания!")
            break

        else:
            print("Неверный пункт меню. Попробуйте снова.")


# -----------------------------
if __name__ == "__main__":
    main()