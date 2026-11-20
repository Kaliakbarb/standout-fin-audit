# Статус третьего этапа финансового тренажёра

**G3 завершён**, версия программы0.3.0,19 ноября2026. Сохранены10 train+10 dev, выполнены20 контрольных эпизодов для четырёх политик,20 парных проверок и80 точных replay. Финальная проверка прошла в Linux ARM64 без сети, на исправленной версии replay.

## Результат общей проверки

Команда: `./scripts/verify --offline`.

- **620 тестов прошли**,0 failures/errors/skips; pytest96.70с.
- **24/24** acceptance checks третьего этапа.
- **19/19** фактических проверок sandbox.
- Открытый корпус: **10 train+10 dev**, каждый эпизод проверен по hashes, полной регенерации, protected export/load и независимому checker.
- Контрольная серия: **20 эпизодов ×4 политики =80 прогонов**, состав5 clean+5 interacting+10 other.
- **20/20 пар** с заменой ID и порядка прошли; соответствующие полные, частичные, ложные и повторные ответы сохранили оценку при равном бюджете.
- **80/80** исходных trajectories точно воспроизвелись на заново сгенерированных книгах; дополнительный fresh-worker repeat также точен.

Это локальная репетиция опубликованной структуры проверки, не официальный hidden set и не заявленный балл жюри.

## Диагностика политик

Все четыре политики получили одни и те же20 эпизодов с56 ошибками суммарно. Обычные политики выполнялись через public tools в отдельных изолированных workers; oracle работает только у trusted evaluator.

| Политика | TP / FP / FN | Средний F1 | Средний return | Средние действия | Среднее время эпизода |
| --- | --- | ---: | ---: | ---: | ---: |
| Simple, ordinary worker | 56 /0 /0 | 1.00 | 0.9572 | 22.4 | 0.218с |
| Random, fixed seed | 0 /0 /56 | 0.25 | −0.5060 | 13.0 | 0.210с |
| Mark-all,30 обвинений | 0 /600 /56 | 0.00 | −1.0000 | 22.4 | 0.224с |
| Oracle, privileged | 56 /0 /0 | 1.00 | 1.0000 | 1.0 | 0.025с |

Micro P/R/F1 simple и oracle равны1. Macro P/R равны0.75: на пяти чистых эпизодах при пустом ответе P=R=0, но F1=1 по опубликованному правилу. У random средний F1=0.25 складывается из пяти правильных пустых ответов на clean и пятнадцати пропусков ошибок; это не обнаружение четверти ошибок.

Во всех80 основных прогонах invalid-submit и truncation rates равны0. Ложные обвинения на чистых книгах: simple0%, random0%, oracle0%, mark-all100%. Отдельные P/R/F1,return,steps,time по каждой книге, типу ошибки и cohort сохранены в [JSON-сводке](verification/stage3/summary.json). Высокий результат скриптового simple не доказывает сложность задачи для модели или эффект обучения.

## Измеренные ресурсы

Проверенный профиль: Linux ARM64,Python3.11.15;2 CPU,4GiB RAM,64 PIDs,network none,readonly rootfs,tmpfs256MiB. Agent address-space limit512MiB, episode timeout60с. Реальные параметры работающего контейнера сохранены отдельным inspect receipt в манифесте.

| Измерение | Значение и граница |
| --- | --- |
| Полный protocol20+20 | 32.03с, включая preflight/runs/pairs/replay и основную сериализацию |
| Фаза acceptance целиком | 41.11с, включая открытые наборы и sandbox probe; pytest отдельно |
| Максимум одного из80 эпизодов | 0.413с |
| Максимум действий / inspections | 36 /35, ниже100 и ориентировочных50 для полного сканирования |
| Supervisor peak RSS | 114688000bytes,109.4MiB; process-lifetime high-water |
| Largest reaped child peak RSS | 114688000bytes,109.4MiB; lifetime high-water, не сумма процессов и не активная память worker после exec |
| Полный protocol output | 44691653bytes; книги, единственная копия trajectories и отчёты |
| Docker reported image size | 69831348bytes; это размер image, не окончательного передаваемого архива |

Peak всей process tree/контейнера и cold transfer без image cache здесь не измерены. Linux/amd64 требует отдельного запуска. Эти границы не подменяются заданным лимитом4GiB.

## Проверенные сбои и исправления

Контрольные плохие стратегии дают измеренный результат: random пропускает все56 ошибок, mark-all создаёт600 FP; пустой/случайный ответ не получает преимущество на dirty книгах. Это ошибки стратегий, а не сбои execution core.

В orchestration tests реально моделируются прерывания на вызовах1/80/120/121: завершённого каталога/успешного receipt нет, остаётся protected FAILED diagnostic. Повреждённый корпус, overlap seeds, неправильный cohort, изменение исходников и поломка remapping не становятся успешной проверкой. Внешние timeout/agent-failure/protocol-error/time-limit явно исключаются из fixed-action replay. Брокерные зависания, output floods, неверный submit и100-step exhaustion покрыты общим JUnit.

При независимом review найден и исправлен replay edge case: JSON `null` является настоящим неправильным действием, расходующим один inspection, однако прежняя replay-проверка принимала его за незаписанное внешнее событие. Минимальный пример — clean seed901, действия `[null, valid submit]`. Теперь observations, бюджет, terminal outcome и private evaluation точно воспроизводятся; внешние abort остаются отдельным случаем. [Регрессия](../tests/test_protocol.py) вошла в финальные620 тестов, образ пересобран и общий offline-прогон повторён после исправления.

## Данные, конфигурации и приватность

Открытые книги находятся в [datasets](../datasets/README.md), их [индекс](../datasets/manifest.json) содержит20 разных seeds/configs и SHA-256. Представлены четыре типа, оба взаимодействия, currency exponents0/2/3, FX date/rate, partial/missing allocation и19 книг с документированным refund. Clean/dirty размеры перекрываются, поэтому размер сам по себе не маркирует отсутствие ошибок.

Планы train/dev и local holdout заморожены в [configs](../configs/holdout-plan.json). Actual holdout seeds и размеры выводятся из private master; все20 сочетаний структурно отличаются от открытых даже без учёта размеров. Master receipt повторно используется при следующем `scripts/verify`. Private seeds/configs не перенесены в публичный verification archive и не попадают в agent jail.

Протокол делает80 cohort executions,40 paired oracle executions и1 fresh repeat:121 вызов `run_episode`,61 ordinary worker и60 privileged oracle executions. Дополнительно выполняются80 логических replay и160 прямых scorer evaluations80 соответствующих ответов. Secure CLI отдельно запускает один isolation probe. Эти категории не складываются в выдуманное число независимых скрытых эпизодов.

`trajectories.jsonl` связан с компактным private evaluation через `trajectory_run_id`; дублирование больших observations удалено. Отчёты публикуются после всей серии атомарно; private manifest/evaluation имеют mode0600. Полный [протокол и команды](STAGE3_PROTOCOL.md) описывают границы replay, metric conventions и protected storage.

## Артефакты

- [JUnit620 тестов](verification/stage3/tests.xml).
- [24 checks,19 probes,80 результатов,20 pairs,80 replay и агрегаты](verification/stage3/summary.json).
- [Манифест runtime, реального контейнера, image и hashes](verification/stage3/run-manifest.json).
- [Сборка отдельного agent runtime](verification/stage3/sandbox-build.json) и [hash-pinned test dependencies](verification/stage3/requirements.lock.txt).
- Полные книги, JSONL, protected evaluation/seed receipts: `artifacts/secure-izv2he_5/run/`.

Проверенный image: `genesis-fin-audit:stage3`, ID`sha256:3e61436d419b49dd8e58d37eecc94c8beace57f2ac7c7ffa24f179c2f5edafe7`. Все220 host input files совпали с проверенным образом. Ещё два файла внутри image созданы build script — generated lock и sandbox-build receipt; они отдельно извлечены без сети и сверены с recorded hashes. Исходники во время и после прогона не менялись.

## Оставшаяся работа полного кейса

[Все64 требования](CONFORMANCE.md) связаны с кодом, тестами или конкретной оставшейся границей. Финансовая модель ограничена [контрактом0.2](STAGE2_CONTRACT.md); схема книг не изменена ради G3. Генератор пока не добавляет отдельное human-readable company-name field, хотя presentation transform поддерживает такие labels, если они присутствуют.

Следующий этап — test-only mutation report оценщика, показательный сценарий/короткая design note и окончательный release transfer rehearsal с inventory и проверкой передаваемого пакета. G3 не объявляет эти задачи завершёнными. Исторические [G1](STAGE1_STATUS.md) и [G2](STAGE2_STATUS.md) сохранены отдельно.
