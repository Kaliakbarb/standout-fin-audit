# Статус первого этапа финансового тренажёра

Версия 0.1.0, 19 ноября 2026. **Этапы G0/G1 пройдены**: подготовка изоляции и первый сквозной путь из [плана](IMPLEMENTATION_PLAN.md). Единая offline-команда выполнила все тесты и acceptance checks в подготовленном Linux-образе. Полный кейс из четырёх типов ошибок остаётся следующим этапом.

## Реализовано

- Версионированные shared contracts и JSON Schema публичных/закрытых форматов.
- Native KZT causal generator: clean world -> balanced ledger -> контролируемые duplicate/allocation изменения.
- Независимый truth-blind checker с полной сверкой поддерживаемых связей.
- Read-only инструменты, безопасный Decimal-калькулятор, stable pagination, reset и лимит100.
- Строгий submit, exact proofs/currencies/tolerances, maximum matching, common reward.
- Train/closed visibility и immutable terminal outcome.
- Fresh public-only reference worker, JSONL broker с bounded I/O, timeouts и очисткой процессов.
- Linux chroot/seccomp/UID/capability изоляция и обязательные реальные security probes.
- Экспорт/загрузка шести public и двух private файлов; CSV null, целочисленные деньги и no-overwrite.
- CLI generate/demo/verify, сохраняемые реальные траектории, метрики и единая offline container-команда.

## Результаты общей проверки

Команда: `./scripts/verify --offline`. Результат: **336 tests passed, 0 failures, 0 errors, 0 skipped**; pytest около11.3с. Затем выполнены **16 из16** CLI acceptance checks, включая повтор траектории и **19 из19** sandbox probes. Эти числа — фактические локальные проверки первого этапа, не официальные тесты/баллы организаторов.

| Сценарий | Политика | TP / FP / FN | F1 | Return | Всего шагов |
| --- | --- | --- | ---: | ---: | ---: |
| Чистая компания | Simple | 0 / 0 / 0 | 1 | 0.986 | 8 |
| Две ошибки | Simple | 2 / 0 / 0 | 1 | 0.984 | 9 |
| Чистая компания | Mark-all | 0 / 30 / 0 | 0 | -1 | 8 |
| Две ошибки | Mark-all | 0 / 30 / 2 | 0 | -1 | 9 |
| Две ошибки | Silent | 0 / 0 / 2 | 0 | -1 | 1 |
| Две ошибки | Evaluator-only oracle | 2 / 0 / 0 | 1 | 1 | 1 |

Default seed19: чистая компания200 проводок, ошибочная206; у каждой24 счёта и20 платежей (44 документа). Обычный simple-аудитор находит ошибки через JSON-инструменты, без checker/truth imports. В closed observations все rewards0/info{}; истинные результаты появляются только в post-run отчёте.

Артефакты:

- [JUnit всех336 тестов](verification/stage1/tests.xml).
- [Сводка16 сквозных проверок,19 probes и10 прогонов политик](verification/stage1/summary.json).
- [Манифест образа, ресурсов и SHA-256 исходников](verification/stage1/run-manifest.json).
- Полные документы/траектории/private evaluation локально: `artifacts/secure-hjo2xqhh/run/` (generated, исключены из git).

Проверено на **Linux ARM64, Python3.11.15**, через Colima native virtualization. Образ `genesis-fin-audit:stage1`, ID `sha256:d756cef4d59869549914df3deab1751d16a46b236646b139358d3de288feabd0`; Docker reported size69 227 421 bytes. В запуске принудительно заданы2 CPU,4 GiB RAM,64 PIDs, network none, readonly rootfs. Это проверка default/scenario suite с enforced limits; отдельное полномасштабное исследование производительности ещё не заявляется.

На Mac development suite:331passed и5 корректных skips Linux security tests. Разработка выполняется через `scripts/dev` с отдельным cache-environment, вне синхронизируемого Documents. Устойчивость импорта проверена повторными командами без PYTHONPATH workaround. Secure-прогон не использует host environment.

## Граница первого этапа

Stage1 поддерживает `duplicate_invoice` и `payment_allocation` в нативной валюте. Контракты новых семейств резервируются, но FX/cutoff generation и independent validation ещё не реализованы. Refund и late clearing не включены; atomic-payment policy опубликована. Чистые partial/split/repeated/unpaid случаи поддержаны.

Полное чтение default clean занимает7 inspections, ошибочного default —8; с submit соответственно8 и9 действий. Эти значения проверены на конкретном default profile, не объявляются для всех возможных размеров. Проверка oracle отдельно от ordinary policy.

Следующий этап — четыре семейства и два зависимых взаимодействия. Финальные train/dev наборы10+10, локальный20+20 protocol, mutation report, архив релиза и итоговая демонстрация ещё потребуют работы. Все64 требования пока не считаются закрытыми.

## Поддерживаемая изоляция

Trusted supervisor остаётся root в контейнере с четырьмя ограниченными capabilities: SYS_CHROOT, SETUID, SETGID и KILL. Последняя нужна для остановки непривилегированного worker по deadline. Worker после fresh exec/chroot/chdir/setresuid имеет UID/GID65534, пустые supplementary groups и effective/permitted/inheritable capabilities, no_new_privs и seccomp. Его readonly jail содержит Python stdlib и только public worker-код, без generator/checker/evaluator/private files.

Private seed/env/FD не передаются. Нет `/proc`, socket/fork/shell доступа; stdout/stderr ограничены. Closed reports выпускаются после всего batch. Уязвимости Linux/Docker и все возможные аппаратные/timing side channels не считаются доказанно исключёнными.

Cooperative режим — удобная разработка без доказательства OS-изоляции. Linux ARM64 проверяется на отдельном Colima профиле; переносимость Linux/amd64 подтверждается отдельным будущим прогоном.
