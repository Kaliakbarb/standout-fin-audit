# Открытые эпизоды FinTech

10 train и10 dev эпизодов, план0.3.0, схема books0.2.0. Полные seed/config и SHA-256 находятся в manifest.json.

Это открытые учебные fixtures: gold, provenance и seeds доступны человеку в репозитории. Агент получает только public записи через инструменты; индекс, seed и private не попадают в его runtime.

Git не сохраняет права0600/0700: verify_corpus проверяет source hashes и использует временную protected копию для строгого load_episode, не меняя исходные файлы/права. Runtime export/load сохраняют строгие private permissions.

Проверка: ./scripts/dev python -c "from pathlib import Path; from fin_audit.corpus import verify_corpus; print(verify_corpus(Path('datasets')))"
