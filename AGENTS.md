# AGENTS.md

- Repo root is a small Python toolset; `requirements.txt` lists the single external dependency (`esptool`).
- The main CLI is `esp32_nvs_tool.py`; `backup_esp32.py` and `restore_esp32.py` are thin wrappers around it.
- Flash operations prefer the pip-installed `esptool` from the local `.venv` (created by `setup_env.py`), then fall back to any `esptool` on the system PATH, then the bundled `tools/esptool.exe`.
- Run `python setup_env.py` once to create `.venv` and install `requirements.txt`; no other dependency install steps exist.
- `backup` creates a timestamped folder under `backups/` and writes `manifest.json` plus one binary per flash region.
- `erase-nvs` backs up first unless `--no-backup` is passed, then erases every partition with type `data` and subtype `nvs` from the partition table.
- `restore` writes the exact region binaries from a backup folder back to their recorded offsets; use the matching backup for the same device.
- Default partition table offset is `0x8000`; only override `--partition-table-offset` when the target firmware uses a different layout.
- Backup filenames are sanitized for Windows path rules; keep that behavior if changing labels or manifest naming.
- Keep generated backup artifacts out of git; `backups/` and `__pycache__/` are already ignored.
- There are no automated tests here; the local verification step is `python -m py_compile esp32_nvs_tool.py backup_esp32.py restore_esp32.py`. Testing requires a physical ESP32 device and is not included in CI.  Let the user run them manually when needed.
