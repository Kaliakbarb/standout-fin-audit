# Публичные правила второго этапа

Версия 0.2.0, модель `receivables_multi_v2`. Правила ниже описывают реализованный ограниченный тренажёр, а не универсальные стандарты бухгалтерии. Обязательные поля исходного кейса сохранены; дополнительные поля открыты агенту и версионированы.

## Что видит аудитор

Журнал ведётся в базовой валюте KZT с exponent2. Счета и платежи могут быть в другой объявленной валюте; manifest содержит `currency_exponents`. Курсы в `fx.csv` — положительные десятичные строки, количество major units целевой валюты за 1 major unit исходной. Все курсы синтетические.

Каждая строка ledger дополнительно содержит:

| Поле | Открытый смысл |
| --- | --- |
| `journal_id` | Идентификатор одного атомарного сбалансированного журнала |
| `source_currency` | Валюта исходного документа |
| `source_amount_minor` | Полная исходная сумма документа, со знаком |
| `applied_fx_date` | Дата фактически использованной валютной оценки |
| `applied_fx_rate` | Фактически использованный курс, decimal string |
| `allocated_minor` | Фактически распределённая source-сумма платежа; null для recognition |

Эти значения описывают наблюдаемую запись, без отметки «ошибка» или правильного ответа. У всех строк одного journal согласованы metadata, source, timestamp и направление. Скрытые seed/truth/provenance не входят в public payload; reserved private labels дополнительно отклоняются до наблюдения.

Для FX строк инструмент `get_record("fx", record_id)` возвращает курс по открытому ID. Этот же ID используется как evidence. Полная policy имеет evidence ID `policy` и доступна через `get_document("policy")`.

## Правильная оценка и денежные направления

Корректная дата курса — дата source document в timezone компании: `issued_at` счёта или `timestamp` платежа. Ошибочный ledger timestamp не меняет правильную дату FX автоматически. Для native KZT используется identity rate1, без отдельной строки FX.

Конвертация source minor units:

`round_policy(amount_minor × Decimal(rate) × 10**(base_exponent − source_exponent))`.

Используется явное округление из policy. Binary float не участвует в денежных расчётах. Нативные partial/split payments и разные правильные курсы recognition/payment являются законными случаями.

Для положительного платежа с source-суммой S и фактически распределённой частью A:

- cash = conversion(S, applied payment rate);
- receivable settlement = conversion(A, correct invoice-date carrying rate);
- unallocated = conversion(S−A, applied payment rate);
- realised FX balance = cash − receivable settlement − unallocated; знак определяет gain/loss.

Все debit/credit значения неотрицательны. Корректный FX gain/loss — часть сбалансированного журнала, а не самостоятельное обвинение.

## Канонические находки

Суммы issues — неотрицательные величины в base minor units. Type-specific суммы не складываются в универсальную оценку ущерба.

| Тип | Canonical affected IDs | Сумма |
| --- | --- | --- |
| `duplicate_invoice` | Все primary AR-debit recognition row IDs одного положительного invoice, включая исходную строку | Избыточное gross recognition при одинаковой наблюдаемой оценке копий |
| `payment_allocation` | Точное множество payment_id + invoice_id | Положительный unallocated credit у полностью документированного назначения |
| `fx_conversion` | Один primary row ID: AR для recognition или CASH для receipt | Модуль разницы observed primary valuation и правильной source-date conversion |
| `period_cutoff` | Один primary row ID целого журнала | Модуль наблюдаемой primary-суммы, перенесённой в неверный период |

Полные наборы доказательств: duplicate — policy + invoice doc + все primary recognition IDs; allocation — policy + invoice/payment docs + receipt rows; FX — policy + source doc + primary row + correct FX row; cutoff — policy + source doc + primary row. Каждый реальный proof проверяется на доступность и предел 10 IDs. Partial evidence не засчитывается.

## Граница отчётного периода

Период `[period_start, period_end)`; входящие ISO timestamps имеют offset и сравниваются как instants. Cutoff возникает при различии period membership source event и опубликованного journal. Сдвиг внутри того же периода сам по себе не является cutoff issue.

Документы и журналы соседних дней остаются доступны. Во всех мультивалютных мирах есть корректные пограничные source-события, поэтому сам необычный offset или соседняя дата не служит маркером аномалии. Эквивалентная запись instant с другим offset сохраняет дату FX после пересчёта в company timezone и сохраняет verdict.

## Два взаимодействия

1. **Распределение + FX одного платежа.** Остаток unallocated оценивается при наблюдаемом курсе; исправление FX меняет его сумму. Ручной контрольный пример checker: invoice$1000@500; payment$600, правильный 505, применён 495, allocated$500. Allocation49 500₸, FX6 000₸; после исправления курса allocation50 500₸. Генератор дополнительно проверяется на fractional rates и minor-unit rounding.
2. **Распределение + cutoff одного платежа.** Переносится целый сбалансированный journal у границы периода, а часть назначения потеряна. После исправления даты allocation остаётся доказуемым. Фильтрация только по строкам текущего периода потеряла бы часть необходимых фактов.

Flags взаимодействий выбирают существующие configured issues и не увеличивают их число скрыто. Две пары могут сосуществовать на разных платежах, если явно задано достаточно allocation issues. Количество всех issues ограничено 12; impossible combinations отклоняются.

## Законные возвраты

Native credit note — invoice с отрицательным `total_minor`, отрицательными line amounts и открытым `original_invoice_id`. Исходный invoice положителен и соответствует entity/currency. Отрицательный payment ссылается на credit note. Debit/credit ledger остаются неотрицательными: revenue/AR reversal и исходящий cash journal отражаются отдельными документированными событиями.

Такие возвраты являются корректными контролями. Foreign refunds, cross-currency settlement одного invoice и произвольный late clearing не входят в текущую модель. Invoice FX-мишени генератора остаются неоплаченными, чтобы не вводить неописанные изменения settlement carrying value.

## Независимая проверка и совместимость

Generator сначала строит и проверяет clean world, затем применяет причинные изменения и получает blind-derived issues. Checker получает только public records/policy, проверяет все source relations, metadata, counterparts, balance, FX, период и полноту issues. Затем trusted harness сравнивает derived result с intended changes. Ошибочные/неподдерживаемые связи отклоняются, не называются чистыми.

Исторические native0.1.0 exports остаются читаемыми; новые native и multi exports имеют 0.2.0. JSONL protocol,100-step lifecycle, matcher и common reward сохранены. Новые функции не изменяют правила FP/FN, чистого ответа, дублей или закрытого feedback.

Поддерживаемый upper bound:2000 ledger rows,200 source documents суммарно,200 FX rows. Полный scan занимает не более 40+5+4 страниц (policy уже доступна при reset), оставляя место для submit в 100 действиях.
