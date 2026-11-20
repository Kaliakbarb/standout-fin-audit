# Четвёртый этап: проверяемые поломки и маскирующиеся ошибки

Версия программы — **0.4.0**. Финансовая схема остаётся0.2.0, открытый корпус и его планы —0.3.0, wire protocol —1.0. Stage4 сохраняет все проверки G3 и добавляет два отдельных свидетельства: тесты распознают намеренно повреждённый оценщик, а ordinary worker находит ошибки при совпадающих агрегатах.

## Что демонстрирует сценарий

В вымышленной компании есть два счёта:50 000₸ и1 000USD. По опубликованной политике долларовый счёт должен оцениваться по500₸/USD. Корректная выручка и дебиторская задолженность от признания этих счетов —550 000₸.

Две независимые ошибки скрывают друг друга в этом показателе:

| Изменение | Эффект на выбранный итог |
| --- | ---: |
| Повторно признать счёт50 000₸ | +50 000₸ |
| Применить450 вместо500 к счёту1 000USD | −50 000₸ |
| Обе ошибки вместе | 0₸ |

При обеих ошибках итог снова550 000₸. Это не делает книгу корректной: checker и агент должны отдельно доказать duplicate и FX. Сумма абсолютных эффектов на этот конкретный показатель —100 000₸; она не называется универсальным финансовым ущербом или суммой всех type-specific issues.

Четыре книги `clean`, `duplicate_only`, `fx_only`, `masked_both` сохраняют те же исходные документы, FX-таблицу и политику. Их проводки и gold независимо проверяются; каждый журнал сбалансирован. Исправление одной ошибки раскрывает оставшееся расхождение с ожидаемым итогом.

Дополнительно два counterfactual repair показывают зависимость ошибок:

- `allocation_fx` → `allocation_fx_repaired`: исправление курса устраняет FX issue и пересчитывает сумму оставшегося allocation issue.
- `allocation_cutoff` → `allocation_cutoff_repaired`: исправление даты устраняет cutoff issue, оставляя доказуемую ошибку распределения того же платежа.

После этого меняются все ID и порядок таблиц `masked_both`; смысл находок должен сохраниться. Всего9 книг. На каждой выполняются реальный `simple`, отрицательный контроль `silent` и trusted `oracle`:27 executions,18 ordinary workers и9 evaluator-only oracle executions. Предположения о точности simple проверяются фактическим результатом.

## Мутации оценщика

До первого mutation experiment зафиксирован [план12 ошибок](../configs/scorer-mutations.json). Проверки запускаются против временных AST-копий `evaluation.py`, по одной поломке за раз. Используется реальный scorer test suite с заранее указанными detection selectors; у каждой копии проверяются путь импорта и SHA-256. Рабочий оценщик не получает новых параметров ослабления проверки.

Выбраны12 неэквивалентных поломок: currency, полный proof, one-to-one matching, FP/FN в F1, clean F1, стоимость inspection, greedy matching, сумма, граница tolerance, exact affected-ID set и invalid-submit reward. Это ограниченный опубликованный набор типичных ошибок, а не доказательство обнаружения любого возможного дефекта программы.

Baseline без изменений обязан пройти. Если baseline не проходит, все12 остаются в отчёте `not_run`; список не сокращается ради процента успеха. Мутация считается обнаруженной только при `AssertionError` в call phase настоящего detection test. Import/setup/runtime failure, crash, timeout, превышение stdout и отсутствие теста не засчитываются как semantic kill. Survivor и invalid делают acceptance неуспешной.

Отчёт хранит полный заранее выбранный список, выбранные тесты, команды, hashes исходника/изменения, exit codes, классификацию, время и ограниченный diagnostic output. Временные копии и процессы очищаются; итог публикуется в новом каталоге.

```bash
./scripts/dev python scripts/mutation_check.py --output artifacts/mutation-check
```

Это developer-only проверка. Agent jail содержит только stdlib и worker; инструмента изменения оценщика у агента нет.

## Демо и общий запуск

Короткое демо без заявления о проверенной OS-изоляции:

```bash
./scripts/dev fin-audit showcase --profile cooperative --output artifacts/showcase-demo
```

После завершения появляется `artifacts/showcase-demo/showcase.html`. Отчёт самодостаточен, работает без сети, JS-библиотек и сервера. Он показывает финансовый расчёт, документы/проводки из proof sets и реальные оценки ordinary simple, silent и privileged oracle.

Полная проверка в поддерживаемом Linux secure profile:

```bash
./scripts/prepare
./scripts/verify --offline
```

Подготовка image выполняется заранее. Проверка использует `--pull=never --network=none`,2 CPU,4GiB и read-only root filesystem. Она запускает общий pytest, полный G3 regression, showcase и весь declared mutation suite; любой провал возвращает nonzero. Старые `--stage 1/2/3` доступны отдельно.

## Post-run граница и целостность

Showcase собирается в private staging directory. Пока ordinary workers работают, карточки анализа и оценки не публикуются. После всех27 executions записываются analysis, summary, real trajectories и private evaluation/manifest; новый completed leaf публикуется атомарно. При прерывании остаётся protected FAILED receipt, а не успешный итог.

Renderer принимает только completed/passed receipt. Он проверяет hashes пяти обязательных файлов, source packages, canonical book/gold digests и соответствие cards реальным issues. Значения HTML экранируются. Он не принимает произвольный `completed:true` как достаточное свидетельство, не делает записи в исходные книги и не отдаёт live private feedback агенту.

Структура полного результата:

```text
run/
  regression/       открытый корпус и полный G3 protocol
  showcase/         9 экспортированных книг,27 trajectories,analysis/evaluation/manifest,COMPLETE
  mutations/        baseline,12 mutants и итоговая классификация
  showcase.html     проверенный post-run отчёт
  summary.json      общие проверки всех трёх направлений
  run-manifest.json hashes входов и runtime
  COMPLETE.json     completed/passed и hashes итогов
```

Фактические количества, measured failures и ресурсы фиксируются в [статусе G4](STAGE4_STATUS.md). Официальный балл, сложность для произвольной модели и реальные аудиторские гарантии из этих локальных результатов не следуют. Финальная передача release и проверка чистого offline-хоста остаются G5.
