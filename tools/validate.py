#!/usr/bin/env python3
"""校验 `plugins/` 下的插件元数据（pydantic 模型）。

用法：
    uv run python tools/validate.py            # 结构与字段校验（离线）
    uv run python tools/validate.py --fetch    # 额外查询 release、核对资产 digest 与内层 plugin.toml
    uv run python tools/validate.py ccr loj-uoj
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import market
from pydantic import ValidationError


def main(argv: list[str]) -> int:
    fetch = "--fetch" in argv
    names = [arg for arg in argv if not arg.startswith("-")]

    if names:
        paths = [market.PLUGINS_DIR / f"{Path(name).stem}.toml" for name in names]
    else:
        paths = sorted(market.PLUGINS_DIR.glob("*.toml"))

    if not paths:
        print("没有插件元数据需要校验。")
        return 0

    failed = False
    for path in paths:
        errors: list[str] = []
        if not path.exists():
            errors.append("文件不存在")
        else:
            try:
                meta = market.load_plugin(path)
            except tomllib.TOMLDecodeError as exc:
                errors.append(f"TOML 解析失败：{exc}")
            except (ValidationError, ValueError) as exc:
                errors.extend(market.format_errors(exc))
            else:
                if meta.name != path.stem:
                    errors.append(f"name `{meta.name}` 与文件名 `{path.stem}` 不一致")
                elif fetch:
                    errors.extend(market.verify_remote(meta))

        if errors:
            failed = True
            print(f"[FAIL] {path.name}")
            for error in errors:
                print(f"    - {error}")
        else:
            print(f"[ OK ] {path.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
