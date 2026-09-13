"""下载 HearthstoneJSON 中文全量卡表并精简为 carddb 所需字段。

用法: python scripts/fetch_cards.py [--out data/cache/cards.zh.json]

用 cards.json(全卡表)而非 cards.collectible.json: 附魔(e后缀)/衍生物(t后缀)/
英雄技能等不可收藏卡不在 collectible 子集里, 缺了只会显示原始 card_id
(2026-09-13 实测: JAIL_907e02/CS2_017o/JAIL_877t 全部裸 ID)。
只保留 carddb.py 读取的字段, 缓存文件体积与加载时间保持可控。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

URL = "https://api.hearthstonejson.com/v1/latest/zhCN/cards.json"
KEEP = ("id", "name", "cost", "type", "dbfId", "text")


def main() -> int:
    ap = argparse.ArgumentParser(description="刷新中文卡表缓存(全量+精简)")
    ap.add_argument("--out", default="data/cache/cards.zh.json")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    print(f"下载 {args.url} ...")
    req = urllib.request.Request(args.url, headers={
        "User-Agent": "hsbot-fetch-cards/1.0 (github.com/hsbot)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        cards = json.loads(resp.read().decode("utf-8"))
    slim = [{k: c[k] for k in KEEP if c.get(k) is not None} for c in cards]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    named = sum(1 for c in slim if c.get("name"))
    print(f"完成: {len(slim)} 张卡(有名字 {named}) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
