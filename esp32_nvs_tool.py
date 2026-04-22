from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


FLASH_BOOTLOADER_START = 0x1000
DEFAULT_PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0x1000
PARTITION_ENTRY_SIZE = 32
PARTITION_TABLE_MAGIC = 0x50AA
DATA_TYPE = 0x01
NVS_SUBTYPE = 0x02


@dataclass(frozen=True)
class PartitionEntry:
    offset: int
    size: int
    type: int
    subtype: int
    label: str
    flags: int


@dataclass(frozen=True)
class BackupRegion:
    offset: int
    size: int
    label: str
    file_name: str


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def esptool_path() -> Path:
    """Resolve the esptool executable.

    Search order:
    1. Local virtual environment (.venv) created by setup_env.py.
    2. System PATH (covers globally-installed or conda-environment esptool).
    3. Bundled tools/esptool.exe as a last-resort fallback.
    """
    root = repo_root()

    # 1. Local venv
    if sys.platform == "win32":
        venv_esptool = root / ".venv" / "Scripts" / "esptool.exe"
    else:
        venv_esptool = root / ".venv" / "bin" / "esptool"
    if venv_esptool.exists():
        return venv_esptool

    # 2. System PATH
    system_esptool = shutil.which("esptool")
    if system_esptool:
        return Path(system_esptool)

    # 3. Bundled Windows executable
    bundled = root / "tools" / "esptool.exe"
    if bundled.exists():
        return bundled

    raise FileNotFoundError(
        "esptool not found. Run 'python setup_env.py' to create a virtual "
        "environment with esptool installed, or install it manually with "
        "'pip install esptool'."
    )


def hex_int(value: str) -> int:
    return int(value, 0)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def run_esptool(args: list[str], chip: str, port: str, baud: int | None) -> None:
    command = [str(esptool_path()), "--chip", chip, "--port", port]
    if baud is not None:
        command.extend(["--baud", str(baud)])
    command.extend(args)
    subprocess.run(command, check=True)


def read_flash(port: str, chip: str, baud: int | None, offset: int, size: int, output: Path) -> None:
    ensure_parent(output)
    run_esptool(["read_flash", hex(offset), hex(size), str(output)], chip, port, baud)


def write_flash(port: str, chip: str, baud: int | None, offset: int, input_path: Path) -> None:
    run_esptool(
        [
            "write_flash",
            "--flash_mode",
            "keep",
            "--flash_freq",
            "keep",
            "--flash_size",
            "keep",
            "--verify",
            hex(offset),
            str(input_path),
        ],
        chip,
        port,
        baud,
    )


def erase_region(port: str, chip: str, baud: int | None, offset: int, size: int) -> None:
    run_esptool(["erase_region", hex(offset), hex(size)], chip, port, baud)


def parse_partition_table(data: bytes) -> list[PartitionEntry]:
    entries: list[PartitionEntry] = []
    for index in range(0, len(data), PARTITION_ENTRY_SIZE):
        chunk = data[index : index + PARTITION_ENTRY_SIZE]
        if len(chunk) < PARTITION_ENTRY_SIZE:
            break
        if chunk == b"\xFF" * PARTITION_ENTRY_SIZE:
            break

        magic, part_type, subtype, offset, size, label_bytes, flags = struct.unpack(
            "<HBBLL16sL", chunk
        )
        if magic != PARTITION_TABLE_MAGIC:
            break

        label = label_bytes.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        entries.append(
            PartitionEntry(
                offset=offset,
                size=size,
                type=part_type,
                subtype=subtype,
                label=label,
                flags=flags,
            )
        )
    return entries


def partition_label(entry: PartitionEntry) -> str:
    if entry.label:
        return entry.label
    return f"type{entry.type:02X}_subtype{entry.subtype:02X}"


def safe_filename_component(value: str) -> str:
    cleaned = [ch if ch.isalnum() or ch in "-_." else "_" for ch in value]
    result = "".join(cleaned).strip("._")
    return result or "partition"


def build_backup_regions(entries: list[PartitionEntry], partition_table_offset: int) -> list[BackupRegion]:
    regions: list[BackupRegion] = []

    bootloader_size = partition_table_offset - FLASH_BOOTLOADER_START
    if bootloader_size > 0:
        regions.append(
            BackupRegion(
                offset=FLASH_BOOTLOADER_START,
                size=bootloader_size,
                label="bootloader",
                file_name=f"{FLASH_BOOTLOADER_START:08X}_bootloader.bin",
            )
        )

    regions.append(
        BackupRegion(
            offset=partition_table_offset,
            size=PARTITION_TABLE_SIZE,
            label="partition-table",
            file_name=f"{partition_table_offset:08X}_partition-table.bin",
        )
    )

    for entry in sorted(entries, key=lambda item: item.offset):
        label = safe_filename_component(partition_label(entry))
        regions.append(
            BackupRegion(
                offset=entry.offset,
                size=entry.size,
                label=partition_label(entry),
                file_name=f"{entry.offset:08X}_{label}.bin",
            )
        )

    return regions


def make_backup_directory(output_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    token = uuid.uuid4().hex[:8]
    backup_dir = output_root / f"esp32_backup_{stamp}_{token}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    return backup_dir


def load_partition_table_from_device(
    port: str,
    chip: str,
    baud: int | None,
    partition_table_offset: int,
    destination: Path,
) -> list[PartitionEntry]:
    read_flash(port, chip, baud, partition_table_offset, PARTITION_TABLE_SIZE, destination)
    return parse_partition_table(destination.read_bytes())


def create_backup(
    port: str,
    chip: str,
    baud: int | None,
    partition_table_offset: int,
    output_root: Path,
) -> Path:
    backup_dir = make_backup_directory(output_root)
    partition_table_file = backup_dir / f"{partition_table_offset:08X}_partition-table.bin"
    entries = load_partition_table_from_device(
        port,
        chip,
        baud,
        partition_table_offset,
        partition_table_file,
    )

    regions = build_backup_regions(entries, partition_table_offset)
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "chip": chip,
        "port": port,
        "baud": baud,
        "partition_table_offset": partition_table_offset,
        "regions": [asdict(region) for region in regions],
        "partitions": [asdict(entry) for entry in entries],
    }

    for region in regions:
        output = backup_dir / region.file_name
        if output.exists():
            continue
        read_flash(port, chip, baud, region.offset, region.size, output)

    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return backup_dir


def erase_nvs_partitions(
    port: str,
    chip: str,
    baud: int | None,
    partition_table_offset: int,
) -> list[PartitionEntry]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir) / "partition-table.bin"
        entries = load_partition_table_from_device(
            port,
            chip,
            baud,
            partition_table_offset,
            temp_path,
        )

    nvs_entries = [entry for entry in entries if entry.type == DATA_TYPE and entry.subtype == NVS_SUBTYPE]
    if not nvs_entries:
        raise RuntimeError("No NVS partitions were found in the partition table")

    for entry in nvs_entries:
        erase_region(port, chip, baud, entry.offset, entry.size)

    return nvs_entries


def restore_backup(port: str, chip: str, baud: int | None, backup_dir: Path) -> None:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in {backup_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    regions = sorted(manifest["regions"], key=lambda region: region["offset"])

    for region in regions:
        input_path = backup_dir / region["file_name"]
        if not input_path.exists():
            raise FileNotFoundError(f"Missing backup file: {input_path}")
        write_flash(port, chip, baud, int(region["offset"]), input_path)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", required=True, help="Serial port, for example COM3")
    parser.add_argument("--chip", default="esp32", help="ESP chip name passed to esptool.exe")
    parser.add_argument("--baud", type=int, default=115200, help="Optional serial baud rate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up, erase, and restore ESP32 flash regions")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Back up the bootloader, partition table, and all partitions")
    add_common_arguments(backup_parser)
    backup_parser.add_argument(
        "--output-root",
        default="backups",
        help="Directory where timestamped backup folders are created",
    )
    backup_parser.add_argument(
        "--partition-table-offset",
        type=hex_int,
        default=DEFAULT_PARTITION_TABLE_OFFSET,
        help="Partition table offset in flash, usually 0x8000",
    )

    erase_parser = subparsers.add_parser("erase-nvs", help="Erase every NVS partition found in the partition table")
    add_common_arguments(erase_parser)
    erase_parser.add_argument(
        "--partition-table-offset",
        type=hex_int,
        default=DEFAULT_PARTITION_TABLE_OFFSET,
        help="Partition table offset in flash, usually 0x8000",
    )
    erase_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the automatic backup step before erasing NVS",
    )
    erase_parser.add_argument(
        "--output-root",
        default="backups",
        help="Directory where automatic backups are stored",
    )

    restore_parser = subparsers.add_parser("restore", help="Restore flash regions from a previous backup folder")
    add_common_arguments(restore_parser)
    restore_parser.add_argument("--backup-dir", required=True, type=Path, help="Backup folder to restore from")

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "backup":
        output_root = Path(args.output_root)
        backup_dir = create_backup(
            port=args.port,
            chip=args.chip,
            baud=args.baud,
            partition_table_offset=args.partition_table_offset,
            output_root=output_root,
        )
        print(f"Backup created at {backup_dir}")
        return 0

    if args.command == "erase-nvs":
        if not args.no_backup:
            output_root = Path(args.output_root)
            backup_dir = create_backup(
                port=args.port,
                chip=args.chip,
                baud=args.baud,
                partition_table_offset=args.partition_table_offset,
                output_root=output_root,
            )
            print(f"Backup created at {backup_dir}")

        nvs_entries = erase_nvs_partitions(
            port=args.port,
            chip=args.chip,
            baud=args.baud,
            partition_table_offset=args.partition_table_offset,
        )
        for entry in nvs_entries:
            print(f"Erased NVS partition {partition_label(entry)} at 0x{entry.offset:08X} ({entry.size} bytes)")
        return 0

    if args.command == "restore":
        restore_backup(args.port, args.chip, args.baud, args.backup_dir)
        print(f"Restored backup from {args.backup_dir}")
        return 0

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
