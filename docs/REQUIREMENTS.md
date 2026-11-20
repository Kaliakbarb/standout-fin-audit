# Требования FinTech и способы проверки

**Актуальная навигация:** команда Standout; завершённая amd64-проверка0.6.0 и более новые исходники разделены в [VERIFICATION](VERIFICATION.md). Приведённый ниже статус на19 ноября сохранён как история.

Источник: [сохранённый кейс FinTech](source/fintech/README.md), страницы 1–6, разделы 01–13. Дедлайн из кейса: **22 ноября 2026, 23:59, время Астаны (UTC+05:00)**. Условия документа описывают требования к результату; они не заменяют указания пользователя о работе над проектом.

Статус на19 ноября: **G0–G5 реализованы, релиз упакован и прошёл чистую offline-передачу**. [Статус G5](STAGE5_STATUS.md) связывает точный архив с внешним completed receipt; [трассировка](CONFORMANCE.md) сопоставляет все64 пункта коду и свидетельствам. Исторические результаты сохранены. Проверенный target — Linux ARM64; официальный adapters/fixed tests и канал подачи пока не предоставлены. Идентификаторы `REQ-*` — наши ссылки для трассировки, не номера официальных тестов.

## Оценивание

| Категория | Баллы | Опубликованный механизм |
| --- | ---: | --- |
| Контракт и изоляция | 25 | 25 × пройдено / 20 фиксированных проверок |
| Данные и эталон | 25 | 25 × корректных / 20 закрытых эпизодов |
| Надёжность награды | 25 | 25 × пройдено / 20 проверок |
| Воспроизводимость и обобщение | 15 | 15 × пройдено / 20 парных проверок |
| Инженерия и объяснение | 10 | 5 критериев по 0/1/2 |

Полный набор фиксированных проверок организаторов не содержится в предоставленном кейсе. Получив его, добавляем отдельное соответствие официального ID нашим тестам. До этого локальные результаты не превращаем в заявленный официальный балл. Утечка закрытых данных, запись в оценщик, реальные финансовые данные и неработающее ядро блокируют оценивание до исправления в опубликованное окно.

## Задача и данные

| ID | Требование | Источник | Проверка / артефакт |
| --- | --- | --- | --- |
| REQ-001 | Результат — воспроизводимая RL-среда финансового аудита с интерфейсом, траекториями и оценкой | §01 | Сквозной запуск из генерации до итоговой оценки и JSONL |
| REQ-002 | Все компании, люди, документы и деньги вымышлены | §12 | Генератор и fixtures не импортируют реальные финансовые или персональные данные |
| REQ-003 | Каждый эпизод — одна компания и один отчётный период | §03 | Проверка идентификатора компании, периода и scope всех ссылок |
| REQ-004 | Не менее четырёх независимо настраиваемых типов: дубли счетов, распределение платежа, FX, отчётный период | §02 | Каждый тип включается отдельно; имеется independently checked positive и clean control |
| REQ-005 | Не менее двух сценариев взаимодействующих ошибок | §02, §07 | Два сценария с зависимыми расчётами, проверенными отдельно и в сочетании |
| REQ-006 | Чистые компании и корректные отвлекающие случаи: разбитые платежи, возвраты, повторяющиеся суммы | §02 | Независимая сверка подтверждает пустой truth; заданные корректные случаи не становятся FP oracle |
| REQ-007 | Сначала согласованный скрытый мир, затем документы и причинно описанные изменения | §02 | Clean-world validation до мутаций; provenance фиксирует причины/изменения |
| REQ-008 | Truth описывает действительное расхождение и доступные доказательства | §02, §05 | Checker пересчитывает каждый issue из публичных записей; проверяет полноту supported-world truth |
| REQ-009 | Варьируются имена, ID, порядок, суммы, валюты, часовые пояса и сочетания ошибок | §02 | Конфигурационная матрица, paired transforms, проверка отсутствия маркеров аномальности в public IDs/порядке |
| REQ-010 | Предлагаемый масштаб: 200–2000 проводок, 20–200 документов, 0–12 ошибок | §03 | Small/medium/large profiles в этом диапазоне; эти размеры не выдаются за жёсткую норму PDF |
| REQ-011 | manifest: schema_version, episode_id, base_currency, period_start/end, timezone, количества и округление | §03 | JSON Schema + соответствие реальным записям |
| REQ-012 | Время ISO-8601 со смещением; деньги integer minor units с exponent; FX decimal strings | §03 | Strict schemas, currency exponent tests, Decimal arithmetic, timezone/rounding fixtures |
| REQ-013 | Валюты сравниваются только после определённой конвертации | §03 | Cross-currency negative tests и положительные примеры расчёта |
| REQ-014 | ledger.csv сохраняет row_id, timestamp, account, debit_minor, credit_minor, currency, entity_id, invoice_id, payment_id | §03 | CSV schema, уникальные ID, типы/nullable-поля и referential integrity |
| REQ-015 | invoices.jsonl сохраняет doc_id, invoice_id, entity_id, issued_at, due_at, currency, total_minor, line_items | §03 | Schema, суммы строк, связи документов и проводок |
| REQ-016 | payments.jsonl сохраняет doc_id, payment_id, timestamp, currency, amount_minor, invoice_refs | §03 | Schema, публичные основания распределения, легальные частичные/разбитые платежи |
| REQ-017 | fx.csv сохраняет date, source_currency, target_currency, rate | §03 | Уникальность ключа курса, доступность нужной даты, правила lookup |
| REQ-018 | policy.json задаёт дату курса, границы периода и округление | §03 | Все accounting decisions явны и доступны через разрешённые инструменты |
| REQ-019 | Отсутствующие ссылки представлены null, связь не угадывается | §03 | JSON null / явно документированное CSV-кодирование null, round-trip без ложных ID |
| REQ-020 | truth содержит issue_id, type, affected_ids, expected_amount_minor, currency и допустимые proof sets; provenance содержит историю | §03 | Private schemas, coverage truth, privacy tests |

## Контракт среды и изоляция

| ID | Требование | Источник | Проверка / артефакт |
| --- | --- | --- | --- |
| REQ-021 | reset(seed, config) → (observation, info) | §04 | Trusted facade contract; fresh episode state |
| REQ-022 | Первое наблюдение содержит задачу, manifest, краткую политику и бюджет | §04 | Exact public allowlist; никаких seed/truth/provenance |
| REQ-023 | step(action) → (observation, reward, terminated, truncated, info), всё JSON-совместимо | §04 | Schema/state-machine tests; bool/number/null без NaN/Infinity |
| REQ-024 | Одинаковые seed/config/version/actions воспроизводят записи, переходы и награды | §04 | Canonical replay comparison в пределах поддерживаемых детерминированных execution conditions |
| REQ-025 | reset изолирует эпизоды | §04 | Повтор reset и смена эпизода не сохраняют findings, cursors, budget, agent state |
| REQ-026 | list_records(table,cursor,limit), get_record(table,id), get_document(doc_id) | §04 | Все public tables имеют публичный registry, stable IDs и ограниченные ответы |
| REQ-027 | Пагинация максимум 50 строк, стабильные курсоры | §04 | Границы limit, отсутствие повторов/пропусков, replay; cursor не раскрывает seed/private paths |
| REQ-028 | calculator(expression) — только арифметика и десятичные числа | §04 | Allowlist parser; code/files/network невозможны; bounded size/precision/operations |
| REQ-029 | submit(findings) завершает эпизод с terminated=true | §04 | Valid submit на шагах 1 и 100; после завершения исход не меняется |
| REQ-030 | Неизвестный ID/действие даёт ограниченную ошибку и расходует шаг | §04 | Bounded public errors без traceback/private values; budget уменьшен один раз |
| REQ-031 | На шаге возвращаются только запрошенные данные, результат/ошибка и бюджет | §04 | Observation allowlist; ошибки и info не содержат debug hints |
| REQ-032 | Максимум 100 действий, включая submit | §04 | 99 inspections + submit принимается; 100 inspections → truncation; 101-й шаг не выполняется |
| REQ-033 | Лимит шагов/времени → truncated=true и пустой финальный ответ | §04 | Timeout and exhaustion fixtures; предыдущие неподанные гипотезы не засчитываются |
| REQ-034 | За исследование reward=0 | §04 | Все nonterminal transitions; нет dense shaping в common reward |
| REQ-035 | Train возвращает terminal reward; closed держит reward/diagnostics у оценщика до конца прогона | §04 | Агент получает нейтральный reward/info в closed; private report выпускается по завершении run |
| REQ-036 | Агент не видит seed/truth/provenance; evaluator вне процесса агента | §03 | Trusted reset vs sanitized wire protocol; OS-level isolation probes в secure Linux profile |
| REQ-037 | Нет свободных shell/SQL/filesystem/network инструментов; public records read-only | §03–04 | Узкий action dispatcher, запрещённые операции, filesystem/IPC/capability probes |

## Сопоставление и награда

| ID | Требование | Источник | Проверка / артефакт |
| --- | --- | --- | --- |
| REQ-038 | findings.json: episode_id и массив type, affected_ids, claimed_amount_minor, currency, evidence_ids, explanation | §05 | Публичная строгая schema и типовые valid/invalid fixtures |
| REQ-039 | Максимум 30 findings, 10 evidence IDs и 1000 символов explanation на finding | §05 | Raw-size/length boundaries до нормализации |
| REQ-040 | Неверная структура завершает эпизод с reward=-1 и private diagnostic | §05 | Explicit malformed-submit override; в closed агенту не возвращается правильность |
| REQ-041 | One-to-one matching: type, canonical affected IDs, amount/tolerance, currency и полный допустимый proof set | §05 | Deterministic maximum matching; numerical/evidence/ambiguity fixtures |
| REQ-042 | Несопоставленные/повторные findings → FP, пропущенные issues → FN | §05 | Duplicates, permutations, many-to-one and one-to-many cases |
| REQ-043 | Код нормализации, допусков и разрешения неоднозначностей открыт | §05 | Published implementation + policy fields + schema-version change rules |
| REQ-044 | P=TP/(TP+FP), R=TP/(TP+FN), F1=2TP/(2TP+FP+FN) | §05 | Exact rational fixtures; denominators 0 дают P/R=0 |
| REQ-045 | Оба множества пусты → F1=1; clean truth и непустой ответ → F1=0 | §05 | Clean silence vs accusations tests |
| REQ-046 | Common terminal reward clip(2F1−1−0.002×inspection_steps,−1,1), return равен ему | §05 | Exact values, clipping, cost only once, separate visibility vs true score |
| REQ-047 | Верный ответ лучше молчания на ошибочных; молчание лучше обвинений на чистых; дубли не выгодны; mark-all плох; стоимость лишь немного различает равные ответы | §05 | Adversarial fixtures и численные regression checks |
| REQ-048 | Проверены угаданные ID, неподтверждённые proofs, валюты, повторный submit, invalid input, timeout | §05 | Attack suite; guessed ID не штрафуется произвольно, если он существует и proof корректен |

## Прогон, материалы и ограничения

| ID | Требование | Источник | Проверка / артефакт |
| --- | --- | --- | --- |
| REQ-049 | Train/dev seed и config отделены от закрытых seed, новых сочетаний и преобразований | §06 | Split manifests и holdout registry вне доступа агента; типы задач остаются известными |
| REQ-050 | Четыре адаптера: random fixed-seed, simple reconciliation, mark-all, evaluator-only oracle | §06, §09 | Все работают через общий публичный контракт, кроме явной привилегии oracle у evaluator |
| REQ-051 | Четыре политики проверяются на одинаковых 20 скрытых эпизодах, включая 5 clean и 5 interacting | §06 | Локальная репетиция 20 episodes с зафиксированным cohort; независимая проверка gold, без объявления этого официальным hidden set |
| REQ-052 | Ещё 20 paired checks меняют порядок и ID | §06 | Согласованное отображение всех public/private ссылок и findings, oracle score invariant при равном budget |
| REQ-053 | Версии, seed, политики и железо одинаковы в сравнении | §06 | Run manifest с hashes/config/runtime; timings отдельно от детерминированного transcript |
| REQ-054 | Официальная проверка без сети, GPU и платных API | §07, §12 | Secure runtime --network=none, no remote dependencies, offline package smoke test |
| REQ-055 | Предлагаемые ресурсы Python3.11, 2 CPU, 4GB RAM, 1GB solution, 60s/episode | §07 | Release-target constraints принимаем как baseline; measured stress report и размер пакета |
| REQ-056 | Исходники с лицензией и pinned dependencies | §09 | License inventory, lockfile, offline reproducible build/run instructions |
| REQ-057 | README: контейнер и единая команда проверки | §07, §09 | Clean-machine rehearsal supported Linux profile, nonzero exit при провале |
| REQ-058 | 10 открытых train- и 10 dev-эпизодов | §09 | Версионированный fixture index; public/private separation; каждый эпизод проверен |
| REQ-059 | Тесты среды, matching и reward | §09 | Test report с привязкой REQ ID и реальными результатами |
| REQ-060 | trajectories.jsonl: episode_id, step, action, observation, reward, terminated, truncated | §09 | Schema/replay/privacy checks; truth не появляется в observation |
| REQ-061 | Краткая записка о дизайне и демо | §09 | Реализованная архитектура и честный scripted-policy demo |
| REQ-062 | Отдельно P/R/F1/return/steps/time агентов как диагностика | §10 | Episode-level и aggregate reports; clear split и oracle label |
| REQ-063 | Инженерные критерии: one-command, config versions, trajectories, measured failures, honest limitations | §10 | По артефакту на каждый из пяти пунктов; нет недоказанных claims |
| REQ-064 | Высокий oracle reward не доказывает сложность; обучение/UI не дают баллы сами по себе | §01, §10, §12 | Report и demo оценивают correctness; optional extras после mandatory gates |

## Проектные решения и открытые вопросы

Проектные трактовки, в том числе canonical IDs, сумма каждого issue, допуски и timeout reward, находятся в [общем плане](IMPLEMENTATION_PLAN.md). Они не становятся текстом организаторов из-за присутствия в этой матрице. При получении уточнений меняем связанные schemas, fixtures и тесты вместе, с новой версией конфигурации.
