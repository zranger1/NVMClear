# ESP32 NVS Maintenance Tool

This project provides a small Python CLI for ESP32 devices connected over serial.

It does three things:

1. Backs up the flash regions that matter for recovery.
1. Erases every `nvs` partition found in the partition table.
1. Restores a previous backup if something goes wrong.

The tool uses the bundled `tools/esptool.exe`.

## What gets backed up

The backup includes:

1. Bootloader region.
1. Partition table.
1. Every partition listed in the partition table.

Backups are written into timestamped folders under `backups/`.

## Usage

### Backup only

```powershell
python esp32_nvs_tool.py backup --port COM3
```

### Erase NVS with an automatic backup first

```powershell
python esp32_nvs_tool.py erase-nvs --port COM3
```

### Restore from a backup folder

```powershell
python restore_esp32.py --port COM3 --backup-dir backups\esp32_backup_YYYYMMDD_HHMMSS_xxxxxxxx
```

### Optional settings

If your project uses a non-default partition table offset, override it with `--partition-table-offset`.

Example:

```powershell
python esp32_nvs_tool.py erase-nvs --port COM3 --partition-table-offset 0x9000
```

You can also pass `--baud` and `--chip` if needed.

## Restore details

The backup folder contains a `manifest.json` file plus one binary per backed-up flash region. The restore command writes those binaries back to their original offsets.

## Notes

1. Run these commands only on the intended device.
1. Restore writes flash contents back in place, so use the exact backup folder created for that device.
