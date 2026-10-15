# Customer testing restriction

The user explicitly prohibited publishing anything to any real page or account throughout the
testing period (2026-10-15). This includes private, unlisted and test publications on every platform.

Keep `REPEATER_PUBLISHING_ENABLED=false` in production. Do not lift this lock, enable publishing,
or run a real publication test until the user explicitly ends this restriction. Use synthetic
accounts, fake adapters and mocked HTTP for publication tests. Account linking, importing content,
backups and read-only verification remain in scope.
