import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import aio_pika
import cv2
import pytesseract
import logging
import re
import shlex
import xml.etree.ElementTree as ET
try:
    from .find_config import get_bluestacks_instances
except ImportError:
    from find_config import get_bluestacks_instances

from ppadb.client import Client as AdbClient

logger = logging.getLogger(__name__)
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


@dataclass
class PaymentScanTask:
    payment_link: str
    job_id: str

def extract_amount_from_text(
        text: str,
        allow_integer_without_marker: bool = False,
) -> str | None:
    cleaned_text = text.replace("\u00a0", " ").strip()
    if not cleaned_text:
        return None

    money_amount_match = re.search(
        r"(?<!\d)(\d{1,3}(?:\s\d{3})*|\d+)(?:[,.](\d{1,2}))?\s*(?:₽|руб\.?|р\.?|p\.?)(?![a-zа-яё])",
        cleaned_text,
        re.IGNORECASE,
    )
    if money_amount_match:
        amount_match = money_amount_match
    else:
        amount_match = re.search(
            r"(?<!\d)(\d{1,3}(?:\s\d{3})*|\d+)(?:[,.](\d{1,2}))?(?!\d)",
            cleaned_text,
        )

    has_money_marker = money_amount_match is not None
    if not amount_match:
        return None

    # Без валютного маркера берём только явные суммы с копейками, чтобы не спутать с другими числами.
    if not has_money_marker and amount_match.group(2) is None and not allow_integer_without_marker:
        return None

    rubles = amount_match.group(1).replace(" ", "")
    kopecks = amount_match.group(2)
    if kopecks is None:
        kopecks = "00"
    else:
        kopecks = kopecks.ljust(2, "0")

    return f"{rubles},{kopecks}"


def parse_payment_info(xml: str):
    match = re.search(r'<\?xml.*?</hierarchy>', xml, re.DOTALL)
    if not match:
        raise Exception("Ошибка парса XML")
    xml = match.group()
    root = ET.fromstring(xml)
    amount = None
    purpose = None

    # Поиск суммы из EditText элемента
    for i in root.iter():
        if i.attrib.get('class') == "android.widget.EditText":
            text = i.attrib.get('text', '').strip()
            amount = extract_amount_from_text(text)
            if amount:
                break

    if not amount:
        elements_text = []
        for elem in root.iter():
            texts = [
                elem.attrib.get('text', '').strip(),
                elem.attrib.get('content-desc', '').strip(),
            ]
            text = " ".join(item for item in texts if item)
            if text:
                elements_text.append(text)

        pay_button_index = None
        for index, text in enumerate(elements_text):
            if "оплатить" in text.lower():
                pay_button_index = index
                break

        # На экране банка сумма находится в текстовом блоке прямо над кнопкой оплаты.
        if pay_button_index is not None:
            for text in reversed(elements_text[:pay_button_index]):
                amount = extract_amount_from_text(text)
                if amount:
                    break

        if not amount:
            for text in elements_text:
                amount = extract_amount_from_text(text)
                if amount:
                    break

    # Поиск назначения
    for i in root.iter():
        text = i.attrib.get('text', '')
        # TODO: назначение если надо будет, сюда воткнуть можно

    if not amount:
        raise Exception("Сумма платежа не найдена")

    return amount


def wait_for_payment_amount(device, attempts: int = 6, delay: float = 2):
    last_error = None
    last_xml = None
    last_ocr_text = None
    serial = device.serial.replace(':', '_').replace('.', '_')
    screenshot_path = f"screen_amount_{serial}.png"
    for attempt in range(1, attempts + 1):
        ui_xml = device.shell('uiautomator dump /dev/tty').strip()
        last_xml = ui_xml
        try:
            return parse_payment_info(ui_xml)
        except Exception as e:
            last_error = e
            logger.warning("Сумма не найдена в XML, попытка %s/%s: %s", attempt, attempts, e)

        try:
            amount, ocr_text = find_amount_on_screenshot(device, screenshot_path)
            last_ocr_text = ocr_text
            if amount:
                logger.info("Сумма найдена через OCR со скриншота: %s", amount)
                logger.info("OCR текст экрана, где сумма найдена:\n%s", ocr_text)
                print("\n===== OCR ТЕКСТ ЭКРАНА, ГДЕ СУММА НАЙДЕНА =====")
                print(ocr_text)
                print("===== КОНЕЦ OCR ТЕКСТА =====\n")
                return amount
        except Exception as e:
            logger.warning("Сумма не найдена через OCR, попытка %s/%s: %s", attempt, attempts, e)

        if attempt < attempts:
            time.sleep(delay)

    logger.error("XML экрана, где сумма не найдена:\n%s", last_xml)
    logger.error("OCR текст экрана, где сумма не найдена:\n%s", last_ocr_text)
    print("\n===== XML ЭКРАНА, ГДЕ СУММА НЕ НАЙДЕНА =====")
    print(last_xml)
    print("===== КОНЕЦ XML ЭКРАНА =====\n")
    print("\n===== OCR ТЕКСТ ЭКРАНА, ГДЕ СУММА НЕ НАЙДЕНА =====")
    print(last_ocr_text)
    print("===== КОНЕЦ OCR ТЕКСТА =====\n")
    raise last_error

def tap_coordinates(device, x, y):
    device.shell(f"input tap {x} {y}")

def read_img_and_find_txt(device, text: str, screenshot_path: str):
    img = cv2.imread(screenshot_path)
    if img is None:
        raise Exception(f"Не удалось прочитать скриншот: {screenshot_path}")

    data = pytesseract.image_to_data(img, lang="rus", output_type=pytesseract.Output.DICT)
    for i, word in enumerate(data['text']):
        if text in word:
            x = data['left'][i] + data['width'][i] // 2
            y = data['top'][i] + data['height'][i] // 2
            tap_coordinates(device, x, y)


def normalize_ocr_text(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", text.lower())


def take_screenshot(device, screenshot_path: str):
    result = device.screencap()
    with open(screenshot_path, "wb") as f:
        f.write(result)


def find_amount_on_screenshot(device, screenshot_path: str) -> tuple[str | None, str]:
    take_screenshot(device, screenshot_path)
    img = cv2.imread(screenshot_path)
    if img is None:
        raise Exception(f"Не удалось прочитать скриншот: {screenshot_path}")

    data = pytesseract.image_to_data(img, lang="rus", output_type=pytesseract.Output.DICT)
    lines = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue

        line_key = (
            data["block_num"][i],
            data["par_num"][i],
            data["line_num"][i],
        )
        lines.setdefault(line_key, []).append({
            "text": word,
            "left": data["left"][i],
            "top": data["top"][i],
            "height": data["height"][i],
        })

    ordered_lines = []
    for words in lines.values():
        words.sort(key=lambda item: item["left"])
        line_text = " ".join(item["text"] for item in words)
        line_top = min(item["top"] for item in words)
        line_bottom = max(item["top"] + item["height"] for item in words)
        ordered_lines.append({
            "text": line_text,
            "top": line_top,
            "bottom": line_bottom,
        })

    ordered_lines.sort(key=lambda item: item["top"])
    ocr_lines = [line["text"] for line in ordered_lines]

    pay_button_line = None
    for line in ordered_lines:
        normalized_line = normalize_ocr_text(line["text"])
        if "оплат" in normalized_line:
            pay_button_line = line
            break

    amount_candidates = []
    search_lines = ordered_lines
    if pay_button_line:
        search_lines = [
            line
            for line in ordered_lines
            if 120 < line["top"] < pay_button_line["top"]
        ]

    for line in reversed(search_lines):
        amount = extract_amount_from_text(line["text"])
        if amount:
            return amount, "\n".join(ocr_lines)

        amount = extract_amount_from_text(line["text"], allow_integer_without_marker=bool(pay_button_line))
        if amount:
            rubles = int(amount.split(",", 1)[0])
            if rubles >= 10:
                distance_to_pay_button = pay_button_line["top"] - line["bottom"] if pay_button_line else 0
                amount_candidates.append((distance_to_pay_button, rubles, amount))

    if amount_candidates:
        amount_candidates.sort(key=lambda item: (item[0], -item[1]))
        return amount_candidates[0][2], "\n".join(ocr_lines)

    return None, "\n".join(ocr_lines)


def find_phrase_on_screenshot_and_click(device, phrase: str, screenshot_path: str) -> bool:
    take_screenshot(device, screenshot_path)
    img = cv2.imread(screenshot_path)
    if img is None:
        raise Exception(f"Не удалось прочитать скриншот: {screenshot_path}")

    data = pytesseract.image_to_data(img, lang="rus", output_type=pytesseract.Output.DICT)
    target_words = [normalize_ocr_text(word) for word in phrase.split()]
    target_words = [word for word in target_words if word]

    recognized_words = []
    for i, word in enumerate(data["text"]):
        normalized_word = normalize_ocr_text(word)
        if not normalized_word:
            continue

        recognized_words.append({
            "text": normalized_word,
            "left": data["left"][i],
            "top": data["top"][i],
            "width": data["width"][i],
            "height": data["height"][i],
        })

    for start_idx in range(len(recognized_words) - len(target_words) + 1):
        phrase_words = recognized_words[start_idx:start_idx + len(target_words)]
        if [word["text"] for word in phrase_words] != target_words:
            continue

        left = min(word["left"] for word in phrase_words)
        top = min(word["top"] for word in phrase_words)
        right = max(word["left"] + word["width"] for word in phrase_words)
        bottom = max(word["top"] + word["height"] for word in phrase_words)
        x = (left + right) // 2
        y = (top + bottom) // 2
        tap_coordinates(device, x, y)
        print(f"Клик по OCR-координатам {x} {y} для {phrase}")
        return True

    print(f"Фраза {phrase} не найдена на скриншоте {screenshot_path}")
    return False


def find_target_and_click(device, target_text: str) -> bool:
    """
    Парс дерева и поиск элемента с заданным текстом
    :param device:
    :param target_text:
    :return:
    """
    output = device.shell("uiautomator dump /dev/tty")
    match = re.search(r"<\?xml.*?</hierarchy>", output, re.DOTALL)
    print(f"Поиск XML в дереве UI")
    if not match:
        raise Exception("Не удалось найти XML в выводе uiautomator")
    xml_str = match.group()
    root = ET.fromstring(xml_str)

    # Парс дочерний элемент + родитель для подъема по дереву
    parent_map = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    # Ищем элемент по тексту
    target_element = None
    for elem in root.iter():
        if elem.attrib.get("text") == target_text:
            target_element = elem
            break

    if target_element is None:
        print(f"Элемент с текстом {target_text} не найден")
        return False

    # Поднимаемся по дереву пока не найдем кликабельный элемент
    clickable_elem = target_element
    while clickable_elem is not None:
        if clickable_elem.attrib.get('clickable') == "true":
            break
        clickable_elem = parent_map.get(clickable_elem)

    # Если не нашли в дереве кликабельный элемент - юзаем сам целевой
    if clickable_elem is None:
        clickable_elem = target_element

    # Ищем границы элемента
    bounds_str = clickable_elem.attrib["bounds"]
    coords = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if not coords:
        raise Exception(f"Неверный формат границ: {bounds_str}")

    # Координаты центра
    x1, y1, x2, y2 = map(int, coords.groups())
    center_x = (x1 + x2) // 2
    center_y = (y1 + y2) // 2

    # Клик
    device.shell(f"input tap {center_x} {center_y}")
    print(f'Клик по координатам {center_x} {center_y} для {target_text}')
    return True


def get_screen_size(device) -> tuple[int, int]:
    output = device.shell("wm size")
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    if not match:
        return 1080, 1920

    return int(match.group(1)), int(match.group(2))


def scroll_page_down(device):
    width, height = get_screen_size(device)
    x = width // 2
    start_y = int(height * 0.8)
    end_y = int(height * 0.25)
    device.shell(f"input touchscreen swipe {x} {start_y} {x} {end_y} 700")


def scroll_page_down_light(device):
    width, height = get_screen_size(device)
    x = width // 2
    start_y = int(height * 0.65)
    end_y = int(height * 0.45)
    device.shell(f"input touchscreen swipe {x} {start_y} {x} {end_y} 500")


def scroll_page_up(device):
    width, height = get_screen_size(device)
    x = width // 2
    start_y = int(height * 0.25)
    end_y = int(height * 0.8)
    device.shell(f"input touchscreen swipe {x} {start_y} {x} {end_y} 700")


def find_target_and_click_with_scroll(device, target_text: str, max_scrolls: int = 3) -> bool:
    """
    Ищет элемент на текущем экране и ниже по странице.
    Если элемент не найден, возвращает экран примерно в исходное положение.
    """
    serial = device.serial.replace(':', '_').replace('.', '_')
    screenshot_path = f"screen_browser_button_{serial}.png"

    scrolls_done = 0
    for _ in range(max_scrolls):
        scroll_page_down(device)
        scrolls_done += 1
        time.sleep(1.5)

        if find_phrase_on_screenshot_and_click(device, target_text, screenshot_path):
            return True

        if find_target_and_click(device, target_text):
            return True

    for _ in range(scrolls_done):
        scroll_page_up(device)
        time.sleep(0.7)

    return False


def find_target_and_click_with_light_scroll(device, target_text: str, screenshot_prefix: str) -> bool:
    serial = device.serial.replace(':', '_').replace('.', '_')
    screenshot_path = f"{screenshot_prefix}_{serial}.png"

    scroll_page_down_light(device)
    time.sleep(1)

    if find_phrase_on_screenshot_and_click(device, target_text, screenshot_path):
        return True

    if find_target_and_click(device, target_text):
        return True

    scroll_page_up(device)
    time.sleep(0.7)
    return False


def _process_single_device(device, payment_link):
    """
    Обрабатывает один эмулятор: открывает ссылку, выбирает СБП, Т-Банк, извлекает сумму.
    Возвращает словарь с результатом или выбрасывает исключение.
    """
    serial = device.serial.replace(':', '_').replace('.', '_')  # для имени файла
    logger.info(f"Начало обработки на {device.serial}")

    try:
        # Открываем ссылку
        device.shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(payment_link)}")
        time.sleep(3)  # начальное ожидание загрузки

        # 1. Клик по СБП
        ui_xml = device.shell('uiautomator dump /dev/tty').strip()
        find_target_and_click(device, "Система быстрых платежей")
        time.sleep(3)

        # 2. Делаем скриншот, если понадобится
        ui_xml_after_click = device.shell('uiautomator dump /dev/tty').strip()
        screenshot_path = f"screen_after_spb_{serial}.png"
        result = device.screencap()
        with open(screenshot_path, "wb") as f:
            f.write(result)
        logger.info(f"Скриншот сохранён: {screenshot_path}")

        # 3. Ищем и кликаем Т-Банк
        t_bank_clicked = find_target_and_click(device, "Т-Банк")
        if not t_bank_clicked:
            read_img_and_find_txt(device, "Т-Банк", screenshot_path)
            logger.info("Кликнули по Т-Банк через OCR")

        # 4. Проверяем промежуточную кнопку банка перед поиском суммы
        time.sleep(5)  # даём странице загрузиться
        browser_button_clicked = find_target_and_click_with_scroll(device, "Здесь, в браузере")
        if browser_button_clicked:
            logger.info("Кликнули по кнопке 'Здесь, в браузере'")
            time.sleep(3)

        # 5. Если банк предлагает привязать счёт, пропускаем этот шаг
        skip_account_link_clicked = find_target_and_click_with_light_scroll(
            device,
            "Не нужно",
            "screen_skip_account_link",
        )
        if skip_account_link_clicked:
            logger.info("Кликнули по кнопке 'Не нужно'")
            time.sleep(3)

        # 6. Получаем XML экрана с суммой
        amount = wait_for_payment_amount(device)

        return {
            "device": device.serial,
            "status": "ok",
            "amount": amount,
            "screenshot": screenshot_path
        }

    except Exception as e:
        logger.error(f"Ошибка на устройстве {device.serial}: {e}")
        return {
            "device": device.serial,
            "status": "error",
            "error": str(e)
        }


def connect_bluestacks_devices():
    """
    Подключается ко всем инстансам BlueStacks и возвращает найденные ADB-устройства.
    """
    print("Начинаем коннект к ADB и поиск инстансов...")
    client = AdbClient(host="127.0.0.1", port=5037)

    instances = get_bluestacks_instances()
    print(f"Найдено инстансов в конфиге: {len(instances)}")
    for name, port in instances:
        try:
            client.remote_connect("127.0.0.1", port)
            print(f"Успешно подключились к '{name}' на порту {port}")
        except Exception as e:
            print(f"Ошибка подключения к '{name}' (порт {port}): {e}")

    devices = client.devices()
    print(f'Всего девайсов: {devices}')
    if not devices:
        raise Exception("BlueStacks не найден.")

    return devices


class PaymentScanQueue:
    """
    RabbitMQ-очередь сканирования: один consumer-воркер закрепляется за одним эмулятором.
    prefetch=1 не даёт воркеру взять новый QR, пока текущий не обработан.
    """

    def __init__(
            self,
            rabbitmq_url: str | None = None,
            task_queue_name: str | None = None,
    ):
        self.rabbitmq_url = rabbitmq_url or os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
        self.task_queue_name = task_queue_name or os.getenv("PAYMENT_TASK_QUEUE", "payment_scan_tasks")
        self.reply_queue_name = f"payment_scan_results_{uuid.uuid4().hex}"
        self.devices = []
        self.workers = []
        self.started = False
        self._start_lock = asyncio.Lock()
        self.connection = None
        self.publisher_channel = None
        self.result_channel = None
        self.task_queue = None
        self.result_queue = None
        self.pending_results = {}

    async def start(self):
        async with self._start_lock:
            if self.started:
                return

            try:
                self.connection = await aio_pika.connect_robust(self.rabbitmq_url)
                self.publisher_channel = await self.connection.channel()
                self.task_queue = await self.publisher_channel.declare_queue(
                    self.task_queue_name,
                    durable=True,
                )

                self.result_channel = await self.connection.channel()
                self.result_queue = await self.result_channel.declare_queue(
                    self.reply_queue_name,
                    durable=False,
                    exclusive=True,
                    auto_delete=True,
                )
                await self.result_queue.consume(self._handle_result)

                self.devices = await asyncio.to_thread(connect_bluestacks_devices)
                self.workers = [
                    asyncio.create_task(self._worker(device))
                    for device in self.devices
                ]
                self.started = True
                logger.info(
                    "RabbitMQ-очередь сканирования запущена, эмуляторов: %s, queue: %s",
                    len(self.devices),
                    self.task_queue_name,
                )
            except Exception:
                await self._close_rabbitmq()
                raise

    async def stop(self):
        for worker in self.workers:
            worker.cancel()

        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)

        self.workers = []
        self.started = False
        for future in self.pending_results.values():
            if not future.done():
                future.set_exception(RuntimeError("Очередь сканирования остановлена"))
        self.pending_results.clear()

        await self._close_rabbitmq()

    async def _close_rabbitmq(self):
        if self.connection:
            await self.connection.close()

        self.connection = None
        self.publisher_channel = None
        self.result_channel = None
        self.task_queue = None
        self.result_queue = None

    async def scan(self, payment_link: str):
        if not self.started:
            await self.start()

        loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        future = loop.create_future()
        self.pending_results[job_id] = future
        task = PaymentScanTask(payment_link=payment_link, job_id=job_id)

        try:
            await self.publisher_channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(task.__dict__, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                    correlation_id=job_id,
                    reply_to=self.reply_queue_name,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=self.task_queue_name,
            )
            logger.info("QR добавлен в RabbitMQ-очередь. job_id: %s", job_id)
            return await future
        finally:
            self.pending_results.pop(job_id, None)

    async def _worker(self, device):
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(self.task_queue_name, durable=True)

        try:
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process(requeue=False):
                        payload = json.loads(message.body.decode("utf-8"))
                        payment_link = payload["payment_link"]
                        job_id = payload["job_id"]
                        logger.info("Эмулятор %s взял QR из RabbitMQ. job_id: %s", device.serial, job_id)

                        try:
                            result = await asyncio.to_thread(_process_single_device, device, payment_link)
                        except Exception as exc:
                            logger.exception("Ошибка воркера эмулятора %s", device.serial)
                            result = {
                                "device": device.serial,
                                "status": "error",
                                "error": str(exc)
                            }

                        await self._publish_result(message.reply_to, job_id, result)
        finally:
            await channel.close()

    async def _publish_result(self, reply_to: str | None, job_id: str, result: dict):
        if not reply_to:
            logger.warning("Не указана reply_to очередь для результата job_id: %s", job_id)
            return

        await self.publisher_channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps({
                    "job_id": job_id,
                    "result": result,
                }, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
                correlation_id=job_id,
                delivery_mode=aio_pika.DeliveryMode.NOT_PERSISTENT,
            ),
            routing_key=reply_to,
        )

    async def _handle_result(self, message: aio_pika.IncomingMessage):
        async with message.process(requeue=False):
            payload = json.loads(message.body.decode("utf-8"))
            job_id = payload.get("job_id")
            future = self.pending_results.get(job_id)
            if not future:
                logger.warning("Получен результат для неизвестного job_id: %s", job_id)
                return

            if not future.done():
                future.set_result(payload.get("result"))

    async def get_status(self):
        queue_size = None
        if self.publisher_channel:
            queue = await self.publisher_channel.declare_queue(
                self.task_queue_name,
                durable=True,
                passive=True,
            )
            queue_size = queue.declaration_result.message_count

        return {
            "started": self.started,
            "rabbitmq_configured": bool(self.rabbitmq_url),
            "task_queue": self.task_queue_name,
            "devices": [device.serial for device in self.devices],
            "queue_size": queue_size,
            "pending_results": len(self.pending_results),
        }


def process_payment_link(payment_link: str):
    """
    Совместимость для ручного запуска: обрабатывает одну ссылку на первом доступном эмуляторе.
    Для FastAPI используется PaymentScanQueue, чтобы распределять разные QR по свободным эмуляторам.
    """
    devices = connect_bluestacks_devices()
    device = devices[0]
    print(f"Обработка ссылки на одном устройстве: {device.serial}")
    return _process_single_device(device, payment_link)


def process_payment_link_on_all_devices(payment_link: str):
    """
    Старый режим: запускает одну ссылку на всех эмуляторах параллельно.
    Оставлен только для диагностики.
    """
    devices = connect_bluestacks_devices()

    # Запускаем обработку параллельно
    results = []
    with ThreadPoolExecutor(max_workers=len(devices)) as executor:
        # Создаём задачи для каждого устройства
        future_to_device = {
            executor.submit(_process_single_device, device, payment_link): device
            for device in devices
        }

        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                result = future.result()
                results.append(result)
                print(f"Завершена обработка {device.serial}: {result.get('amount')}")
            except Exception as exc:
                print(f"Устройство {device.serial} сгенерировало исключение: {exc}")
                results.append({"device": device.serial, "status": "error", "error": str(exc)})

    return results


# Тестовый вызов
if __name__ == "__main__":
    payment = "https://qr.nspk.ru/AD100001GDO3ABN29LNQLSG3C1U4OE0R"
    result = process_payment_link(payment)
    print("\nИтоговые результаты:")
    print(result)