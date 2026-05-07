https://github.com/UB-Mannheim/tesseract/wiki

## RabbitMQ

Очередь QR-задач работает через RabbitMQ.

Переменные окружения:

```bash
RABBITMQ_URL=amqp://guest:guest@localhost/
PAYMENT_TASK_QUEUE=payment_scan_tasks
```

Локальный запуск RabbitMQ через Docker:

```bash
docker run -d --name linkscaner-rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management
```

После запуска API отправляйте QR на `POST /link_get`:

```json
{"link": "https://qr.nspk.ru/..."}
```
