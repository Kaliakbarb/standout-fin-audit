# Автономный релиз FinTech

Пакет программы 0.6.0 предназначен для Linux/amd64 Docker engine. На управляющем хосте нужны Python3.11 или новее и Docker CLI с доступом к engine. `uv`, локальная модель, платные API, pip/apt и сеть для выполнения не нужны: Python3.11 и закреплённые зависимости уже находятся в image archive. Другую архитектуру нельзя считать проверенной без собственного прогона.

## Что передаётся

Внешний `genesis-fin-audit-0.6.0-submission.zip` содержит неизменяемый `genesis-fin-audit-0.6.0-linux-amd64.tar.gz` с исходниками, тестами,20 открытыми train/dev книгами, схемами, конфигурациями, лицензиями и `runtime/image.tar`. Внутри tar.gz находятся оба launcher — `./verify` и `./demo`, пять browser assets, лаборатория и открытое showcase-досье с27 реальными trajectories. Закрытые контрольные seeds/configs из рабочих artifacts в публичный пакет не копируются.

Рядом с tar.gz внутри ZIP передаются `SHA256SUMS`, внешний `CLEAN_ROOM_REPORT.json`, `DELIVERY_REPORT.json`, JUnit и итоговая сводка чистого прогона. `EXECUTION_PLATFORM.json` и `LOADED_IMAGE.json` фиксируют наблюдаемую платформу и точный загруженный образ, `web-summary.json` — фактический HTTP/API-прогон. `HOST_CONFIG.json`, `INITIAL_ENVIRONMENT.json` и `CLEANUP.json` являются явно отмеченными публичными подмножествами исходных отчётов; private command/seed из них исключены. Внешний clean report ссылается на SHA-256 ровно того tar.gz, который распакован и выполнен. Он создаётся после теста и включается только во внешний ZIP, сохраняя проверенный tar.gz неизменным.

## Проверить и распаковать

Сначала сверьте SHA-256 ZIP с отдельно полученным checksum, затем распакуйте ZIP и сверьте SHA-256 внутреннего tar.gz с `SHA256SUMS`:

```bash
shasum -a 256 genesis-fin-audit-0.6.0-linux-amd64.tar.gz
```

Имя конечного архива указано в каталоге сдачи. На Linux вместо `shasum` можно использовать `sha256sum`. Digest должен совпасть с отдельно полученным checksum; это контроль целостности, а не цифровая подпись автора.

Для автоматической защищённой распаковки из исходного проекта:

```bash
python3 -B scripts/release.py extract /path/to/release.tar.gz \
  --output /path/to/new-directory --sha256 EXPECTED_SHA256
```

Extractor проверяет все tar members до записи: пути, типы, дубликаты, размеры, отсутствие links; существующий каталог не заменяется. Для ручной распаковки следует использовать только уже проверенный архив и новый каталог. Файлы корня пакета находятся непосредственно в архиве; после распаковки перейти в выбранный новый каталог.

## Одна команда

Из корневого каталога распакованного пакета:

```bash
./verify --offline
```

Чтобы выбрать engine явно:

```bash
./verify --offline --context YOUR_DOCKER_CONTEXT
```

Launcher принудительно включает release mode. Сначала проверяются точный inventory, размеры, modes и SHA-256 файлов; затем архив image всегда загружается локально. OCI manifest ID, config ID и rootfs layers связываются с проверенным архивом. После загрузки используется точный immutable ID, поддерживаемый данным Docker store. Наличие одноимённого cached tag не заменяет проверку. Image pull не выполняется.

Тесты и acceptance запускаются с `--pull=never --network=none`,read-only rootfs,2 CPU,4GiB RAM и64 PIDs. Обычный worker получает отдельный непривилегированный chroot/seccomp runtime и публичные инструменты. Неподдерживаемая sandbox завершает проверку ошибкой; перехода к cooperative нет.

По умолчанию output directory и private master-seed receipt создаются **рядом с bundle**, не внутри него. Повторный запуск сохраняет первоначальный inventory. Явные пути:

```bash
./verify --offline --output /path/to/new-results \
  --seed-file /path/to/private-master-seed.json
```

Каталог результатов должен быть новым, seed receipt — вне bundle. Ошибка теста, нарушения целостности или acceptance возвращает nonzero. `cgroup-metrics.json` записывается после завершения дочерних процессов и содержит kernel peak/limits; отсутствие неподдерживаемого counter явно отмечается.

Результаты сначала записываются в пустой anonymous Docker volume `/output` одного контейнера. После его остановки launcher считывает действительный exit code, копирует artifacts через `docker cp` без сохранения чужих UID/GID и удаляет только свой exact container ID вместе с его anonymous volume. Host-каталог остаётся private0700; закрытые файлы сохраняют0600 и принадлежат вызывающему пользователю. Host filesystem не монтируется на запись в контейнер, поэтому различия UID между Linux, macOS и VM не требуют ослабления разрешений. При ошибке, тайм-ауте или Ctrl+C выполняется такая же адресная очистка; сбой копирования или очистки завершает проверку ошибкой.

## Интерактивный полигон из пакета

Из того же корня запустите:

```bash
./demo --offline --port 8765
```

Доступен выбор context через `--context YOUR_DOCKER_CONTEXT`. Откройте http://127.0.0.1:8765/. Host launcher использует только Python stdlib и Docker CLI, проверяет bundle и image, затем передаёт HTTP через Docker exec в localhost web server внутри контейнера. `network=none`, read-only rootfs, ограничения и secure worker profile сохраняются. Внешняя сеть и registry не используются; browser assets входят в exact image. Ctrl+C убирает только контейнер своего запуска. При ошибке проверки manifest/image fallback на локальные исходники отсутствует.

## Где смотреть результат

- `tests.xml`: полный JUnit без замены failures пропусками.
- `run/summary.json` и `run/COMPLETE.json`: приложение и материалы, включая named presentation.
- `run/application/`: полный G4/G3 regression,20+20, mutation baseline/faults и showcase.
- `run/readiness/web-summary.json`: HTTP-проверка пяти assets, трёх книг и двух реальных worker на каждой; hash этого отчёта включён в readiness summary.
- `run/showcase.html`: самодостаточные карточки источников и реальные результаты.
- `cgroup-metrics.json`: whole-container cgroup measurements, включая тесты и workers; Docker-daemon/VM сюда не входят.
- `CLEAN_ROOM_REPORT.json` рядом с передаваемым архивом: отдельный fresh-engine/airgap/transfer proof; его нельзя вывести из одного успешного запуска приложения в старом engine.

Public sample не является официальным hidden set. Oracle получает готовые ответы; simple — самостоятельная скриптовая сверка. Фиксированные проверки и адаптеры организаторов, а также канал сдачи, пока не предоставлены. Подготовка локального комплекта не означает его отправку организаторам.

## Восстановить пакет из исходников

С доступом к сети заранее подготовить pinned image:

```bash
./scripts/prepare --platform linux/amd64 --context YOUR_AMD64_CONTEXT --export-image dist/image-0.6.0-linux-amd64.tar
./scripts/verify --offline --context YOUR_AMD64_CONTEXT --output artifacts/pre-release-check
```

Затем получить точный image ID через `docker image inspect` и собрать пакет из completed secure showcase:

```bash
python3 -B scripts/release.py build --output dist/release-0.6.0 \
  --image-id sha256:EXACT_IMAGE_ID --image-archive dist/image-0.6.0-linux-amd64.tar \
  --sample artifacts/pre-release-check/run/application/showcase \
  --context YOUR_DOCKER_CONTEXT
```

Builder сверяет runtime source hashes с точным образом, image archive/config/architecture и approved sample receipt. Package timestamps/order/ownership нормализуются; одинаковые входные bytes дают одинаковый tar.gz. Новая Docker-сборка или новый timing report могут менять artifact hashes и требуют новой фиксации, а не заявления о bit-identical пересборке.

`scripts/clean_rehearsal.py` — developer runner для повторения чистого offline transfer в поддерживаемой Colima VM: отдельные containerd/dockerd roots и Unix socket, новый net namespace, пустые images/containers/volumes до import. Он не очищает и не переключает рабочий engine. Точные фактические результаты и проверенная платформа фиксируются в [статусе 0.6.0](RELEASE_0_6_STATUS.md) и внешнем receipt.

Для текущего Linux amd64 прогона в full-system QEMU на ARM Mac явно выберите отдельный x86_64 профиль и `overlay2`:

```bash
python3 -B scripts/clean_rehearsal.py dist/release-0.6.0/bundle \
  --profile genesis-fin-audit-amd64 --expected-platform linux/amd64 \
  --storage-driver overlay2 --output artifacts/clean-release-0.6.0 \
  --sha256 EXPECTED_ARCHIVE_SHA256
```

Нужны поддержка OverlayFS ядром Linux guest и пригодная файловая система для нового private Docker root; в текущем x86_64 guest используется ext4. Runner принимает только `--storage-driver vfs` или `overlay2`; значение по умолчанию `vfs` сохранено для совместимости. VFS полностью копирует слои и в QEMU может превысить закреплённое время импорта image. Выбор `overlay2` меняет реализацию хранилища нового пустого daemon: источники image, private containerd/root/socket, отсутствие маршрутов и `network=none` приложения сохраняются. Лимиты2 CPU/4GiB и существующие тайм-ауты загрузки, проверок и исполнения не увеличиваются. Незавершённый импорт не является прогоном приложения или тестов; успех подтверждает только итоговый completed receipt. Исторические VFS-свидетельства 0.5.0 остаются без изменений.

После успешного чистого прогона точного tar.gz соберите внешний ZIP:

```bash
python3 -B scripts/submission.py --release dist/release-0.6.0 \
  --clean artifacts/ACTUAL_COMPLETED_CLEAN_RUN --output dist/submission-0.6.0
```

Каталог `--clean` должен содержать действительный completed receipt этого архива. Сборщик проверяет его hash-цепочки, тесты, платформу, web-readiness и завершённую очистку отдельного engine. Он копирует только разрешённые публичные свидетельства, удаляет trusted command/environment из public host config и проверяет отсутствие закрытых seed в sidecars. Результат — ZIP, его отдельный `.sha256` и публичные файлы рядом; исходный sealed tar.gz не меняется. Наличие этой команды в документации не означает, что чистый прогон уже завершён: актуальное состояние указано в статусе релиза.

## Архитектура и фактический способ исполнения

Архив 0.6.0 содержит Linux amd64 image. Для официального хоста нужен Docker engine, способный исполнить amd64 Linux и поддерживающий требуемые security primitives. При локальной репетиции на ARM Mac используйте отдельную full-system x86_64 VM; простая маркировка image или qemu-user не подтверждают работу seccomp. External clean receipt фиксирует client host, VM, daemon и application architectures, а также способ исполнения. Эмуляция хоста явно обозначается и не выдаётся за физический x86-компьютер.

Старый sealed release 0.5.0 для ARM64 сохраняется отдельно и не заменяет проверку нового amd64-архива. Внешний `CLEAN_ROOM_REPORT.json` создаётся после распаковки и выполнения точного нового архива. Архив, manifest и все опубликованные checksum должны относиться к одной версии.
