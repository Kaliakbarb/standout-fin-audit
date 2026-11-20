# Протокол третьего этапа

Версия программы и планов — **0.3.0**. Схема бухгалтерских книг остаётся **0.2.0**, wire protocol — **1.0**; старые native books0.1.0 поддерживаются. Финансовая семантика второго этапа не меняется.

## Открытые наборы

В [datasets](../datasets/README.md) сохранены10 train и10 dev эпизодов. [Индекс](../datasets/manifest.json) содержит seed, полный config, table/issue counts и SHA-256 каждого файла. [План](../configs/corpus-plan.json) и схема индекса версионированы. Train/dev имеют разные seeds и configurations; clean controls, четыре типа, обе зависимые пары, refund и foreign exponent0/2/3 представлены явно.

Это открытые учебные данные: человек может читать их gold и provenance. Worker не получает файловый доступ к корпусу или исходникам программы. Даже при открытой разметке агент действует через те же public tools.

`verify_corpus` проверяет индекс, точный набор файлов, hashes, повторную генерацию, round-trip и полную независимую сверку. Изменение gold или книги, удаление файла и unsafe path приводят к ошибке. Git не сохраняет mode0600/0700: для проверки открытых fixtures создаётся временная защищённая копия; исходники не chmod-ятся и строгий runtime loader не ослабляется.

```bash
./scripts/dev python -m fin_audit.cli datasets verify --directory datasets
./scripts/dev python -m fin_audit.cli datasets export --directory artifacts/open-copy
```

Export принимает только новый каталог и не заменяет существующий.

## Локальная контрольная серия

Четыре политики проходят одни и те же20 новых эпизодов:5 clean,5 interacting и10 other. Структурные сочетания известных ошибок опубликованы в [holdout plan](../configs/holdout-plan.json). Реальные128-bit seeds и размеры выводятся из private master seed; configurations отличаются от открытых наборов. Диапазон размеров общий для всех трёх групп, чтобы фиксированный размер не служил ярлыком чистоты.

Контрольный набор закрыт относительно изолированного worker. Это не официальный hidden set организаторов, и опубликованный исходный код не претендует скрывать структурный план от читателя репозитория. Никакого обучения на этих20 примерах не выполняется.

Обычные `random`, `simple` и `mark_all` получают fresh subprocess на каждый эпизод. `random` использует фиксированную policy seed; `simple` сверяет доступные записи; `mark_all` — отрицательный контроль с30 обвинениями. `oracle` исполняется только у trusted evaluator, берёт gold и показывает верхнюю границу корректности оценщика. Его результаты отделены от способностей ordinary policies.

Перед запуском каждый эпизод проходит полный checker без truth/provenance, protected export/load и проверку полных proof sets. В closed mode каждая строка wire имеет reward0/info{}; истинная оценка хранится у evaluator. Отчёты публикуются только после завершения всей серии, пар и replay. Код организаторов пока не предоставлен, поэтому это наши reference adapters.

## Двадцать пар и повторение

Для каждого контрольного эпизода создаётся новая презентация: меняются episode/entity/business/document/row/FX/journal IDs и credit-note references, перемешиваются все четыре таблицы. Суммы, курсы, даты, валюты и accounting policy сохраняются. `policy` остаётся зарезервированным ID. Если вход содержит human display labels, они также преобразуются; базовый генератор пока использует entity IDs без отдельного поля имени компании.

Новый gold пересчитывается checker из преобразованной публичной книги. Его семантика должна совпасть с remapped исходным gold; issue IDs пересчитываются канонически, private provenance получает историю и новый public hash. ID map не отдаётся агенту.

Каждая пара проверяет:

- исходный и преобразованный oracle через общий lifecycle;
- четыре соответствующих ответа: полный, пустой, частичный и повторный/ложный;
- одинаковые TP/FP/FN/P/R/F1/return при равном бюджете7 inspections для прямых scorer fixtures;
- сохранение смысла unknown IDs, malformed полей и неправильных ответов — remapper не исправляет их.

Итого20 пар,40 oracle executions и160 прямых оценок80 соответствующих ответов. Это не160 дополнительных эпизодов. Перестановка может менять порядок исследования обычной политики; универсальная неизменность её числа запросов не утверждается.

Все80 исходных policy trajectories повторяются на заново сгенерированной книге: сравниваются канонические observations/actions/rewards/flags и private evaluation. Это80 логических replay, без запуска ещё80 workers. Время исполнения исключено из сравнения. Внешний timeout не подменяется успешным replay: он делает соответствующую проверку неуспешной. Дополнительно ordinary `simple` повторяется в свежем worker один раз.

Точные счётчики протокола:80 cohort executions +40 paired oracle executions +1 policy repeat =121 вызов `run_episode`; из них61 ordinary subprocess и60 privileged oracle executions. Secure CLI отдельно выполняет один sandbox probe. Открытый корпус не прибавляется к этим20 контрольным случаям.

## Команда и артефакты

```bash
./scripts/prepare
./scripts/verify --offline
```

Подготовка может использовать сеть; проверка использует заранее собранный образ `genesis-fin-audit:stage3` с `--pull=never --network=none`. `--stage 1` и `--stage 2` сохраняют прежние regression batches. Docker context выбирается явно без переключения глобального context. Неподдерживаемая sandbox не заменяется cooperative режимом.

По умолчанию host wrapper атомарно создаёт и затем повторно использует gitignored `artifacts/.protocol-master-seed.json` с mode0600. Это фиксирует одну серию для последующих проверок. Другой private receipt задаётся `--seed-file /path/to/private.json`. Seed не печатается и не попадает в worker argv/env/jail.

Для разработки без заявления об OS isolation:

```bash
./scripts/dev python -m fin_audit.cli verify --stage 3 --profile cooperative --master-seed 123 --output artifacts/g3-local
```

Явный `--master-seed` повторяет серию. Без него прямой CLI выбирает новый seed и сохраняет его в protected receipt. Публичный пример123 предназначен для отладки, не для утверждения о закрытости.

В корне результата находятся exported open corpus, summary и source/runtime manifest. В защищённом `protocol/`:

| Файл | Содержание |
| --- | --- |
| `episodes/`, `paired_episodes/` | Исходные и преобразованные книги, private части защищены |
| `trajectories.jsonl` | Реальные121 траектории с episode_id,step,action,observation,reward,terminated,truncated; reward на wire маскирован |
| `evaluation.json` | Истинные оценки и equivalence fixtures; mode0600 |
| `run-manifest.json` | Реальные master/episode/presentation seeds, configs, hashes, runtime; mode0600 |
| `summary.json` |80 episode-level результатов, policy/type/cohort aggregates,20 pairs,80 replay |
| `COMPLETE.json` | Завершение серии, отдельный passed-флаг и hashes четырёх отчётов |

Протокол собирает результаты в private staging directory и публикует новый leaf атомарно. Исключение оставляет protected `FAILED` receipt вместо успешного результата. Завершённый протокол может иметь `passed:false`; это не успех acceptance. CLI verify и общий script возвращают nonzero при failed check или failed test.

P/R/F1 в aggregate — среднее по подходящим эпизодам; отдельно указаны micro counts. Clean F1=1 при пустом ответе не делает precision/recall равными1. Return всегда считается исходной общей формулой, включая стоимость inspections; отдельной награды «по типу» нет. Invalid submit и truncation, clean false accusations, шаги и время видны отдельно.

RSS — измеренный process-lifetime high-water отдельно у supervisor и крупнейшего завершившегося child, не сумма и не peak всего контейнера. Ограничения2 CPU/4GiB,512MiB address space worker и60s/episode указаны отдельно от измерений. Время whole wrapper включает проверку открытых наборов и sandbox probe, время protocol — его собственную работу. Cold transfer/release test и полный container memory peak остаются отдельной задачей G5.

## Граница выводов

Локальное прохождение доказывает проверенные свойства программы на указанных данных и платформе. Оно не устанавливает официальный балл, сложность задачи для произвольной модели, пользу обучения или реальные аудиторские гарантии. [Трассировка64 требований](CONFORMANCE.md) отмечает незавершённые части. Следом идут test-only scorer mutants, showcase и окончательный release rehearsal.
