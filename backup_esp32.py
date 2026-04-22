from __future__ import annotations

import sys

from esp32_nvs_tool import main


if __name__ == "__main__":
    raise SystemExit(main(["backup", *sys.argv[1:]]))
