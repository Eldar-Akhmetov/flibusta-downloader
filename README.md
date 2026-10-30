# Flibusta Archiver

Автоматизированный конвейер для скачивания ежемесячных архивов Флибусты с booktracker.org, конвертации FB2 → TXT и складирования результата по папкам. Состоит из двух сервисов в Docker Compose:

- **downloader** — обходит раздел «Ежемесячные архивы (Флибуста)», скачивает торренты, распаковывает ZIP, режет FB2 на батчи и ставит задачи в SQLite.
- **converter** — забирает задачи из SQLite и конвертирует FB2 → TXT через `ebook-convert` (Calibre). Масштабируется репликами.

## Требования

- Docker и Docker Compose V2
- Свободное место: полный архив Флибусты — сотни ГБ; для теста хватит 5–10 ГБ
- (опционально) Cookie booktracker.org, если сайт требует авторизацию для просмотра magnet-ссылок

## Быстрый старт

```
cp .env.example .env
```
при необходимости отредактировать .env
```
docker compose up -d --build
docker compose logs -f
```
Остановить:
```
docker compose down
```
Полностью удалить состояние (темы, задачи, скачанные файлы):
```
docker compose down
rm -rf ./downloads ./work ./txt_books ./state
```

## Как это работает

downloader сканирует FORUM_URL (f=255 — раздел ежемесячных архивов), собирает ссылки на темы.

Для каждой новой темы: извлекает magnet-ссылку (или .torrent), скачивает через libtorrent в ./downloads/<hash>/.

Распаковывает ZIP в ./downloads/<hash>/_extracted/, находит все .fb2, режет их на батчи по JOBS_PER_BATCH (по умолчанию 200).

Каждый батч копируется в ./work/fb2_queue/<hash>/part_NNNN/ и регистрируется как задача в ./state/jobs.sqlite3 со статусом pending.

Реплики converter атомарно захватывают задачи (BEGIN IMMEDIATE), конвертируют FB2 → TXT в ./txt_books/<hash>/part_NNNN/, ставят done или failed.

Тема помечается downloaded только после успешной постановки всех батчей в очередь. failed_download повторяется при следующем запуске.


Каталоги
Путь	Назначение
./downloads	Скачанные торренты и распакованные FB2
./work/fb2_queue	Батчи FB2, готовые к конвертации
./txt_books	Готовые TXT, структура part_NNNN/ сохраняется
./state/jobs.sqlite3	Очередь задач и статусы тем
Настройки (.env)

### Переменная	                По умолчанию	                Смысл
FORUM_URL	                …viewforum.php?f=255	        Раздел форума для сканирования
COOKIE	пусто	            Cookie booktracker.org,         если требуется авторизация
DOWNLOADER_POLL_INTERVAL	0	                            0 — один проход и выход; иначе цикл с паузой в секундах
JOBS_PER_BATCH	            200	                            С колько FB2 в одном батче (одна задача конвертера)
LISTEN_PORT	                6881	                        Порт libtorrent
METADATA_TIMEOUT	        180	                            Ожидание метаданных magnet, секунды
TORRENT_TIMEOUT	            86400	                        Общий таймаут скачивания торрента
DELETE_TORRENT_AFTER_QUEUE	false	                        Удалять торрент-каталог после постановки в очередь
CONVERT_REPLICAS	        2	                            Количество контейнеров-конвертеров
CONVERT_WORKERS	            1	                            Потоков внутри одного конвертера
CONVERT_TIMEOUT	            600	                            Таймаут одного ebook-convert
CONVERT_POLL_INTERVAL	    10	                            Пауза между опросами очереди при простое
STALE_RUNNING_SECONDS	    86400	                        Через сколько «зависшая» задача возвращается в pending
DELETE_FB2	                false	                        Удалять FB2 из очереди после успешной конвертации

## Проверка состояния

### Статусы тем:
```
docker compose exec downloader python -c "
import sqlite3
db = sqlite3.connect('/state/jobs.sqlite3')
for r in db.execute('SELECT status, COUNT(*) FROM topics GROUP BY status'):
    print(r)
"
```

### Статусы задач:
```
docker compose exec converter python -c "
import sqlite3
db = sqlite3.connect('/state/jobs.sqlite3')
for r in db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status'):
    print(r)
"
```

### Последние ошибки:
```
docker compose exec converter python -c "
import sqlite3
db = sqlite3.connect('/state/jobs.sqlite3')
for r in db.execute('SELECT id, topic_url, error FROM jobs WHERE status=\"failed\" ORDER BY id DESC LIMIT 10'):
    print(r)
"
```

### Удалить запись о теме и все её задачи, чтобы downloader взял её заново:
```
docker compose exec downloader python -c "
import sqlite3
db = sqlite3.connect('/state/jobs.sqlite3')
url = 'https://booktracker.org/viewtopic.php?t=XXXXX'
db.execute('DELETE FROM jobs WHERE topic_url=?', (url,))
db.execute('DELETE FROM topics WHERE topic_url=?', (url,))
db.commit()
print('cleared')
"
```

Затем удалить соответствующие каталоги в ./downloads, ./work, ./txt_books (имя каталога — первые 16 символов SHA-256 от URL темы) и перезапустить downloader.

## Масштабирование
Больше параллельной конвертации: увеличьте CONVERT_REPLICAS (реплики контейнера) или CONVERT_WORKERS (потоки внутри одного контейнера). Реплики предпочтительнее: каждый ebook-convert — отдельный процесс, и изоляция по памяти проще.

Лимиты ресурсов для конвертера заданы в compose.yaml: cpus: '2', memory: 2048M на реплику. Меняйте под свою машину.

Размер батча: JOBS_PER_BATCH=200 — компромисс между балансировкой реплик и накладными расходами SQLite. Уменьшайте (50–100) при 4+ репликах, увеличивайте (500–1000) при 1–2.

## Известные ограничения
SQLite на локальном диске. ./state не должен быть на NFS/SMB — BEGIN IMMEDIATE там работает некорректно.

Конвертация теряет форматирование. TXT не поддерживает курсив, жирный шрифт, изображения — это свойство формата, а не ошибка.

Темы «Ежедневные обновления» обновляются в одном и том же URL. После первого скачивания они помечаются downloaded и автоматически не перекачиваются. Для ежемесячных архивов это не проблема: каждый месяц — новая тема.

ebook-convert может падать на отдельных битых FB2. Такие файлы логируются в jobs.error, задача уходит в failed, остальные файлы батча конвертируются.

failed_download повторяется при каждом запуске downloader'а. Если тема стабильно не обрабатывается (нет magnet, битый архив), она будет каждый раз пытаться снова — это ожидаемое поведение для временных сбоев, но для постоянных ошибок лучше чистить запись вручную (см. выше).