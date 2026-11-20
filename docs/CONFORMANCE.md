# Трассировка требований релиза 0.6.0

**Обновление 21 ноября, команда Standout:** чистая передача сохранённого amd64 ZIP завершена: [receipt](verification/release-0.6.0/CLEAN_ROOM_REPORT.json), [заключительный аудит](verification/release-0.6.0/FINAL_SUBMISSION_AUDIT.json). Последние дизайн и интерактивные исправления находятся только в новых исходниках. Их проверки и граница упаковки описаны в [VERIFICATION](VERIFICATION.md).

Эта таблица связывает все 64 пункта [матрицы кейса](REQUIREMENTS.md) с реализацией и проверяемыми свидетельствами. Текущая Linux amd64 упаковка и актуальный полигон описаны в [статусе 0.6.0](RELEASE_0_6_STATUS.md); исторические свидетельства ниже сохраняются отдельно от нового completed receipt. Это локальное соответствие требованиям, а не неизвестным фиксированным тестам организаторов. [Исторический G5 / 0.5.0 ARM64 завершён](STAGE5_STATUS.md): его [clean-transfer receipt](verification/stage5/CLEAN_ROOM_REPORT.json) подтверждает точный архив a541… и826-test run в пустом offline engine. Этот отчёт не подтверждает новый amd64-архив.

«Реализовано» означает наличие указанного механизма и регрессий. Готовность передаваемой версии подтверждают общий JUnit, acceptance report и внешний `CLEAN_ROOM_REPORT.json` с SHA-256 её точного архива. Receipt сохранённого ZIP0.6.0 не распространяется на последующие изменения исходников. «Частично» сохраняет конкретную границу; наличие ссылки не превращает её в завершённую сдачу.

| REQ | Статус | Проверка / свидетельство |
| --- | --- | --- |
| 001 | Реализовано | [Сквозные прогоны](../tests/test_integration.py), [протокол 20 эпизодов](../tests/test_protocol.py), CLI verify |
| 002 | Реализовано | [Синтетический генератор](../src/fin_audit/multi_generator.py), [планы наборов](../configs/corpus-plan.json); внешние финансовые данные не загружаются |
| 003 | Реализовано | [Checker](../tests/test_stage2_checker.py): scope, source relationships, broken siblings; [schemas](../tests/test_contract_schemas.py) |
| 004 | Реализовано | [Каждое семейство и точные количества](../tests/test_stage2_generation.py), независимые [hand fixtures](../tests/test_stage2_checker.py) |
| 005 | Реализовано | [Обе зависимые пары, их исправление и совместное присутствие](../tests/test_stage2_generation.py), [public-only counterfactual repairs](../tests/test_showcase.py), [paired protocol](../tests/test_protocol.py) |
| 006 | Реализовано | [Чистые split/partial/repeated/refund и realised FX](../tests/test_stage2_generation.py), [ordinary worker controls](../tests/test_stage2_integration.py) |
| 007 | Реализовано | [Error toggles сохраняют исходный мир](../tests/test_stage2_generation.py), provenance и clean hash; [round-trip](../tests/test_storage.py) |
| 008 | Реализовано | [Truth-blind полнота и повреждённые книги](../tests/test_stage2_checker.py), [потерянный issue/proof в gold](../tests/test_storage.py), [hand-calculated masking](../tests/test_showcase.py) |
| 009 | Реализовано | [Synthetic company/entity names, независимые от ошибок](../tests/test_presentation.py), [ID/order transforms](../tests/test_transforms.py), [деньги/offsets](../tests/test_stage2_contracts.py), [комбинации](../tests/test_corpus.py). `names-1.0` опционален и не меняет frozen corpus0.3 |
| 010 | Реализовано | [Supported large profiles](../tests/test_stage2_generation.py), [50-inspection scan](../tests/test_stage2_integration.py), [размеры открытого корпуса](../datasets/manifest.json) |
| 011 | Реализовано | [Manifest schemas](../tests/test_contract_schemas.py), [contracts/round-trip](../tests/test_stage2_contracts.py) |
| 012 | Реализовано | [Decimal/exponents/large integers/time offsets](../tests/test_stage2_contracts.py), [money primitives](../tests/test_data.py) |
| 013 | Реализовано | [FX и условная сумма allocation](../tests/test_stage2_checker.py), [wrong currency FP](../tests/test_evaluation.py) |
| 014 | Реализовано | [CSV/null/extensions и corruption](../tests/test_storage.py), [schema](../schemas/episode.schema.json) |
| 015 | Реализовано | [Invoice schemas](../tests/test_contract_schemas.py), [source and counterpart checker](../tests/test_stage2_checker.py) |
| 016 | Реализовано | [Allocation hand calculations](../tests/test_stage2_checker.py), [native partial/missing references](../tests/test_data.py) |
| 017 | Реализовано | [FX row IDs и доступ через tools](../tests/test_stage2_integration.py), [missing/invalid FX](../tests/test_stage2_contracts.py) |
| 018 | Реализовано | [Policy и public tools](../tests/test_environment.py), [явный контракт FX/period](STAGE2_CONTRACT.md) |
| 019 | Реализовано | [Explicit CSV null и lossless JSON extension cells](../tests/test_storage.py), [no guessed links](../tests/test_data.py) |
| 020 | Реализовано | [Gold/provenance strict export/load](../tests/test_storage.py), [schemas](../tests/test_contract_schemas.py), [private transform history](../tests/test_transforms.py) |
| 021 | Реализовано | [Reset/full submit/replay](../tests/test_environment.py) |
| 022 | Реализовано | [Initial public allowlist](../tests/test_environment.py), [reserved authoring labels](../tests/test_public_privacy.py) |
| 023 | Реализовано | [Actual wire trajectories](../tests/test_contract_schemas.py), [strict JSON](../tests/test_integration.py) |
| 024 | Реализовано | [Fresh-worker repeats](../tests/test_integration.py), [80 exact logical replays](../tests/test_protocol.py) |
| 025 | Реализовано | [Reset counters/cursors/outcome, failed load](../tests/test_environment.py), fresh process per ordinary episode |
| 026 | Реализовано | [Record/document aliases, policy, FX access](../tests/test_environment.py), [FX evidence IDs](../tests/test_stage2_integration.py) |
| 027 | Реализовано | [No gaps, stable/scoped cursors, page bounds](../tests/test_environment.py) |
| 028 | Реализовано | [Allowlist arithmetic, precision and resource attacks](../tests/test_calculator.py) |
| 029 | Реализовано | [Submit at100, immutable terminal outcome](../tests/test_environment.py) |
| 030 | Реализовано | [Invalid actions spend one step, bounded errors](../tests/test_environment.py) |
| 031 | Реализовано | [Snapshot isolation and public allowlists](../tests/test_environment.py), [private-key guard](../tests/test_public_privacy.py) |
| 032 | Реализовано | [99 inspections+submit;100 inspections truncate](../tests/test_environment.py) |
| 033 | Реализовано | [Deadline before/during action and empty-final truncation](../tests/test_environment.py), [real hung worker](../tests/test_integration.py) |
| 034 | Реализовано | [Nonterminal reward schema](../tests/test_contract_schemas.py), [closed traces](../tests/test_protocol.py) |
| 035 | Реализовано | [Train/closed true-score equivalence](../tests/test_integration.py), [atomic full-protocol publication](../tests/test_protocol.py) |
| 036 | Реализовано | [Actual Linux UID/chroot/seccomp/FD/env/proc isolation](../tests/test_security_runtime.py), [closed protocol](../src/fin_audit/protocol.py) |
| 037 | Реализовано | [Calculator attacks](../tests/test_calculator.py), [sandbox probes and fail-closed launcher](../tests/test_security_runtime.py) |
| 038 | Реализовано | [Strict findings definition в общей schema](../schemas/episode.schema.json), [runtime malformed submits](../tests/test_evaluation.py) |
| 039 | Реализовано | [Raw size/length limits before normalization](../tests/test_evaluation.py), [schema boundaries](../tests/test_contract_schemas.py) |
| 040 | Реализовано | [Malformed minus-one override and masked wire](../tests/test_environment.py), [broker classification](../tests/test_integration.py) |
| 041 | Реализовано | [Complete proof, type/currency/tolerance, maximum matching vs exhaustive](../tests/test_evaluation.py), [FX proof](../tests/test_stage2_contracts.py), [реальные AST-поломки этих правил](../configs/scorer-mutations.json) |
| 042 | Реализовано | [Duplicate/shotgun/omission/one-to-one matching](../tests/test_evaluation.py), [transformed imperfect answers](../tests/test_transforms.py) |
| 043 | Реализовано | [Open evaluator](../src/fin_audit/evaluation.py), [policy/type rules](STAGE2_CONTRACT.md), [versioned schemas](../schemas/episode.schema.json) |
| 044 | Реализовано | [Independent rational fixtures and exact reward](../tests/test_evaluation.py) |
| 045 | Реализовано | [Clean silence/accusation fixtures](../tests/test_evaluation.py), [5 control clean episodes](../tests/test_protocol.py) |
| 046 | Реализовано | [Step cost, clipping, exact fraction, terminal return](../tests/test_evaluation.py), [lifecycle boundary](../tests/test_environment.py), [wrong-step-cost mutation](../configs/scorer-mutations.json) |
| 047 | Реализовано | [Silence/false/duplicate/mark-all comparisons](../tests/test_evaluation.py), [same-cohort policies](../tests/test_protocol.py) |
| 048 | Реализовано | [Guessed complete evidence, unknown/wrong claims](../tests/test_evaluation.py), [timeouts/re-submit/invalid wire](../tests/test_integration.py) |
| 049 | Реализовано | [Disjoint open/holdout seeds and new configs](../tests/test_corpus.py), [private persistent master seed](../tests/test_stage3_host.py) |
| 050 | Реализовано | [Four reference adapters](../src/fin_audit/agent_worker.py), [privileged oracle only in trusted runner](../src/fin_audit/runner.py), [same cohort](../tests/test_protocol.py) |
| 051 | Реализовано | [20 local holdout cases:5 clean,5 interacting,10 other](../configs/holdout-plan.json), [80 runs](../tests/test_protocol.py) |
| 052 | Реализовано | [20 paired ID/order cases and equal-budget score checks](../tests/test_protocol.py), [collision/malformed/unknown-ID invariance](../tests/test_transforms.py) |
| 053 | Реализовано | [Version/hash/runtime manifest](../src/fin_audit/reporting.py), [trusted exact configs and replay](../src/fin_audit/protocol.py) |
| 054 | Реализовано; transfer receipt отдельно | [Network=none/no GPU](../scripts/docker_common.py), [fresh independent network namespace/store](../scripts/clean_rehearsal.py), [tests](../tests/test_clean_rehearsal.py). Фактический перенос подтверждается внешним receipt |
| 055 | Реализовано; amd64 измерения отдельно | [2 CPU/4GiB/60s limits](../scripts/docker_common.py), [whole-container peak/counters](../scripts/container_verify.py), [archive payload<1GB gate](../scripts/release_support.py), [platform checks](../tests/test_prepare_platform.py). Target 0.6.0 — Linux amd64; фактические peak/limits и способ исполнения подтверждает внешний receipt |
| 056 | Реализовано | [MIT](../LICENSE), [lock](../uv.lock), [pinned image](../docker/Dockerfile), [фактические license notices/inventory](THIRD_PARTY_NOTICES.md), [sealed release builder](../scripts/release_support.py) |
| 057 | Реализовано; transfer receipt отдельно | [Forced release launcher/verify](../scripts/verify), [safe extraction/checksum/load](../tests/test_release.py), [missing-manifest failclosed](../tests/test_release_entry.py), [packaged demo](../tests/test_demo.py), [release guide и внешний ZIP](RELEASE_GUIDE.md) |
| 058 | Реализовано | [Versioned20 open episodes and hashes](../datasets/manifest.json), [regenerate/hash/permissions verification](../tests/test_corpus.py) |
| 059 | Реализовано | [Environment](../tests/test_environment.py), [scorer](../tests/test_evaluation.py), [integration](../tests/test_protocol.py), [mutation harness](../tests/test_mutation_harness.py), [release](../tests/test_release.py), [web-readiness](../tests/test_web_readiness.py); текущий JUnit относится к completed receipt 0.6.0, исторический — к статусу G5 |
| 060 | Реализовано | [Trajectory definition в общей schema](../schemas/episode.schema.json), [real traces and exact replay](../tests/test_protocol.py) |
| 061 | Реализовано | [Reviewer design note](DESIGN_NOTE.md), [трёхминутный сценарий](DEMO_GUIDE.md), [текущий интерактивный полигон](WEB_DEMO.md), [completed evidence viewer](STAGE4_CONTRACT.md), [историческое офлайн-досье 0.5.0](verification/stage5/showcase.html). Актуальное досье находится в `sample/showcase.html` передаваемого пакета |
| 062 | Реализовано | [Per-episode and by-policy/type/cohort metrics](../src/fin_audit/protocol.py); oracle explicitly privileged |
| 063 | Реализовано; transfer receipt отдельно | [Текущий статус 0.6.0](RELEASE_0_6_STATUS.md), [исторические измерения 0.5.0](STAGE5_STATUS.md), [release integrity](../tests/test_release.py), [packaged demo](../tests/test_demo.py), [whole cgroup](../tests/test_container_verify.py), [honest limitations](DESIGN_NOTE.md). Fresh transfer связывается с SHA архива внешним receipt |
| 064 | Реализовано | [Report scope and oracle limitations](STAGE3_PROTOCOL.md); local successes are not official score or learned-policy quality |

G4 добавил mutation report и masking/repair showcase; G5 добавляет имена, sealed archive, license inventory, whole-container metrics и инструменты fresh offline transfer. Встроенный отчёт не подписывает собственную будущую передачу: фактический clean receipt поставляется рядом с архивом и содержит его SHA. Официальные fixed checks/adapters и канал подачи не предоставлены. Для нового amd64-релиза фактическая платформа и clean-transfer proof публикуются отдельно. Эти внешние границы не выдаются за выполненную официальную оценку.


В 0.6.0 добавлены обязательные hashed HTML/CSS/JS/MJS assets, packaged `./demo`, реальный loopback HTTP/API smoke с secure workers, проверка архитектур image/VM/application и сборщик внешнего ZIP с исключением закрытых seeds. Старый sealed архив 0.5.0 не меняется. Новая официальная оценка не выводится из локальных тестов; основание готовности — actual completed receipt точного текущего архива.
