# Статус второго этапа финансового тренажёра

**G2 завершён**, версия 0.2.0,19 ноября 2026. Реализованы все четыре семейства ошибок, два зависимых сценария, корректные валютные случаи и документированные возвраты. Общий offline-прогон выполнен в Linux с изолированным worker.

## Подтверждённые результаты

Команда: `./scripts/verify --offline --stage 2`.

- **529 тестов прошли**,0 failures/errors/skips; pytest22.41 с.
- **23 из 23** сквозных acceptance checks.
- **19 из 19** проверок Linux sandbox.
- **9 сценариев ×5 политик =45 прогонов**, плюс отдельный repeat и isolation probe.
- Ordinary simple worker во всех девяти сценариях получил точные находки: FP=FN=0,F1=1.

| Сценарий | TP / FP / FN simple | Return | Шаги |
| --- | --- | ---: | ---: |
| Чистая мультивалютная компания с возвратом | 0 / 0 / 0 | 0.986 | 8 |
| Только дублирование | 1 / 0 / 0 | 0.984 | 9 |
| Только распределение платежа | 1 / 0 / 0 | 0.984 | 9 |
| Только FX | 1 / 0 / 0 | 0.986 | 8 |
| Только cutoff | 1 / 0 / 0 | 0.986 | 8 |
| Все четыре семейства | 4 / 0 / 0 | 0.984 | 9 |
| Распределение + FX одного платежа | 2 / 0 / 0 | 0.986 | 8 |
| Распределение + cutoff одного платежа | 2 / 0 / 0 | 0.984 | 9 |
| Неверная FX-дата recognition | 1 / 0 / 0 | 0.986 | 8 |

Во всех девяти сценах mark-all получил return−1; evaluator-only oracle —1. Oracle показывает верхнюю границу проверяющего, не способности обычного агента. Эти значения — локальная диагностика, не официальные баллы и не результат обучения модели.

## Что проверено

- Четыре independently configurable типа: duplicate,allocation,FX,cutoff; точные суммы, affected IDs и полные evidence sets.
- FX по неправильной дате и неправильному числовому курсу, recognition и receipt; денежные разрядности 0/2/3, дробные курсы, точные большие integer-суммы.
- Корректные realised FX gain/loss, split/partial/unpaid/repeated-sum случаи и native credit-note/refund без ложных обвинений.
- Условная сумма allocation меняется при исправлении FX; исправление cutoff оставляет allocation видимым. Обе пары могут сосуществовать на разных платежах.
- `[start,end)` в timezone компании: обе стороны границ и эквивалентные instants с другими offsets.
- Полный independent checker: source/metadata/counterparts/balance, все issues, неизвестные и повреждённые связи; он не получает truth/provenance.
- До 2000 ledger rows,200 документов,200 FX rows; большой clean-profile прочитан ordinary worker за≤50 inspections.
- Реальный JSONL worker/CLI, queryable FX evidence IDs, CSV/JSON round-trip signed sources и metadata, strict submit и неизменённая common reward.
- Exact replay, ID rename/order checks, closed feedback, reserved private-key guard, старые native0.1.0 exports.
- Linux resource/isolation gates и регрессии первого этапа в одном общем запуске без сети.

На default seed19 clean-case содержит 200 проводок,45 документов и 8 FX rows. All-four case —207 проводок с теми же 45 документами и 8 FX rows. Counts могут меняться с profile; это наблюдаемый объём данных, не закрытая разметка.

## Исправленные проблемы

Независимый review обнаружил order-dependent collision journal ID с ещё не зарегистрированным row ID; guard теперь проверяется после регистрации всех записей. Добавлены permutation regressions.

Наследованный native checker принимал exponent2.0 как integer2 через равенство Python-словарей. Теперь значение проверяется строго как int; дополнительно согласуются manifest/policy versions.10 regression cases подтверждены общим прогоном.

Обязательный boundary target резервируется до случайного выбора FX-мишеней, чтобы допустимые сочетания не отклонялись случайно. Округление проверяется независимыми ручными примерами: механическое масштабирование уже округлённых проводок не используется как финансовый эталон.

## Артефакты проверки

- [JUnit529 тестов](verification/stage2/tests.xml).
- [Сводка 23 checks,19 probes и 45 прогонов](verification/stage2/summary.json).
- [Манифест версии, образа, ресурсов и SHA-256 исходников](verification/stage2/run-manifest.json).
- Полные generated документы, траектории и private evaluation: `artifacts/secure-sa4hg6o1/run/`.

Проверенная платформа: **Linux ARM64,Python3.11.15**. Образ `genesis-fin-audit:stage2`, ID`sha256:c5ffcfcb799abde1a5e2631f50806d1eb9be08e1f2360a34dc62180d35f68335`, Docker size69 272 649 bytes. Заданы 2 CPU,4 GiB total RAM,64 PIDs,network none,readonly rootfs. Переносимость Linux/amd64 требует отдельного прогона.

## Граница завершённого этапа

Модель ограничена [публичным контрактом 0.2](STAGE2_CONTRACT.md): базаKZT, одна документированная invoice-reference у платежа, foreign settlement в валюте invoice, native refunds, atomic receipt без late clearing. Это программный тренажёр; реальные аудиторские гарантии не заявляются.

Следующие этапы полного кейса: финальные 10 train+10 dev, замороженный локальный 20-эпизодный и 20-парный протокол, measured failure/resource analysis, mutation report, демонстрация и окончательный offline release. Все 64 требования полной сдачи пока не объявляются закрытыми. [Исторический первый этап](STAGE1_STATUS.md) сохранён отдельно.
