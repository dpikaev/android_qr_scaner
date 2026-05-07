import os
import re

def get_bluestacks_instances():
    """Парсит bluestacks.conf без использования configparser и возвращает список (instance_name, adb_port)."""
    # Возможные пути к конфигу (можно добавить свои)
    candidates = [
        r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf",
        r"D:\BS\BlueStacks_nxt\bluestacks.conf",   # из вашей ошибки
        # поискать в ProgramData рекурсивно
    ]
    config_path = None
    for path in candidates:
        if os.path.exists(path):
            config_path = path
            break

    if not config_path:
        # рекурсивный поиск в C:\ProgramData (на случай, если имя папки отличается)
        for root, dirs, files in os.walk(r"C:\ProgramData"):
            if 'bluestacks.conf' in files:
                config_path = os.path.join(root, 'bluestacks.conf')
                break

    if not config_path:
        raise FileNotFoundError("Не удалось найти bluestacks.conf. Проверьте путь к BlueStacks.")

    instances = []
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Ищем строки: bst.instance.<название>.adb_port="5555"
            match = re.match(r'^bst\.instance\.(.+?)\.adb_port="(\d+)"', line)
            if match:
                instance_name = match.group(1)
                port = int(match.group(2))
                instances.append((instance_name, port))
    return instances


