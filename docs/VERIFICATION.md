# Проверки, доказательства и границы версий

Этот документ разделяет три объекта: сохранённый автономный релиз, последнее обновление интерфейса и текущие исходники при публикации в GitHub. Результат одного объекта не переносится автоматически на другой.

## 1. Текущие исходники перед GitHub-публикацией

Полный host-прогон выполнен 21 ноября 2026 по времени Астаны / 20 ноября UTC:

```bash
./scripts/dev pytest -q --junitxml=artifacts/github-publication/source-tests.xml
node --test tests/web_lesson.test.mjs tests/web_replay.test.mjs
```

| Показатель | Результат |
| --- | ---: |
| Python passed | 1013 |
| Python failures / errors | 0 / 0 |
| Python skipped | 5 |
| Python elapsed | 75,198 s по JUnit |
| JavaScript passed | 11 |
| JavaScript failures / skipped | 0 / 0 |

Пять пропусков — настоящие Linux sandbox tests. На macOS они требуют подготовленный Linux-контейнер. Этот прогон подтверждает host-регрессии текущего кода, но не является новым доказательством secure Linux-изоляции или clean-room передачи.

Доказательства: [receipt и hashes исходников](verification/github-publication/COMPLETE.json), [JUnit](verification/github-publication/tests.xml), [контрольные суммы копий](verification/github-publication/COPIED_SHA256SUMS).

## 2. Последнее обновление интерфейса и исправлений

Целевой прогон обновления v5:

- 89 Python tests, 0 failures/errors/skips.
- 11 JavaScript tests, 0 failures/skips.
- Реальный HTTP через локальный preview и secure backend: четыре состояния исправлений, reward исходного запуска +0,992, неизменная исходная попытка.
- До терминального ответа endpoint исправлений возвращает 409; неверные флаги — 400.
- Браузерная проверка: состояния 550 000/2, 500 000/1, 600 000/1, 550 000/0; просмотр изменённых строк; мобильная ширина 390 px; ошибок консоли не обнаружено.

Доказательства: [COMPLETE](verification/frontend-v5/COMPLETE.json), [HTTP](verification/frontend-v5/real-http.json), [browser checks](verification/frontend-v5/browser-checks.json), [Python log](verification/frontend-v5/python-tests.txt), [Node log](verification/frontend-v5/node-tests.txt), [JUnit](verification/frontend-v5/tests.xml).

Приведённый secure HTTP-пример использовал сохранённый backend и новую host-реализацию post-run checker. Он не заменяет сборку актуального кода в новом image. [Галерея](SCREENSHOTS.md) показывает эту версию исходников.

## 3. Сохранённый автономный релиз 0.6.0

Точный ZIP:

```text
genesis-fin-audit-0.6.0-submission.zip
73 854 478 bytes
SHA-256 a2ec5f1302751d182c636482999950547162108a031b5151c0c825cfc22a435d
```

Внутренний архив:

```text
genesis-fin-audit-0.6.0-linux-amd64.tar.gz
73 722 947 bytes
SHA-256 76a1f02d12331da6e6b6b6b0b6d46f56db5e7c2f05fed9cd043d42fbdc0325e1
```

### Фактическое выполнение

| Проверка | Результат |
| --- | ---: |
| Python tests | 994 passed, 0 failures/errors/skips |
| Прикладные проверки | 48 |
| Проверки изоляции | 19 |
| Обнаруженные смысловые мутации | 12 / 12 |
| Mutation survivors / invalid / timeouts | 0 / 0 / 0 |
| Контрольные эпизоды | 20 |
| Запуски четырёх политик на одном cohort | 80 |
| Парные ID/order случаи | 20 |
| Точные replay | 80 |
| Проверки packaged demo | 13 |
| Secure workers в packaged demo | 6 |
| Группы заключительного аудита | 18 passed |

Лимиты — 2 CPU, 4 GiB RAM, без swap, 64 PID. Пик памяти всего контейнера — 288 063 488 bytes / 274,72 MiB, OOM=0. Clean-run занял 2082,24 s в full-system QEMU x86_64 Linux VM на ARM Mac. Это не измерение физического amd64-хоста. Работа подготовленного пакета выполнялась без сети, GPU и платных API.

Сохранённые копии публичных документов переданного ZIP:

- [CLEAN_ROOM_REPORT](verification/release-0.6.0/CLEAN_ROOM_REPORT.json): точный архив, платформа, JUnit и hashes.
- [EXECUTION_PLATFORM](verification/release-0.6.0/EXECUTION_PLATFORM.json): способ исполнения amd64.
- [FINAL_DELIVERY_COMPLETE](verification/release-0.6.0/FINAL_DELIVERY_COMPLETE.json): компактный итог.
- [FINAL_SUBMISSION_AUDIT](verification/release-0.6.0/FINAL_SUBMISSION_AUDIT.json): 18 групп проверки архива и цепочек доказательств.
- [JUnit](verification/release-0.6.0/tests.xml), [cgroup metrics](verification/release-0.6.0/cgroup-metrics.json).
- [READINESS_REPORT](verification/release-0.6.0/READINESS_REPORT.json), [web summary](verification/release-0.6.0/web-summary.json), [packaged demo](verification/release-0.6.0/PACKAGED_DEMO.json).
- [Полная публичная summary.json](verification/release-0.6.0/summary.json): неизменённые bytes из ZIP; per-policy/per-cohort/per-type метрики, все80 cohort runs,20 pairs и80 replay.

Для прямой проверки формата журнала добавлен [реальный открытый masking-пример](verification/release-0.6.0/masked-simple-trajectories.jsonl): шесть исходных JSONL-строк одного `simple` run, включая reset и пять действий. Он исполнялся в closed feedback mode, поэтому все wire rewards равны0, а info пуст. Его [истинная post-run оценка](verification/release-0.6.0/masked-simple-evaluation.json) отдельно показывает2 TP, F1=1 и reward0,992. Это опубликованный showcase, не закрытый контрольный мир организаторов. [EXAMPLE_MANIFEST](verification/release-0.6.0/EXAMPLE_MANIFEST.json) фиксирует исходный файл, run_id, отбор строк и hashes; строки траектории сохранены побайтово.

Это отобранные документы, а не полный архив всех raw результатов. Исходный [SHA256SUMS](verification/release-0.6.0/SHA256SUMS) описывает комплект ZIP; часть перечисленных файлов намеренно не хранится в Git. [COPIED_SHA256SUMS](verification/release-0.6.0/COPIED_SHA256SUMS) проверяет файлы, скопированные в эту папку. Архив и образ остаются в локальном `dist/`.

### Что этот receipt не подтверждает

После фиксации ZIP обновлены дизайн, текст интерфейса и интерактивный repair endpoint. Старый архив остался неизменным. Перед сдачей последней версии необходимы новая сборка и полный offline clean-run с новым SHA-256. Программная версия 0.6.0 в metadata сама по себе не означает побайтового совпадения исходников с сохранённым релизом.

## Исторические этапы

В `docs/verification/stage1`–`stage5` сохранены исходные отчёты развития проекта. Их даты, версии и архитектуры относятся к тем прогонам. G5 / 0.5.0 — исторический ARM64-релиз. Не используйте его результаты для утверждений о новом amd64-пакете.

## Повторить

Для обычных регрессий используйте команды раздела 1. Для secure-проверки подготовьте Linux image, затем выполните:

```bash
./scripts/prepare --platform linux/amd64 --context NAME
./scripts/verify --offline --context NAME
```

Для передачи и проверки готового пакета — [RELEASE_GUIDE](RELEASE_GUIDE.md). Для сверки требований — [CONFORMANCE](CONFORMANCE.md).

## Честные ограничения

Локальный контрольный набор и reference adapters принадлежат проекту. Они не являются официальными скрытыми тестами организаторов. 19 наших isolation probes не равны официальным 20 contract checks. Oracle — привилегированный контроль оценщика. Официальная оценка и отправка организаторам этими отчётами не подтверждаются.
