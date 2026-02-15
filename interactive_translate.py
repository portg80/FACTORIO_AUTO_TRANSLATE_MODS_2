# interactive_translate.py
import os
import re
import shutil

import requests
from typing import Dict, List, Tuple, Optional

# ---------- Google Translate API (без ключа) ----------
def translate_text(text: str, src: str = "en", dst: str = "ru") -> Optional[str]:
    """
    Перевод текста через неофициальный Google Translate API.
    """
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": src,
        "tl": dst,
        "dt": "t",
        "q": text,
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            proxies={"http": None, "https": None},
            timeout=10
        )
        if response.status_code != 200:
            print(f"[ERROR] HTTP {response.status_code}")
            return None
        result = response.json()
        translated = "".join(item[0] for item in result[0])
        return translated
    except Exception as e:
        print(f"Translation error: {e}")
        return None

# ---------- Парсинг файлов перевода (адаптировано из основного модуля) ----------
def parse_ru_lines(lines: List[str]) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, str]]]:
    """
    Парсит строки файла перевода (ru) с учётом закомментированных заголовков.
    Возвращает:
        ru_sections: {clean_section: list_of_original_lines}
        ru_keys: {clean_section: {key: original_line}}
    """
    ru_sections = {}
    ru_keys = {}
    current_section = None
    current_section_clean = None

    for line in lines:
        stripped = line.lstrip("; \t")
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = line
            current_section_clean = stripped
            ru_sections.setdefault(current_section_clean, [])
            ru_keys.setdefault(current_section_clean, {})
            ru_sections[current_section_clean].append(line)
        elif current_section_clean:
            ru_sections[current_section_clean].append(line)
            if "=" in line:
                line_stripped = line.lstrip("; \t")
                if "=" in line_stripped:
                    key = line_stripped.split("=", 1)[0].strip()
                    ru_keys[current_section_clean][key] = line
    return ru_sections, ru_keys

def merge_locale_files(src_path: str, dst_path: str, src_lang: str = "en", dst_lang: str = "ru") -> None:
    """
    Полностью идентична функции из основного модуля, но принимает языки как аргументы.
    """
    if not os.path.exists(dst_path):
        shutil.copy(src_path, dst_path)
        print(f"[COPIED] {os.path.basename(dst_path)} (did not exist)")
        return

    with open(src_path, "r", encoding="utf-8") as f:
        src_lines = [l.rstrip("\n") for l in f]

    with open(dst_path, "r", encoding="utf-8") as f:
        dst_lines = [l.rstrip("\n") for l in f]

    ru_sections, ru_keys = parse_ru_lines(dst_lines)

    en_sections = set()
    en_keys = {}

    output_lines = []
    current_section_clean = None

    def add_obsolete_section_lines(section_clean):
        if section_clean not in ru_sections:
            return
        obsolete = []
        for line in ru_sections[section_clean]:
            stripped = line.lstrip("; \t")
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            if "=" in line:
                line_stripped = line.lstrip("; \t")
                if "=" in line_stripped:
                    key = line_stripped.split("=", 1)[0].strip()
                    if key not in en_keys.get(section_clean, set()):
                        if not line.startswith(";"):
                            line = "; " + line
                        obsolete.append(line)
                else:
                    obsolete.append(line)
            else:
                obsolete.append(line)
        if obsolete:
            output_lines.extend(obsolete)

    i = 0
    while i < len(src_lines):
        line = src_lines[i]
        stripped = line.lstrip("; \t")
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section_clean = stripped
            en_sections.add(current_section_clean)
            en_keys.setdefault(current_section_clean, set())
            output_lines.append(line)
        elif current_section_clean and "=" in line and not line.startswith(";"):
            key = line.split("=", 1)[0].strip()
            en_keys[current_section_clean].add(key)

            if current_section_clean in ru_keys and key in ru_keys[current_section_clean]:
                ru_line = ru_keys[current_section_clean][key]
                if ru_line.startswith(";"):
                    ru_line = re.sub(r"^[;\s]+", "", ru_line)
                output_lines.append(ru_line)
            else:
                output_lines.append(line)
        else:
            output_lines.append(line)

        next_is_new_section = False
        if i + 1 < len(src_lines):
            next_line = src_lines[i + 1]
            next_stripped = next_line.lstrip("; \t")
            if next_stripped.startswith("[") and next_stripped.endswith("]"):
                next_is_new_section = True
        else:
            next_is_new_section = True

        if next_is_new_section and current_section_clean:
            add_obsolete_section_lines(current_section_clean)
            current_section_clean = None

        i += 1

    for section_clean, lines in ru_sections.items():
        if section_clean not in en_sections:
            output_lines.extend(lines)
            output_lines.append("")

    while output_lines and output_lines[-1] == "":
        output_lines.pop()

    with open(dst_path, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")

    print(f"[MERGED] {os.path.basename(dst_path)}")

# ---------- Функция для обновления ru-файла новыми переводами ----------
def apply_updates_to_ru(ru_lines: List[str], updates: Dict[str, Dict[str, str]]) -> List[str]:
    """
    Применяет обновления переводов к списку строк ru-файла.
    updates: {section_clean: {key: new_text}}
    Возвращает новый список строк.
    """
    # Сначала парсим ru, чтобы знать, где какие строки находятся
    ru_sections, ru_keys = parse_ru_lines(ru_lines)
    # Создадим копию ru_sections, чтобы модифицировать
    new_ru_sections = {sec: lines.copy() for sec, lines in ru_sections.items()}

    for section_clean, key_dict in updates.items():
        if section_clean not in new_ru_sections:
            # Если раздела нет в ru, создаём новый раздел (с незакомментированным заголовком)
            new_ru_sections[section_clean] = [f"[{section_clean[1:-1]}]"]  # убираем скобки? нет, section_clean уже включает скобки
        # Ищем строку с данным ключом в разделе
        found = False
        for idx, line in enumerate(new_ru_sections[section_clean]):
            stripped = line.lstrip("; \t")
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key_dict.keys():  # но key_dict может содержать несколько ключей, поэтому нужно обрабатывать по одному
                    # Это упрощение: в updates приходит по одному ключу за раз, но мы будем вызывать эту функцию один раз со всеми обновлениями
                    pass
        # Для простоты реализуем построчную замену во втором проходе
        # Лучше: преобразуем new_ru_sections в список строк и будем заменять по ключам
        pass

    # Упрощённый подход: не пытаемся модифицировать структуру, а просто создаём новый файл на основе en и словаря обновлений.
    # Но для этого нам нужен en-файл, которого здесь нет. Значит, этот подход не подходит.
    # Придётся реализовать замену строк в существующем списке.

    # Реализуем:
    # 1. Создадим словарь позиций: для каждого раздела и ключа индекс строки в new_ru_sections[section]
    positions = {}
    for section_clean, lines in ru_sections.items():
        for idx, line in enumerate(lines):
            stripped = line.lstrip("; \t")
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                positions[(section_clean, key)] = (section_clean, idx)

    # 2. Для каждого обновления
    for section_clean, key_dict in updates.items():
        for key, new_text in key_dict.items():
            if (section_clean, key) in positions:
                # Заменяем существующую строку
                sec, idx = positions[(section_clean, key)]
                old_line = new_ru_sections[sec][idx]
                # Сохраняем возможный комментарий в начале? Но если ключ есть в en, он не должен быть закомментирован
                # Поэтому заменяем на "key=new_text"
                new_ru_sections[sec][idx] = f"{key}={new_text}"
            else:
                # Ключа нет в ru, нужно добавить строку в конец раздела
                # Сначала убедимся, что раздел существует
                if section_clean not in new_ru_sections:
                    new_ru_sections[section_clean] = [f"[{section_clean[1:-1]}]"]
                # Добавляем строку в конец раздела (перед возможными пустыми строками, но мы просто добавим)
                new_ru_sections[section_clean].append(f"{key}={new_text}")

    # Собираем все строки обратно в список
    result = []
    for section_clean, lines in new_ru_sections.items():
        result.extend(lines)
        result.append("")  # разделитель между разделами
    # Убираем последнюю пустую
    if result and result[-1] == "":
        result.pop()
    return result

# Более простая альтернатива: после сбора изменений просто запускаем merge, но перед этим записываем изменения в файл.
# Для этого нам нужно уметь обновлять файл на месте, что мы и сделаем через временную структуру.

def update_ru_file(ru_path: str, updates: Dict[str, Dict[str, str]]) -> None:
    """
    Обновляет файл ru, заменяя строки для указанных ключей.
    updates: {section_clean: {key: new_text}}
    """
    with open(ru_path, "r", encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]

    new_lines = apply_updates_to_ru(lines, updates)

    with open(ru_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

# ---------- Интерактивный перевод ----------
def select_mod_menu(mods_dir: str, unpacked_dir: str, src_lang: str, dst_lang: str):
    """
    Меню выбора мода и файла, запуск интерактивного перевода.
    """
    if not os.path.exists(unpacked_dir):
        print("[ERROR] No unpacked folder found. Run extract first.")
        return

    mods = [d for d in os.listdir(unpacked_dir) if os.path.isdir(os.path.join(unpacked_dir, d))]
    if not mods:
        print("[ERROR] No mods found in unpacked folder.")
        return

    print("\n--- Select mod ---")
    for i, mod in enumerate(mods, 1):
        print(f"{i} - {mod}")
    print("0 - Back")

    choice = input("Enter number: ").strip()
    if choice == "0":
        return
    try:
        mod_index = int(choice) - 1
        if mod_index < 0 or mod_index >= len(mods):
            print("Invalid number.")
            return
        mod_name = mods[mod_index]
    except ValueError:
        print("Invalid input.")
        return

    # Выбор файла .cfg из папки src_lang
    src_dir = os.path.join(unpacked_dir, mod_name, src_lang)
    if not os.path.exists(src_dir):
        print(f"[ERROR] No {src_lang.upper()} folder for this mod.")
        return

    files = [f for f in os.listdir(src_dir) if f.endswith(".cfg")]
    if not files:
        print("[ERROR] No .cfg files found.")
        return

    print(f"\n--- Select file in {mod_name} ---")
    for i, f in enumerate(files, 1):
        print(f"{i} - {f}")
    print("0 - Back")

    choice = input("Enter number: ").strip()
    if choice == "0":
        return
    try:
        file_index = int(choice) - 1
        if file_index < 0 or file_index >= len(files):
            print("Invalid number.")
            return
        filename = files[file_index]
    except ValueError:
        print("Invalid input.")
        return

    src_path = os.path.join(src_dir, filename)
    dst_path = os.path.join(unpacked_dir, mod_name, dst_lang, filename)

    # Запускаем интерактивный перевод для этого файла
    interactive_translate_file(src_path, dst_path, src_lang, dst_lang)


def interactive_translate_file(src_path: str, dst_path: str, src_lang: str, dst_lang: str):
    """
    Интерактивный перевод одного файла.
    """
    print(f"\n--- Translating {os.path.basename(src_path)} ---")

    # Читаем en-файл
    with open(src_path, "r", encoding="utf-8") as f:
        src_lines = [l.rstrip("\n") for l in f]

    # Если dst-файла нет, создаём копию en
    if not os.path.exists(dst_path):
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy(src_path, dst_path)
        print(f"[INFO] Created new {dst_lang.upper()} file from {src_lang.upper()}.")

    # Читаем текущий ru-файл для отображения существующих переводов
    with open(dst_path, "r", encoding="utf-8") as f:
        dst_lines = [l.rstrip("\n") for l in f]

    # Парсим ru для быстрого доступа к существующим переводам
    _, ru_keys = parse_ru_lines(dst_lines)

    # Словарь для сбора изменений: {section_clean: {key: new_text}}
    updates = {}

    # Проходим по строкам en и предлагаем перевод
    current_section = None
    total = 0
    translated = 0

    for line in src_lines:
        stripped = line.lstrip("; \t")
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            print(f"\n--- Section {current_section} ---")
        elif current_section and "=" in line and not line.startswith(";"):
            total += 1
            key = line.split("=", 1)[0].strip()
            en_text = line.split("=", 1)[1].strip()

            # Существующий перевод
            existing = None
            if current_section in ru_keys and key in ru_keys[current_section]:
                existing_line = ru_keys[current_section][key]
                if "=" in existing_line:
                    existing = existing_line.split("=", 1)[1].strip()
                # Если строка закомментирована, существующий перевод всё равно покажем
                if existing_line.startswith(";"):
                    existing = f"[COMMENTED] {existing}"

            print(f"\nKey: {key}")
            print(f"EN: {en_text}")
            if existing:
                print(f"Current {dst_lang.upper()}: {existing}")

            # Получаем машинный перевод
            mt = translate_text(en_text, src=src_lang, dst=dst_lang)
            if mt:
                print(f"MT: {mt}")
            else:
                print("MT: (translation failed)")

            # Запрашиваем действие
            while True:
                print("\nOptions:")
                print("  [Enter] - accept MT (or skip if no MT)")
                print("  type your translation")
                print("  s - skip (keep current)")
                print("  q - quit and save")
                inp = input("Your choice: ").strip()

                if inp == "":
                    # Enter: accept MT if exists, else skip
                    if mt:
                        new_text = mt
                        break
                    else:
                        print("No MT available, skipping.")
                        new_text = None
                        break
                elif inp.lower() == "s":
                    new_text = None
                    break
                elif inp.lower() == "q":
                    # Сохраняем и выходим
                    if updates:
                        # Обновляем ru-файл
                        update_ru_file(dst_path, updates)
                        # Запускаем merge для окончательной обработки
                        merge_locale_files(src_path, dst_path, src_lang, dst_lang)
                        print(f"[SAVED] Changes applied and merged.")
                    else:
                        print("No changes.")
                    return
                else:
                    # Считаем, что пользователь ввёл свой перевод
                    new_text = inp
                    break

            if new_text is not None:
                # Сохраняем изменение
                if current_section not in updates:
                    updates[current_section] = {}
                updates[current_section][key] = new_text
                translated += 1
                print(f"✓ Translation recorded")

    # Конец файла
    print(f"\n--- End of file ---")
    print(f"Total keys: {total}, Translated: {translated}")

    if updates:
        # Обновляем ru-файл
        update_ru_file(dst_path, updates)
        # Запускаем merge
        merge_locale_files(src_path, dst_path, src_lang, dst_lang)
        print("[SAVED] All changes applied and merged.")
    else:
        print("No changes.")

if __name__ == "__main__":
    pass
    #select_mod_menu()
