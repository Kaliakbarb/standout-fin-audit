# Лицензии и сторонние материалы

Этот документ описывает происхождение компонентов. Он не заменяет их полные license/copyright notices и не назначает всему контейнеру одну лицензию.

| Область | Условия и источник |
| --- | --- |
| Собственные исходники Genesis FinAudit | [MIT](../LICENSE), copyright2026 Genesis FinAudit project contributors |
| Условия кейса Genesis Hackathon | [Сохранённый источник FinTech](source/fintech/README.md), шесть PNG и транскрипция. Права принадлежат авторам условий; MIT проекта на них не распространяется. Снимки сохранены для трассировки требований, не как оригинальный PDF |
| Синтетические финансовые эпизоды | Созданы нашим генератором; внешние банковские, персональные или коммерческие финансовые данные не использованы. Frozen open corpus0.3 хранится в [datasets](../datasets/README.md) |
| CPython и стандартная библиотека | Фактический `LICENSE` из подготовленного image включается в runtime license inventory. Он содержит условия Python и перечисленных в нём включённых компонентов; не заменять его одной строкой «MIT» |
| DebianBookworm, libseccomp и системные библиотеки | Точные package/version из `dpkg-query`, `/usr/share/doc/*/copyright` установленных пакетов и используемые `/usr/share/common-licenses/*` сохраняются из проверяемого image. Это несколько лицензий и отдельных copyright notices |
| Python development/test packages | Версии закреплены в [uv.lock](../uv.lock). Полные notices извлекаются из фактически установленных distribution files exact image; inventory фиксирует hashes и provenance. Product runtime dependencies в `pyproject.toml` пусты, но acceptance image содержит test tools |

## Состав Python test toolchain

Lockfile и package metadata фиксируют следующий набор. Краткие обозначения ниже — ориентир; приоритет имеют извлечённые полные тексты notices в release inventory.

| Distribution | Закреплённая версия | Обозначение из package metadata / license file |
| --- | --- | --- |
| attrs |26.1.0|MIT|
| hypothesis |6.170.15|MPL-2.0|
| iniconfig |2.3.1|MIT|
| jsonschema |4.26.0|MIT|
| jsonschema-specifications |2025.9.1|MIT|
| packaging |26.3|Apache-2.0 OR BSD-2-Clause|
| pluggy |1.7.0|MIT|
| Pygments |2.21.0|BSD-2-Clause|
| pytest |8.4.2|MIT|
| referencing |0.37.0|MIT|
| rpds-py |2026.9.1|MIT|
| sortedcontainers |2.4.0|Apache2.0 в metadata|
| typing_extensions |4.16.0|PSF-2.0|

Фактический image может также содержать installation tooling и дополнительные bundled notices. Release inventory перечисляет его полный обнаруженный состав; эта таблица не выдаётся за исчерпывающий software bill of materials контейнера. Для бинарных расширений одно поле package license не заменяет notices включённых компонентов.

## Проверяемый runtime inventory

Окончательная сдача содержит `runtime/licenses.json` с image ID, OS/architecture и перечнем компонентов. Полные тексты лежат в `runtime/licenses/python/`, `runtime/licenses/cpython/`, `runtime/licenses/debian/` и `runtime/licenses/debian-common/`. `release-manifest.json` связывает inventory и hashes каждого файла с точным image. Сборщик `collect_image_metadata` в [scripts/release_support.py](../scripts/release_support.py) запускается по immutable image ID, а не по плавающему имени тега. Данные получаются чтением metadata и notice files готового image без сети. Они относятся к этому image; после rebuild их нужно получить повторно. Отсутствующий CPython LICENSE останавливает сборку; число записей и текстов берётся из фактического inventory, а не из этой записки.

Базовый image закреплён digest в [Dockerfile](../docker/Dockerfile). `libseccomp2` закреплён версией. Подготовка/build может требовать загрузок; переданный готовый image и offline verification не обращаются к registries, PyPI, apt или платным API.

Ни модельные веса, ни сторонние изображения, UI-библиотеки или сетевые сервисы в продукт не включены. При разработке применялся Codex; это помощь в создании кода, не компонент runtime и не результат обучения аудитора. Официальные adapters организаторов не были предоставлены: reference policies в репозитории написаны в рамках этого проекта.
