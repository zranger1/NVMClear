# ESP32 NVS Maintenance Tool

This project provides a small Python CLI for ESP32 devices connected over serial.

It does three things:

1. Backs up the flash regions that matter for recovery.
1. Erases every `nvs` partition found in the partition table.
1. Restores a previous backup if something goes wrong.

## Setup

Create a local virtual environment and install `esptool` from PyPI:

```powershell
python setup_env.py
```

This creates a `.venv` folder and installs the dependencies listed in `requirements.txt`.
After that, run the tool with the Python interpreter of your choice — it will automatically
use the `esptool` from `.venv` first, then fall back to any `esptool` already on your PATH,
and finally to the bundled `tools/esptool.exe` if neither is found.

## What gets backed up

The backup includes:

1. Bootloader region.
1. Partition table.
1. Every partition listed in the partition table.

Backups are written into timestamped folders under `backups/`.

## Usage

### Backup only

```powershell
python esp32_nvs_tool.py backup --port COM3 --baud 115200
```

### Erase NVS with an automatic backup first

```powershell
python esp32_nvs_tool.py erase-nvs --port COM3 --baud 115200
```

### Restore from a backup folder

```powershell
python restore_esp32.py --port COM3 --baud 115200 --backup-dir backups\esp32_backup_YYYYMMDD_HHMMSS_xxxxxxxx
```

### Optional settings

If your project uses a non-default partition table offset, override it with `--partition-table-offset`.

Example:

```powershell
python esp32_nvs_tool.py erase-nvs --port COM3 --baud 115200 --partition-table-offset 0x9000
```

You can also pass `--chip` if needed.

## Restore details

The backup folder contains a `manifest.json` file plus one binary per backed-up flash region. The restore command writes those binaries back to their original offsets.

## Notes

1. Run these commands only on the intended device.
1. Restore writes flash contents back in place, so use the exact backup folder created for that device.
