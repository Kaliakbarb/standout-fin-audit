# План реализации финансового тренажёра Genesis

Дата: **19 ноября 2026**. Дедлайн по кейсу: **22 ноября 2026,23:59 Астана**. **G0–G5 завершены для Linux ARM64**, релиз0.5.0 упакован и повторно выполнен из точного архива в пустом network-isolated engine. [Фактический статус G5](STAGE5_STATUS.md) содержит checksum,826-test receipts и ограничения. Исторические отчёты сохранены. Официальная отправка не выполнена: канал ещё не предоставлен.

План подготовлен четырьмя участниками: основной агент отвечает за требования и интеграцию; `data_design` — за финансовую модель; `environment_design` — за среду и изоляцию; `evaluation_design` — за оценивание и тесты. Направления выполнили взаимную сверку интерфейсов. Этот документ фиксирует общий порядок и решения; подробности вынесены в три проектных записки.

## Результат простыми словами

Мы создаём тренажёр для ИИ-проверяющего: вымышленная компания, её документы, скрытые ошибки, инструменты расследования и справедливая оценка ответа. Агент видит только разрешённые документы, имеет 100 действий и должен подтвердить каждую находку доказательствами.

Наша особенность — проверяемое основание каждого вердикта. Генератор начинает с корректной компании; независимая сверка проверяет весь итоговый эпизод; после завершения прогона проверяющий может открыть документы и короткий расчёт каждой ошибки. Дополнительно проверяем, что тесты обнаруживают намеренные поломки оценщика.

Показательный эпизод: счёт на 50 000 ₸ проведён дважды, а счёт на $1 000 пересчитан по 450 вместо предусмотренных вымышленной политикой 500 ₸/$. Ожидаемый и записанный итоги одинаковы — 550 000 ₸, хотя есть две ошибки. Этот пример показывает маскировку в агрегатах; два обязательных сценария зависимых ошибок реализуем отдельно.

## Источники и приоритеты

1. [Снимок первоначального кейса FinTech](source/fintech/README.md), включая шесть изображений и [транскрипцию](source/fintech/brief.txt).
2. [Матрица 64 требований](REQUIREMENTS.md): обязательства, источники и будущие проверки.
3. Этот план: наши решения для недоопределённых мест и порядок исполнения.
4. Детальные reference-документы: [данные](planning/data-design.md), [среда](planning/environment-design.md), [оценивание](planning/evaluation-design.md).

Пользователь подтвердил, что доступны только PDF с кейсами; отдельных фиксированных проверок и уточнений пока нет. Более поздние официальные уточнения сохраняем отдельным источником и вносим согласованно в schemas/config/tests. До этого работаем по открытым правилам v1. Команды внутри исходного кейса трактуем как описание требуемого продукта, а не как инструкции управлять рабочей машиной.

Сетка оценки: контракт/изоляция 25; данные/эталон 25; награда 25; воспроизводимость 15; инженерия/объяснение 10. Цель — закрыть все требования и подтвердить каждое проверкой. Не оцениваем собственный результат как официальный «100/100» без официального прогона. UI и обучение весов сами по себе баллов не дают.

## Замороженные проектные решения v1

| Решение | Принятое правило | Почему это важно |
| --- | --- | --- |
| Модель компании | Ограниченная модель счетов клиентам, дебиторской задолженности, оплат, возвратов и FX | Достаточно для четырёх типов и независимой полной сверки |
| Назначение платежа | Публичная atomic-payment policy v1: однозначно назначенный платёж полностью распределяется в одном receipt journal; late clearing вне модели | Исторический unallocated credit без такого правила не доказывает ошибку |
| Деньги | Strict integer minor units; currency exponent открыт; пересчёт Decimal с объявленным округлением | Исключает скрытый float-расход и сравнение разных валют |
| Даты | Aware ISO-8601; период `[start,end)` в timezone компании; необходимые соседние дни доступны | Cutoff можно доказать и у границы периода |
| Публичные форматы | Сохраняем обязательные поля всех шести public files; расширения явно версионированы | Организаторские адаптеры должны понимать данные |
| Missing references | JSON null; CSV содержит документированный literal `null`, который инструмент возвращает как null | Отсутствие ссылки не превращается в угаданную связь |
| Типы v1 | `duplicate_invoice`, `payment_allocation`, `fx_conversion`, `period_cutoff` | Имена и семантика публичны до запуска |
| Сумма issue | Неотрицательная type-specific величина в base currency; подробные формулы в data-design §4–5 | Суммы разных типов не выдаются за аддитивный общий ущерб |
| Canonical IDs | Exact set по опубликованным правилам; business IDs и document/evidence IDs различимы | Нельзя получить TP большим набором «всего подозрительного» |
| Evidence | Все IDs существуют в текущем public registry; один полный допустимый set содержится в findings | Красивый текст не заменяет доказательства |
| Допуск | Default 0 minor units после опубликованного округления; изменение допуска требует версии | Проверяющий не подстраивается под ответ |
| Непрочитанный ID | Факт прежнего открытия не добавляем к TP-критериям | PDF не требует наказывать угаданный, но существующий и достаточный proof |
| Matching | Детерминированный maximum-cardinality one-to-one matching | Дубли дают FP, порядок не меняет метрики |
| Лимиты | 100 total actions; submit входит; inspections — non-submit, в том числе ошибочные | Submit100 допустим; 100-й inspection завершает эпизод без ответа |
| Неверный submit | Structural invalid → terminated и истинная reward=-1 | Это явное правило кейса |
| Truncation | Empty final + common formula; не добавляем отдельный штраф -1 | На clean после 100 inspections формула даёт 0.8; status показываем отдельно |
| Closed mode | Агенту reward=0 и sanitized info до конца всего run; истинная оценка private | Промежуточная обратная связь не раскрывает gold |
| Secure runtime | Linux Docker, trusted supervisor и изолированный fresh worker; fail-closed | Отдельный процесс с общим доступом к truth недостаточен |
| Development mode | Cooperative режим явно обозначен и не засчитывается как проверенная изоляция | Локальный удобный запуск не маскирует security gaps |
| Зависимости | Python 3.11; минимальный runtime; pytest/Hypothesis для тестов; версии и hashes фиксируем при реализации | Официальный запуск автономен и воспроизводим |
| Целевые ресурсы | 2 CPU, 4 GiB total RAM, без GPU/сети/API; 60s/episode; весь release <1GB как консервативная цель | Эти числа в PDF предложены; мы принимаем их release-target |

Политика с одинаковыми численными суммами, но разными валютами не получает TP. Семантически неверные поля дают FP; нарушенная структура всего submit даёт терминальный отказ. Валидация wire не допускает NaN/Infinity, duplicate JSON keys, бесконтрольные размеры и bool вместо integer.

### Два обязательных взаимодействия

1. **Распределение + FX одного платежа.** Документированное назначение и source amount доступны. После неправильной валютной оценки меняется величина потерянного распределения; truth пересчитывается после обеих мутаций.
2. **Распределение + cutoff одного платежа.** Платёж перенесён в соседний период и частично не распределён. Проверяющий использует документы и доступные adjacent-period rows, чтобы не объявлять весь платёж отсутствующим.

Пример из reference: счёт на $1 000 при carrying rate 500; платёж $600, правильный payment rate 505, применён 495, распределено $500. Наблюдаемый журнал в major KZT: debit cash 297 000 + FX loss 2 500 = credit AR 250 000 + unallocated 49 500. Allocation issue = 49 500, payment FX issue = 6 000. При исправлении FX allocation issue изменится на 50 500. Это hand-checked design fixture, ещё не результат программы.

### Доказуемость и объём

Supported max preset: до 2000 ledger rows, суммарно до 200 invoice/payment docs, до 200 FX rows. `list_records` возвращает полные открытые записи. Полный просмотр: не более 40 + 5 + 4 страниц и 1 policy-запроса = 50 inspections. Это верхняя граница доступа к данным, не доказательство умения агента найти ответ. Каждый issue имеет достаточный witness максимум 10 IDs; недоступный/нулевой после округления issue отклоняется до выдачи эпизода.

## Модули и интерфейсы

| Модуль | Небольшой интерфейс | Ответственность / владелец |
| --- | --- | --- |
| `contracts` | Versioned schemas, public registries, money/time value rules | Общее согласование; изменения интегрирует основной агент |
| `generator` | `generate(seed, config) -> EpisodeBundle(public, private)` | Причинная история, корректная материализация, мутации, presentation transforms; данные |
| `checker` | `reconcile(public, policy) -> DerivedIssues` | Полная независимая сверка поддерживаемого мира, issues и proofs; данные с review оценивания |
| `environment` | Trusted `reset(seed,config)`, `step(action)` | Read-only tools, lifecycle, counters, clock, private evaluation; среда |
| `evaluation` | `evaluate(findings, truth, registry, inspection_steps)` | Strict validation, matching, P/R/F1/common return; оценивание |
| `runner` | Запустить policy через JSONL, сохранить траекторию и private report | Linux isolation, closed/train, timeout/resources; среда |
| `policies` | Получить public observation, вернуть одно action | Random/simple/mark-all в sandbox; evaluator-only oracle отдельно; оценивание |
| `reporting` | Построить отчёт из завершённых реальных артефактов | JSON summary, diagnosis и optional static viewer; интеграция |

`checker` восстанавливает ошибки, получая **только public records и публичную policy**, без proposed truth, скрытой истории и provenance. Лишь после завершения этого восстановления trusted harness сравнивает DerivedIssues с планом изменений/эталоном. Так проверяются не только перечисленные issues, но и полнота truth по всем поддерживаемым relations. Checker не импортирует mutation implementations. Simple baseline не вызывает private checker и не получает generator internals. Общие чистые примитивы parsing/arithmetic допустимы.

Роль recognition primary определяется публично по назначению счёта, `payment_id=null` и виду AR/revenue journal. AR-debit в документированном refund не считается повторным признанием invoice. Отсутствующее назначение платежа не выводится из совпадения entity, даты или суммы.

Trusted `reset` не пересылает seed/config агенту. В worker приходит только sanitized start payload. Агент не исполняется в trusted Python process, не получает closures/объект среды/пути к эпизоду. Публичные FX IDs находятся в tool envelope, полная policy доступна через `get_document("policy")`. Все пять действий описаны в environment-design §3.

## Порядок работ и критерии перехода

Каждый gate проверяется реальными артефактами. Незавершённый gate не считается зелёным из-за наличия кода или красивого демо.

### Этап 0 Подготовка и согласование

Результат: сохранённый источник, матрица требований, contracts v1, ручные fixtures и проверенный Linux execution path.

При старте были macOS arm64, Python3.12.1 и uv без Docker. Теперь подготовлены Python3.11.15 и отдельный Colima/Docker profile; G0 проверен реальными Linux probes, затем интегрирован со сквозной бухгалтерской задачей. Базовый проект security profile уточнён: trusted parent также получает KILL для остановки worker после смены UID. У worker capabilities пусты.

**Gate G0:** public/protected schemas и семантика согласованы; fresh isolated worker обменивается JSON; попытки получить sentinel через paths/env/FD/proc или запись в private store неуспешны; неподдерживаемый secure mode завершает проверку ошибкой. Если chroot/seccomp не реализуемы в доступном runner, выбираем проверяемую альтернативную Linux topology до заявления о security. Это техническая зависимость, не причина останавливать работу над чистыми модулями данных/оценки.

### Этап 1 Минимальный сквозной тренажёр

Параллельно после contracts v1:

- данные: clean native-currency world, duplicate и allocation, независимая сверка;
- среда: reset/step, tools, stable pagination, Decimal calculator, counters;
- оценивание: submit schema, matcher, common reward, clean/false/duplicate fixtures.

Основной агент соединяет части и запускает чистый и ошибочный эпизоды от генерации до trajectory/report. Обычная policy идёт только через tools. Security spike интегрируется в тот же путь, без второго «официального» оценщика.

**Gate G1:** hand-checked суммы, TP/FP/FN/reward совпадают с расчётом; clean silence и false accusation различаются; invalid submit и budget boundaries верны; одинаковые входы дают одинаковую каноническую траекторию.

### Этап 2 Полный обязательный набор

Добавляем Decimal FX и cutoff, две реальные комбинации, законные повторяющиеся суммы, split/partial payments, документированный refund. Каждый public факт достаточен для своего proof, missing links не угадываются. Расширяем supported sizes и public export; все четыре reference adapters готовы.

Refund fallback без новых top-level tables: credit-note в invoices с публичной ссылкой в line_items, signed total/payment по явной policy, nonnegative debit/credit ledger. Это открытая схема v1; при несовместимости с официальной схемой меняем представление, сохраняя документированную законность возврата.

**Gate G2:** все четыре типа включаются независимо; оба interaction сценария проверены целиком; каждый positive issue ненулевой и доказуем; independent checker подтверждает полноту gold и чистоту controls; maximum preset читается <=50 inspections. Замораживаем совместимую schema/config version до финальных exports.

### Этап 3 Соответствие, устойчивость и диагностика

Выполняем весь mapped набор contract/scorer/data/security tests, stateful actions, replay, трансформации ID/порядка и новые комбинации известных параметров. Запускаем четыре политики на одной локальной контрольной серии из20 новых эпизодов:5 clean,5 interacting,10 остальных. Отдельно20 paired checks. Это репетиция структуры проверки, не официальный hidden set.

**Gate G3:** каждый `REQ-*` имеет реализованный тест/артефакт; нет известного необъяснённого нарушения; соблюдены privacy и immutable outcome; представлены episode-level P/R/F1/return/steps/time, invalid/truncated rates и ресурсы. Измеренные проблемы сохраняются с seed/config только в trusted artefacts и воспроизводимым минимальным примером для разработчика.

### Этап 4 Отличительные доказательства

При готовом обязательном ядре:

- парные correct/mutated случаи с независимым пересчётом зависимых эффектов;
- сценарий взаимной маскировки итогов;
- test-only mutants оценщика: игнорируется currency, принимается часть proof, повтор считается TP, исчезает FP/FN, неверна clean F1 или стоимость шагов;
- краткие карточки документов/расчёта; static post-run viewer при оставшемся времени.

**Gate G4:** корректный evaluator проходит проверки; каждый неэквивалентный заранее выбранный mutant обнаружен тестом либо явно отмечен survivor с объяснением и исправлением пробела; demo использует реальные выполненные траектории. Production scorer не содержит доступного агенту «ослабить проверку». Viewer не входит в критический путь CLI.

### Этап 5 Автономная сдача

Фиксируем версии/лицензии, экспортируем проверенные10 train+10 dev, trajectories и design note, собираем release для согласованной Linux architecture. Предпочтительный offline путь: заранее построенный image tar -> docker load -> run `--pull=never --network=none` на целевых ресурсах. Build preparation может использовать сеть заранее, official runtime — нет.

Единая команда `./scripts/verify --offline` реализована: выполняет тесты и текущий acceptance protocol, пишет JSON/человекочитаемый отчёт и возвращает nonzero при провале. При отсутствии Docker/sandbox она даёт явную ошибку, а не успешный cooperative результат. Чистая передача release без подготовленного image cache остаётся проверкой G5.

**Gate G5:** чистый offline запуск из передаваемого пакета без image cache/сетевой загрузки; проверены все материалы сдачи, размер, CPU/RAM/episode time; отчёт и короткое демо соответствуют фактической версии. Официальная подача пользователем/авторизованным каналом выполняется отдельно, когда канал организаторов известен.

## Задачи и зависимости

Все задачи, кроме отмеченных «готово», запланированы. Владельцы означают зоны следующей реализации, а не утверждение, что код уже создан.

| Задача | Владелец | Зависит от | Проверяемый результат |
| --- | --- | --- | --- |
| P00 Источник и snapshot | Интеграция | — | **Готово:**6 исходных PNG, provenance, транскрипция |
| P01 Требования и общий план | Все, интеграция | P00 | **Готово:** матрица 64 REQ, согласованный план и взаимная проверка |
| P02 Schemas/policy/type rules | Все, интеграция | P01 | **Готово v0.2:** native0.1/0.2 и multi0.2, public/private schemas,protocol,money/null/ID rules |
| P03 Python3.11/Linux setup и sandbox spike | Среда | P01 | **Готово:** G0 actual Linux probes, fail-closed и execution path |
| P04 Money/time primitives и ручные fixtures | Данные | P02 | **Готово:** minor/Decimal/offset/rounding fixtures |
| P05 Clean world и материализация | Данные | P04 | **Готово:** docs + balanced native journal |
| P06 Duplicate/allocation + initial checker | Данные | P05 | **Готово:** positive/clean controls,proofs,truth-blind completeness |
| P07 Lifecycle/tools/calculator | Среда | P02 | **Готово:** JSON contract,100-step lifecycle,stable pages,safe arithmetic |
| P08 Matcher/common reward | Оценивание | P02,P04 | **Готово:** counts,clean/proof/currency/tolerance/limits,attack regressions |
| P09 Первый полный запуск | Интеграция | P03,P06,P07,P08 | **Готово G1:** generate -> tools -> submit -> score -> trajectory, offline |
| P10 FX/cutoff | Данные | P06,P08 | **Готово G2:** все4 семейства, FXdate/rate, periods, открытый контракт0.2 |
| P11 Interactions/distractors/refunds | Данные | P10 | **Готово G2:** обе зависимые пары, foreign/refund controls, полный blind checker |
| P12 Secure runner/train-closed/logs | Среда | P03,P07,P08 | **Готово G2:** fresh isolated workers,masked feedback,public/private outputs,19 probes |
| P13 Четыре policy adapters | Оценивание | P09,P10,P12 | **Готово G2:** reference random/simple/mark-all/evaluator-oracle;45 diagnostic runs, код организаторов пока не предоставлен |
| P14 Public10+10 fixtures и supported sizes | Данные, интеграция | P11,P12 | **Реализовано G3:** frozen20 fixtures, hashes, exact regeneration, protected round-trip и independent validation |
| P15 Contract/attack/stateful suite | Оценивание, среда | P11,P12,P13 | **Реализовано G3:** [трассировка64 REQ](CONFORMANCE.md), contract/scorer/data/security/replay regressions; незавершённые release материалы отмечены явно |
| P16 Replay,20 paired и20 holdout rehearsal | Все | P13,P14,P15 | **Реализовано G3:**80 policy runs,20 paired checks,80 fixed-action replay и fresh-worker repeat; [результат](STAGE3_STATUS.md) |
| P17 Resource/offline measurements | Среда | P12,P16 | **Готово G5:** whole-container cgroup peak283070464bytes,2CPU/4GiB, empty-store transfer146.35с, exact archive72.27MB |
| P18 Counterfactuals/showcase | Данные, оценивание | P11,P16 | **Реализовано G4:** одинаковые550000₸ при двух ошибках, отдельные исправления, обе dependent repairs,9 книг/27 executions |
| P19 Scorer mutation checks | Оценивание | P08,P15 | **Реализовано G4:** frozen12 AST faults, baseline и настоящие detection tests; runtime/import/timeout не считаются kills |
| P20 Post-run cards/viewer | Интеграция | P16,P18,P19 | **Реализовано G4:** offline HTML, карточки источников,27 logical replay, hashes, XSS/path checks; desktop/mobile QA |
| P21 Release/docs/design note | Интеграция, среда | P14,P16,P17 | **Готово G5:** sealedarchive+image,124-component/165-text notices,design/demo/release notes,inventory,one-command,fullopensample |
| P22 Final rehearsal/demo | Все | P18,P19,P21 | **Готово G5:** exact archivea541…→freshindependentdaemon0/0/no routes→826tests/47checks; cleanup+existingengine preserved |

Критический путь: P02 -> P04–P09 -> P10–P16 -> P17/P21 -> P22. P03 идёт сразу параллельно и должен завершиться до утверждения secure сквозного пути. При риске дедлайна сокращаем P20 и художественное оформление; сохраняем все mandatory requirements, независимую проверку и корректность.

## Рабочий календарь

Это ориентир порядка, не обещание длительности до появления кода и первых измерений.

| Дата, Астана | Целевой этап |
| --- | --- |
| 19 ноября | Источник/план, schemas, ранний Linux spike, минимальный сквозной эпизод |
| 20 ноября | Четыре типа, оба взаимодействия, чистые случаи, полная независимая сверка |
| 21 ноября | Изоляция, атаки, все reference policies, replay/paired/holdout, exports |
| 22 ноября | Заморозка функциональности, offline rehearsal, материалы и демо; запас до23:59 |

При задержке показываем фактический пройденный gate и конкретную оставшуюся работу. Возможность финала28–29 ноября предварительная и не влияет на объём текущей сдачи.

## Координация реализации

- Общие schemas/policy и новые interfaces интегрирует основной агент после сверки владельцев. Остальные агенты работают в своих модулях и тестах, не перезаписывают shared files параллельно.
- Каждая задача начинается с `REQ-*` и минимального ручного примера. Заканчивается проверяемым результатом и интеграционным smoke run.
- Data/evaluation взаимно проверяют суммы, completeness и proof sets; environment/evaluation — counters,terminal rules и visibility.
- Ошибка в интеграции исправляется в источнике правила, а не вторым несовместимым scorer или специальным исключением для demo.
- Набор локальных проверок отделён от официальных. Перед финальной сдачей сохраняем versioned run manifest и результаты целиком.

## Материалы сдачи и честные утверждения

- Исходники генератора/среды/checker/scorer с лицензией и pinned dependencies.
- README с контейнером, offline preparation и единой командой проверки.
- Схемы, policy, открытые правила normalization/tolerance/matching.
- Проверенные10 train+10 dev episodes, четыре reference adapters, тесты.
- `trajectories.jsonl` обязательной структуры, private evaluator ledger отдельно.
- P/R/F1/common return/steps/time и failure analysis, с указанием split и версии.
- Design note, ограничения модели, threat model, ресурсы и независимая верификация.
- Демо: скрывающие друг друга ошибки -> документы/расчёт -> correct control -> ID/order invariance -> обнаруженная test-only поломка scorer.

Пока не заявляем реальные аудиторские гарантии, улучшение модели, абсолютную sandbox security или официальный полный балл. Oracle показывает верхнюю границу проверяющего; ordinary policy metrics показываем отдельно. Собственные reference policies не выдаём за ещё не предоставленный код организаторов.

## Вопросы, которые могут изменить совместимость

| Вопрос | Рабочее решение до уточнения | Когда пересматривать |
| --- | --- | --- |
| Полные фиксированные проверки и exact schemas/type names | Публикуем versioned v1 и64 REQ mapping | Сразу при получении официального файла |
| Дополнительные поля, signed refunds,line_items/invoice_refs | Сохраняем обязательные поля, все расширения публичны; conservative fallback в data-design | До заморозки final exports |
| Смысл суммы/affected IDs/tolerance | Объявленные base-currency conditional rules | До official adapter integration |
| Inspection/submit100/timeout reward | Общая формула, правила таблицы v1 | При официальном численном примере |
| Closed scalar feedback и end-of-run | Нейтральный0,report после всего run | До окончательного wire adapter |
| Linux architecture/capabilities и1GB scope | Ранний secure spike; учитываем весь release | До сборки final image |
| Канал сдачи | Неизвестен; материалы готовим локально | Когда объявят организаторы |

## Проверка плана

Три направления проверили общий документ и матрицу требований. Существенных противоречий не осталось; замечание о truth-blind восстановлении ошибок принято в интерфейс checker. Проверены ссылки,64 REQ IDs, зависимости задач и целостность6 исходных снимков. Реализованы G0/G1/G2/G3/G4; общие offline Linux-прогоны, [текущий отчёт](STAGE4_STATUS.md) и остающиеся требования разделены явно.

G3 добавил10 train+10 dev и20+20 protocol; G4 — [mutation/showcase evidence](STAGE4_CONTRACT.md); G5 — имена, sealed release,notices,design/demo notes и чистую offline-передачу. [Статус G5](STAGE5_STATUS.md) содержит фактическую проверку и checksum; код и пакет не изменялись после clean run. Дальнейшие действия зависят от официального канала/adapters или нового запроса пользователя; ни подача, ни другой target здесь не подразумеваются.
