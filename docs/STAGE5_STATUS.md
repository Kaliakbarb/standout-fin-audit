# Релиз0.5.0 и свидетельства автономной проверки

**G5 завершён: релиз0.5.0 упакован и повторно проверен из точного архива в чистом offline engine.** Схема книг0.2, корпус0.3 и wire protocol1.0 сохранены. Отправка организаторам не выполнялась: канал сдачи и их отдельные adapters/fixed tests пока не предоставлены.

Готовый [комплект сдачи ZIP](../dist/release-0.5.0/genesis-fin-audit-0.5.0-submission.zip) содержит14 файлов: неизменяемый release archive и сопроводительные свидетельства. [Краткий delivery report](verification/stage5/DELIVERY_REPORT.json), [оригинальный clean-room receipt](verification/stage5/CLEAN_ROOM_REPORT.json) и [инструкция для получателя](../dist/release-0.5.0/SUBMISSION.md) относятся к фактически выполненной передаче. Внутри архива оставлена исходная версия этой записки до формирования пакета; clean evidence публикуется снаружи, без изменения уже проверенных bytes.

## Финальная чистая проверка

- Новый independent Docker29.5.2/vfs engine: **0 images и0 containers** перед import.
- Собственные containerd root/state/socket; другая network namespace, только loopbackDOWN, **routes=[]**. Application network=`none`.
- Точный архив **72274203bytes**, SHA-256`a541d647fe04aea7f350183a2b7a9bef3b70cbff15d639c99a69b379ff2963e3` распакован в новый каталог и выполнен через packaged `./verify --offline`.
- Повторно **826 tests**,0 errors/failures/skips; **47/47** application checks,19 probes,12/12 mutation kills.
- Clean pytest73.64с, acceptance31.73с; container entry106.30с; полный transfer/import/run/cleanup **146.35с**.
- Whole-container peak **283070464bytes,270MiB**, при memory.max4GiB, swap0,2 CPU,64 PIDs; pids.peak11, OOM/OOM-kill0.
- Legacy store запустил config ID`sha256:1afc8c6dd5f3e211b79b90b19be40f29546c5deda2809bdad6458dbd25b6bba0`; он криптографически связан с preferred OCI2453c2c7… и всеми11 layer diff IDs из sealed image archive.
- Inventory исходного и переданного bundle не изменился; собственные runtime processes/roots удалены; существующий engine/containerd и его images сохранены.

Полный ZIP с proof sidecars — **72362560bytes**; SHA-256`37d9c0c337e114e2be62b89f26a6be9c887f598830ccd403f7212fb685dc4a96`. Вложенный tar.gz внутри ZIP byte-identical проверенному a541… archive. Payload81573572bytes; все размеры ниже1GB.826-test [clean JUnit](verification/stage5/clean-tests.xml), [clean summary](verification/stage5/clean-summary.json), [kernel counters](verification/stage5/clean-cgroup-metrics.json), [initial environment](verification/stage5/INITIAL_ENVIRONMENT.json), [resource/capability proof](verification/stage5/HOST_CONFIG.json) и [cleanup proof](verification/stage5/CLEANUP.json) сохранены отдельно.

## Приложение и материалы

Подготовленный image проверен командой `./scripts/verify --offline`:

- **826 тестов**,0 errors/failures/skips; pytest77.38с.
- **47/47** проверок приложения и материалов; **19/19** sandbox probes.
-10 train+10 dev,20 контрольных эпизодов ×4 политики,20 пар и80 action replay.
-78 baseline scorer tests и12/12 обнаруженных AST-поломок; invalid/survivors/timeouts0.
-9 showcase-вариантов/27 executions; оба dependent repair и ID/order variant.
-Два дополнительных именованных эпизода прошли через ordinary worker; финансовые факты/gold и frozen corpus не изменены.

Kernel cgroup measurement охватывает **весь контейнер с тестами и потомками**, не только supervisor: peak272097280bytes, memory.max4294967296, swap.max0, pids.peak11 приlimit64, cpu.max`200000 100000`, OOM/OOM-kill0. Полный container entry107.05с, из него acceptance29.01с. Daemon и VM в этот peak не входят.

Проверенный image — Linux ARM64/Python3.11.15, preferred OCI ID`sha256:2453c2c72cc5cc30b6b43430a91238c0bb7669b764e48aaf0fbafd24b0d0e98f`, config ID`sha256:1afc8c6dd5f3e211b79b90b19be40f29546c5deda2809bdad6458dbd25b6bba0`. Docker reported image size70066686bytes.243 image source entries включают241 host input и2 build-generated файла; соответствие проверено. Фактическая инвентаризация содержит124 component records и165 license/copyright texts.

## Передаваемый пакет

Builder включает исходники/тесты/схемы/config,20 открытых книг, образ, полные license notices и самодостаточный открытый showcase sample:9×8 файлов книг,27 trajectories и исходный completed receipt. Открытый demonstration gold намеренно сохранён с0600/0700 вне agent jail. Закрытые G3 master/episode seeds и рабочие private holdout receipts в публичный пакет не входят.

Inventory фиксирует каждый файл, SHA-256, размер иmode; архив нормализует порядок,mtime0,uid/gid0. Безопасный extractor сначала проверяет все headers, traversal/duplicate/link/mode/size ограничения, затем атомарно публикует новый каталог. Существующие данные не заменяются. Wrapper не создаёт import caches внутри bundle; outputs и seed receipts размещаются снаружи.

Launcher `./verify --offline` принудительно требует release manifest, даже если файл удалён или заменён symlink. Образ всегда загружается из sealed archive. Поддержаны оба Docker store: OCI manifest ID и legacy config ID криптографически связаны с config и всеми compressed/uncompressed rootfs layer hashes. Исполняется подтверждённый immutable ID, а не cached tag.

## Чистый transfer proof

Успешный запуск приложения в подготовленном engine сам по себе не доказывает передачу. Завершение G5 подтверждает **внешний `CLEAN_ROOM_REPORT.json` рядом с release archive**, связанный с SHA-256 ровно проверенного архива, плюс его JUnit/summary/cgroup receipts. Они создаются после проверки и не добавляются задним числом в неизменяемый архив.

`scripts/clean_rehearsal.py` создаёт новые отдельные containerd/dockerd roots/socket, Unix-forward и network namespace. До import требуется0 images/containers, другая netns, только loopback и отсутствие маршрутов. Исходный engine/containerd не очищаются и не переключаются. Приложение запускается из новой строго распакованной копии; проверяются actual image/rootfs, network none,read-only,2 CPU/4GiB/64 PIDs, JUnit0 skips и kernel counters. После прогона проверяются неизменность bundle, cleanup собственных процессов/roots и сохранность существующего engine.

При разработке smoke фактически прошли5 secure-runtime тестов в пустом isolated engine. Он выявил автоматическое подключение Docker29 к глобальному containerd и различие OCI/config IDs; оба пути исправлены до финальной сборки. Падение явно заданного `DOCKER_HOST` теперь останавливает запуск, без fallback в рабочий engine. Проверены оба CAP_ формата capability names с точным сравнением разрешённого набора.

## Материалы и фактические границы

- [Записка для жюри](DESIGN_NOTE.md), [трёхминутное демо](DEMO_GUIDE.md), [release guide](RELEASE_GUIDE.md), [лицензии](THIRD_PARTY_NOTICES.md).
- [826-test JUnit](verification/stage5/warm-tests.xml), [47 application checks](verification/stage5/warm-summary.json), [whole-container counters](verification/stage5/warm-cgroup-metrics.json), [image/source/limits manifest](verification/stage5/warm-run-manifest.json).
- [Готовое офлайн-досье](verification/stage5/showcase.html).

Эти встроенные материалы фиксируют проверку программы до формирования архива. Точный архивный SHA, transfer outcome и measured clean run относятся к внешнему receipt и комплекту сдачи. Linux/amd64 не проверен. Модель ограничена опубликованной дебиторской/policy семантикой; улучшение обучения, реальные аудиторские гарантии и официальный полный балл не заявляются.
