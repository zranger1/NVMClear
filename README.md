# ESP32 NVS Maintenance Tool

> **WARNING — Read before use**
>
> This tool writes directly to the flash memory of a physical ESP32 device.
> Used incorrectly it can **permanently disable Wi-Fi**, corrupt firmware, or
> **brick the device entirely**. Always let it take an automatic backup first
> (the default), verify you are targeting the correct serial port, and keep the
> matching backup folder until you have confirmed normal device operation.
> You assume all responsibility for any damage caused.

This project provides a small Python CLI for ESP32 devices connected over serial.

It does three things:

1. Backs up the flash regions that matter for recovery.
1. Invalidates PHY calibration data to force RF recalibration on the next boot.
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

### Clear PHY calibration (with automatic backup first)

This is the primary use case for this tool. It invalidates the three PHY
calibration keys (`cal_version`, `cal_mac`, `cal_data`) stored in the `phy`
NVS namespace by marking their NVS bitmap entries as erased. All other NVS
data — including Wi-Fi credentials — is left completely untouched.

On the next boot the ESP32 PHY driver finds no calibration data, performs a
full RF recalibration, and writes fresh values. Wi-Fi will reconnect normally
once calibration completes.

```powershell
python esp32_nvs_tool.py clear-phy-cal --port COM3 --baud 115200
```

To skip the automatic backup (not recommended):

```powershell
python esp32_nvs_tool.py clear-phy-cal --port COM3 --baud 115200 --no-backup
```

### Restore from a backup folder

Use this to undo any operation performed by this tool.

```powershell
python restore_esp32.py --port COM3 --baud 115200 --backup-dir backups\esp32_backup_YYYYMMDD_HHMMSS_xxxxxxxx
```

### Optional settings

If your project uses a non-default partition table offset, pass `--partition-table-offset`:

```powershell
python esp32_nvs_tool.py clear-phy-cal --port COM3 --baud 115200 --partition-table-offset 0x9000
```

You can also pass `--chip` if needed.

## Restore details

The backup folder contains a `manifest.json` file plus one binary per backed-up flash region. The restore command writes those binaries back to their original offsets.

## Notes

1. Run these commands only on the intended device — confirm the correct COM port before running.
1. Always keep the automatic backup until you have verified normal device operation.
1. Restore writes flash contents back in place; use the exact backup folder created for that device.
1. If `clear-phy-cal` reports "No PHY calibration data found", the partition either has never been calibrated or was already cleared — no write is performed.
