# Статус четвёртого этапа

**G4 завершён**, программа **0.4.0**,19 ноября2026. Реализованы и совместно проверены мутационный стенд оценщика, маскирующий сценарий, обе зависимые counterfactual repair и офлайн-досье с настоящими документами и результатами workers.

Команда: `./scripts/verify --offline`.

- **703 теста прошли**,0 failures/errors/skips; pytest143.70с.
- **39/39** общих acceptance checks; **19/19** фактических sandbox probes.
- **78/78** baseline scorer tests; **12/12** заранее объявленных поломок обнаружены assertion-проверками.
- **0** survivors, invalid experiments, timeouts и not-run mutants.
- **9 showcase-вариантов ×3 политики =27 executions**; реальные trajectories сохранены и воспроизведены перед созданием HTML.
- Полный G3 regression сохранён:10 train+10 dev,80 основных policy executions на20 эпизодах,20 пар,80 точных action replay и fresh-worker repeat.

Это локальные результаты на проверенной платформе, не официальный балл организаторов.

## Показательный сценарий

**[Открыть завершённое офлайн-досье](verification/stage4/showcase.html)**. Это копия HTML из финального secure-прогона; сеть, JavaScript и сервер для просмотра не нужны.

В исходных документах два счёта:50 000₸ и1 000USD по правильному курсу500₸/USD. Матрица сохраняет одинаковые source facts, policy и FX-таблицу:

| Вариант | Сумма признанных счетов | Доказанные ошибки |
| --- | ---: | --- |
| Clean | 550 000₸ | 0 |
| Только duplicate | 600 000₸ | 1: повторное признание50 000₸ |
| Только FX | 500 000₸ | 1:450 вместо500₸/USD |
| Обе ошибки | 550 000₸ | 2: duplicate и FX |

В последней строке эффекты **+50 000 /−50 000₸** взаимно маскируются, хотя обе ошибки подтверждаются источниками.100 000₸ — сумма абсолютных эффектов на выбранный recognition/revenue показатель, не универсальная оценка ущерба и не сумма произвольных issue amounts. Каждый журнал сбалансирован, gold заново выведен независимым checker.

Два дополнительных counterfactual repair показывают причинность:

| Исправление | Устраняемая ошибка | Оставшаяся ошибка allocation |
| --- | --- | --- |
| Курс платежа | FX450₸ →0 | 49 925₸ →50 000₸: сумма пересчитывается |
| Период платежа | Cutoff313 050₸ →0 | 52 175₸ →52 175₸: распределение остаётся неверным |

Repair functions выбирают записи и рассчитывают исправления по public documents/policy/metadata. Они не используют truth как инструкцию ремонта. Исходные invoice/payment/FX facts сохраняются.

Девятый вариант меняет ID и порядок `masked_both`. В обоих вариантах ordinary simple доказывает две находки:TP2,FP0,FN0,F1=1. Во всех девяти книгах simple получает точный ответ, return0.992 и5 действий, из них4 inspections. Oracle получает1 без inspections; silent получает−1 на dirty книгах и1 на clean. Эти показатели относятся к фиксированным demonstration variants, а не к девяти новым независимым hidden worlds.

## Мутационный эксперимент

[Frozen plan12 faults](../configs/scorer-mutations.json) зафиксирован до первого эксперимента. SHA-256: `bf0ef2e403052799e85838d7c86b59c64c6482929a0c2638f896b3c74cb87d94`.

Каждая поломка применена к отдельной временной AST-копии `evaluation.py`. Child проверяет путь и hash реально импортированных scorer/contracts; detection tests скопированы из настоящего scorer suite. В отчёте сохранены изменённый исходник, diff, target line, AST/source/patch hashes, команда, exit code и конкретные failing test IDs.

| Mutant | Намеренная поломка | Детектор | Результат |
| --- | --- | --- | --- |
| `ignore_currency` | Не учитывать валюту | Wrong-currency semantic claim | Killed |
| `incomplete_evidence` | Принимать пересечение с proof | Требование полного alternative proof | Killed |
| `reuse_truth` | Засчитывать один issue нескольким findings | Повтор правильной находки даёт FP | Killed |
| `omit_fp` | Убрать FP из F1 denominator | Duplicate finding снижает F1 | Killed |
| `omit_fn` | Убрать FN из F1 denominator | Exact-fraction fixture с пропусками | Killed |
| `wrong_clean_f1` | Clean+empty получает F1=0 | Clean silence convention | Killed |
| `wrong_step_cost` | Увеличить стоимость inspection в10 раз | Reward при30/80/99 inspections | Killed |
| `greedy_matching` | Заменить maximum matching на first-fit | Необходим augmenting path | Killed |
| `ignore_amount` | Не учитывать сумму | Exact amount выше float precision | Killed |
| `exclusive_tolerance` | Отклонить допустимую границу | Inclusive absolute tolerance | Killed |
| `affected_superset` | Принять лишние affected IDs | Exact canonical affected set | Killed |
| `positive_invalid_reward` | Invalid submit получает+1 | Malformed schema →−1 | Killed |

Все12 получили реальный exit code1 из-за `AssertionError` в call phase указанных тестов. Import/setup/runtime exception, crash, timeout, stdout overflow и missing test не считаются kill. Регрессии стенда отдельно проверяют слабый detector, который должен честно сообщить survivor, и runtime/flood/timeout, которые должны остаться invalid. Непрошедший baseline оставляет все12 в знаменателе как not_run.

Production scorer не изменился даже относительно G3: SHA-256 `38ab88da3e503073c342df5747447e9d5f1973563c2c88da5d79e4483589fa69`. В agent API не добавлены switches изменения оценки.12/12 означает обнаружение этих12 заранее выбранных faults; исчерпывающее покрытие любых возможных дефектов не заявляется.

## Целостность отчёта и обнаруженные проблемы

Renderer проверяет completed/passed receipt, hashes пяти файлов и72 файлов девяти episode packages, canonical public/gold digests и независимые расчёты. Все27 trajectories логически повторяются через общий AuditEnv; private evaluation сравнивается с реальным outcome, без новых worker launches. Поддельные scalar rewards, observations и финансовые числа не проходят даже после обновления внешних hashes. Private configs/provenance не встраиваются в HTML; oracle явно обозначен.

Карточки содержат экранированные public sources. Проверены XSS, атомарная публикация нового файла, отказ при неполном receipt, symlinks, запрет overwrite и обход пути через `..`. Desktop1280px и mobile390px проверены в браузере: совпадающие суммы, переход к двум ошибкам, раскрытие invoice и результаты2/0/0; горизонтального overflow всей страницы нет. Широкая таблица результатов имеет собственную прокрутку.

В первом host mutation experiment обнаружилась гонка cleanup: после EOF процесс мог уже завершиться, а сигнал группе на macOS возвращал PermissionError. Стенд теперь сначала дожидается настоящего exit в пределах deadline, затем очищает группу; ошибка остановки живого child не скрывается. Реальные EOF/runtime/survivor regressions и проверки reaped/live PermissionError прошли. До исправления ошибочный запуск не был объявлен успешным mutation result.

При review пути HTML найден lexical `..` bypass проверки «вне receipt». Нормализуется проверенный parent, конечный файл по-прежнему публикуется через exclusive link без следования symlink. Регрессия вошла в общий703-test run. Подпись hero уточнена до «суммы признанных счетов», чтобы не приписывать демонстрации универсальную проверку всех финансовых итогов.

## Измерения и платформа

Финальный профиль — Linux ARM64,Python3.11.15. Inspect работающего контейнера подтверждает2 CPU,4GiB RAM,64 PIDs,network none,read-only rootfs иtmpfs256MiB. Agent address-space limit512MiB, deadline60s/episode; worker capabilities и syscall ограничения проверены probes.

| Измерение | Значение |
| --- | ---: |
| Общий pytest | 143.70с |
| Полная acceptance-фаза G4 после pytest | 73.92с |
| G3 regression внутри неё | 48.65с |
| Showcase generation/storage/27 executions | 9.59с |
| Baseline+12 mutation experiments | 14.76с |
| Supervisor lifetime high-water RSS | 136466432bytes,130.1MiB |
| Largest reaped child lifetime high-water RSS | 136466432bytes,130.1MiB |
| Самодостаточный HTML | 95345bytes |
| Docker reported image size | 69912881bytes |

RSS не является суммой процессов, active worker RSS или peak всего контейнера; child measurement включает trusted test harnesses. Image size не подменяет размер окончательного transfer archive. Cold transfer и full container-tree peak остаются G5; Linux/amd64 требует отдельного прогона.

Проверенный image `genesis-fin-audit:stage4`: `sha256:651df39678565374fd5bcc13d27bc28f7e3dd5b206728715089158641d216a42`. Все230 host input files совпали с проверенным образом; два build-generated файла отдельно извлечены и hash-verified. Источники во время и после полного прогона не менялись.

## Артефакты и границы

- [JUnit703 тестов](verification/stage4/tests.xml).
- [39 checks, G3 regression,27 showcase results и12 mutants](verification/stage4/summary.json).
- [Runtime/image/ресурсы/source hashes и completion receipts](verification/stage4/run-manifest.json).
- [Полный mutation report](verification/stage4/mutations/summary.json), [baseline](verification/stage4/mutations/baseline.json) и сохранённые mutants/diffs в той же папке.
- [Финансовый расчёт showcase](verification/stage4/showcase-analysis.json), [реальные результаты](verification/stage4/showcase-summary.json), [офлайн-досье](verification/stage4/showcase.html).
- Полные protected books/trajectories/closed seeds: `artifacts/secure-w0wk59jo/run/`; closed seeds не скопированы в публичный archive.

Точные API, команды и счётчики изложены в [контракте G4](STAGE4_CONTRACT.md). G3 остаётся121 reference executions плюс отдельный probe; G4 добавляет27 showcase executions и13 detector subprocesses. Logical replay, detector tests и тестовый pytest не выдаются за дополнительные независимые hidden episodes.

Схема книг0.2, открытый корпус0.3 и common reward сохранены. [Трассировка64 требований](CONFORMANCE.md) явно оставляет release inventory, короткую submission note, clean offline transfer и другие отмеченные границы для G5. Исторические [G1](STAGE1_STATUS.md), [G2](STAGE2_STATUS.md), [G3](STAGE3_STATUS.md) не перезаписаны.
