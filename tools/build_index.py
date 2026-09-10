#!/usr/bin/env python3
"""由 `plugins/*.toml` 生成 `index.json`。

先做模型校验；再查询各仓库对应版本 release 取资产 sha256（需网络与可选 GITHUB_TOKEN）。
"""

from __future__ import annotations

import json
import sys
import tomllib

import market
from pydantic import ValidationError


def main() -> int:
    plugins: dict[str, market.PluginMeta] = {}
    failed = False

    for path in sorted(market.PLUGINS_DIR.glob("*.toml")):
        try:
            meta = market.load_plugin(path)
        except tomllib.TOMLDecodeError as exc:
            print(f"[FAIL] {path.name}\n    - TOML 解析失败：{exc}")
            failed = True
            continue
        except (ValidationError, ValueError) as exc:
            print(f"[FAIL] {path.name}")
            for error in market.format_errors(exc):
                print(f"    - {error}")
            failed = True
            continue

        if meta.name != path.stem:
            print(f"[FAIL] {path.name}\n    - name `{meta.name}` 与文件名 `{path.stem}` 不一致")
            failed = True
            continue
        plugins[meta.name] = meta

    if failed:
        return 1

    try:
        index, warnings = market.build_index(plugins)
    except Exception as exc:  # noqa: BLE001 - 汇总网络/资产错误
        print(f"[FAIL] 生成索引失败：{exc}")
        return 1

    for warning in warnings:
        print(f"[WARN] {warning}")

    market.INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {market.INDEX_FILE.name}，共 {len(index['plugins'])} 个插件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
