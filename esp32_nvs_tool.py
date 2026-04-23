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

# NVS page / entry layout (all sizes in bytes)
NVS_PAGE_SIZE = 0x1000          # one flash sector
NVS_PAGE_HEADER_SIZE = 32
NVS_BITMAP_OFFSET = 32
NVS_BITMAP_SIZE = 32
NVS_ENTRIES_OFFSET = 64         # header + bitmap
NVS_ENTRY_SIZE = 32
NVS_ENTRIES_PER_PAGE = 126      # (4096 - 64) / 32

# Entry bitmap states (2 bits per entry, LSB-first within each byte)
NVS_ENTRY_EMPTY = 0b11          # flash-erased default
NVS_ENTRY_WRITTEN = 0b10        # live data
NVS_ENTRY_ERASED = 0b00         # logically deleted

# Entry item types relevant to blob handling
NVS_ITEM_BLOB_DATA = 0x41
NVS_ITEM_BLOB_IDX = 0x48

# Namespace reserved for the namespace registry itself
NVS_NS_INDEX_REGISTRY = 0x00

# PHY calibration namespace and keys stored by esp_phy component
NVS_PHY_NAMESPACE = "phy"
NVS_PHY_KEYS = frozenset({"cal_version", "cal_mac", "cal_data"})


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
    run_esptool(["read-flash", hex(offset), hex(size), str(output)], chip, port, baud)


def write_flash(port: str, chip: str, baud: int | None, offset: int, input_path: Path) -> None:
    run_esptool(
        [
            "write-flash",
            "--flash-mode",
            "keep",
            "--flash-freq",
            "keep",
            "--flash-size",
            "keep",
            "--no-diff-verify",
            hex(offset),
            str(input_path),
        ],
        chip,
        port,
        baud,
    )


# ---------------------------------------------------------------------------
# NVS binary manipulation helpers
# ---------------------------------------------------------------------------

def _nvs_entry_state(bitmap: bytes | bytearray, entry_index: int) -> int:
    """Return the 2-bit state for the entry at *entry_index* (0-based within page)."""
    byte_index = entry_index // 4
    bit_offset = (entry_index % 4) * 2
    return (bitmap[byte_index] >> bit_offset) & 0b11


def _nvs_mark_entry_erased(bitmap: bytearray, entry_index: int) -> None:
    """Clear both state bits for *entry_index*, marking it ERASED (0b00)."""
    byte_index = entry_index // 4
    bit_offset = (entry_index % 4) * 2
    bitmap[byte_index] &= ~(0b11 << bit_offset) & 0xFF


def _nvs_parse_entry_header(raw: bytes) -> dict:
    """Decode the first 8 bytes of a 32-byte NVS entry."""
    ns, item_type, span, chunk_index = struct.unpack_from("<BBBB", raw, 0)
    key_bytes = raw[8:24]
    key = key_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return {"ns": ns, "type": item_type, "span": max(span, 1), "key": key}


def _nvs_resolve_namespace_indices(image: bytes) -> dict[str, int]:
    """Scan every page in *image* and return a mapping of namespace name → NVS index."""
    ns_map: dict[str, int] = {}
    num_pages = len(image) // NVS_PAGE_SIZE
    for page_num in range(num_pages):
        page_base = page_num * NVS_PAGE_SIZE
        bitmap = image[page_base + NVS_BITMAP_OFFSET : page_base + NVS_BITMAP_OFFSET + NVS_BITMAP_SIZE]
        entries_base = page_base + NVS_ENTRIES_OFFSET
        for entry_idx in range(NVS_ENTRIES_PER_PAGE):
            if _nvs_entry_state(bitmap, entry_idx) != NVS_ENTRY_WRITTEN:
                continue
            entry_off = entries_base + entry_idx * NVS_ENTRY_SIZE
            raw = image[entry_off : entry_off + NVS_ENTRY_SIZE]
            if len(raw) < NVS_ENTRY_SIZE:
                break
            hdr = _nvs_parse_entry_header(raw)
            if hdr["ns"] == NVS_NS_INDEX_REGISTRY:
                # Data byte 0 of the registry entry is the assigned namespace index
                ns_index = raw[0x18]
                ns_map[hdr["key"]] = ns_index
    return ns_map


def clear_phy_calibration_in_image(image: bytes) -> tuple[bytes, int]:
    """Return a modified copy of *image* with all PHY calibration entries invalidated.

    Entries are invalidated by clearing their bitmap bits to ERASED (0b00) so
    that NVS reports them as absent on the next boot, triggering a full RF
    recalibration.  The partition table and all other NVS keys are untouched.

    Returns ``(modified_image, entries_cleared_count)``.
    """
    ns_map = _nvs_resolve_namespace_indices(image)
    phy_ns_index = ns_map.get(NVS_PHY_NAMESPACE)
    if phy_ns_index is None:
        return image, 0

    buf = bytearray(image)
    cleared = 0
    num_pages = len(buf) // NVS_PAGE_SIZE

    for page_num in range(num_pages):
        page_base = page_num * NVS_PAGE_SIZE
        bitmap_start = page_base + NVS_BITMAP_OFFSET
        bitmap = bytearray(buf[bitmap_start : bitmap_start + NVS_BITMAP_SIZE])
        entries_base = page_base + NVS_ENTRIES_OFFSET

        entry_idx = 0
        while entry_idx < NVS_ENTRIES_PER_PAGE:
            state = _nvs_entry_state(bitmap, entry_idx)
            if state != NVS_ENTRY_WRITTEN:
                entry_idx += 1
                continue

            entry_off = entries_base + entry_idx * NVS_ENTRY_SIZE
            raw = buf[entry_off : entry_off + NVS_ENTRY_SIZE]
            if len(raw) < NVS_ENTRY_SIZE:
                break
            hdr = _nvs_parse_entry_header(raw)

            if hdr["ns"] == phy_ns_index and hdr["key"] in NVS_PHY_KEYS:
                # Mark this entry and every continuation slot it occupies as ERASED
                for slot in range(hdr["span"]):
                    if entry_idx + slot < NVS_ENTRIES_PER_PAGE:
                        _nvs_mark_entry_erased(bitmap, entry_idx + slot)
                cleared += hdr["span"]
                entry_idx += hdr["span"]
            else:
                entry_idx += 1

        # Write the (possibly modified) bitmap back
        buf[bitmap_start : bitmap_start + NVS_BITMAP_SIZE] = bitmap

    return bytes(buf), cleared


def clear_nvs_phy_calibration(
    port: str,
    chip: str,
    baud: int | None,
    nvs_offset: int,
    nvs_size: int,
) -> int:
    """Read the NVS partition, invalidate PHY calibration entries, write it back.

    Returns the number of entry slots cleared.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        nvs_bin = Path(tmp_dir) / "nvs.bin"
        read_flash(port, chip, baud, nvs_offset, nvs_size, nvs_bin)
        original = nvs_bin.read_bytes()

        modified, cleared = clear_phy_calibration_in_image(original)
        if cleared == 0:
            return 0

        nvs_bin.write_bytes(modified)
        write_flash(port, chip, baud, nvs_offset, nvs_bin)

    return cleared


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

    restore_parser = subparsers.add_parser("restore", help="Restore flash regions from a previous backup folder")
    add_common_arguments(restore_parser)
    restore_parser.add_argument("--backup-dir", required=True, type=Path, help="Backup folder to restore from")

    phy_parser = subparsers.add_parser(
        "clear-phy-cal",
        help="Invalidate PHY calibration entries in the NVS partition to force RF recalibration on next boot",
    )
    add_common_arguments(phy_parser)
    phy_parser.add_argument(
        "--partition-table-offset",
        type=hex_int,
        default=DEFAULT_PARTITION_TABLE_OFFSET,
        help="Partition table offset in flash, usually 0x8000",
    )
    phy_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the automatic backup step before modifying the NVS partition",
    )
    phy_parser.add_argument(
        "--output-root",
        default="backups",
        help="Directory where automatic backups are stored",
    )

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

    if args.command == "restore":
        restore_backup(args.port, args.chip, args.baud, args.backup_dir)
        print(f"Restored backup from {args.backup_dir}")
        return 0

    if args.command == "clear-phy-cal":
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

        # Locate the NVS partition(s) in the partition table
        with tempfile.TemporaryDirectory() as tmp_dir:
            pt_path = Path(tmp_dir) / "partition-table.bin"
            nvs_entries = load_partition_table_from_device(
                args.port, args.chip, args.baud, args.partition_table_offset, pt_path
            )

        nvs_partitions = [
            entry for entry in nvs_entries
            if entry.type == DATA_TYPE and entry.subtype == NVS_SUBTYPE
        ]
        if not nvs_partitions:
            print("No NVS partitions found in the partition table.")
            return 1

        total_cleared = 0
        for nvs_entry in nvs_partitions:
            cleared = clear_nvs_phy_calibration(
                port=args.port,
                chip=args.chip,
                baud=args.baud,
                nvs_offset=nvs_entry.offset,
                nvs_size=nvs_entry.size,
            )
            if cleared:
                print(
                    f"Cleared {cleared} PHY calibration entry slot(s) from NVS partition "
                    f"'{partition_label(nvs_entry)}' at 0x{nvs_entry.offset:08X}."
                )
            else:
                print(
                    f"No PHY calibration data found in NVS partition "
                    f"'{partition_label(nvs_entry)}' at 0x{nvs_entry.offset:08X}."
                )
            total_cleared += cleared

        if total_cleared:
            print("PHY recalibration will run on next boot.")
        else:
            print("Nothing to clear — PHY calibration data was not present.")
        return 0

    parser.error("Unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
