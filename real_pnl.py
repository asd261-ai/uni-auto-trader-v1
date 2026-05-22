"""
READ-ONLY 真實損益計算器。從 stdin 讀 journalctl 輸出,抓 MXFF6 的實際 Match 成交價,
FIFO 配對算「真實 realized P&L(點)」+ 目前未平倉。不下任何單、不碰交易。

用法:
  sudo journalctl -u uni-trader.service --since '08:45:00' --no-pager | python3 real_pnl.py

只配對單腳 MXFF6 市價成交(排除如 'MXFF6/G6' 的價差單)。每口視為 1 lot。
"""
import sys
import re

PAT = re.compile(r"Match \| MXFF6 ([BS]) price=([\d.]+) qty=(\d+)")

fills = []
for line in sys.stdin:
    m = PAT.search(line)
    if m:
        bs, price, qty = m.group(1), float(m.group(2)), int(m.group(3))
        fills.extend([(bs, price)] * qty)

pos = []          # 未平倉腳: list of (side 'L'/'S', entry_price)
realized = 0.0    # 已實現點數
closed = 0        # 已平倉筆數
for bs, price in fills:
    side = "L" if bs == "B" else "S"
    if pos and pos[0][0] != side:          # 反向 → 平倉
        oside, oprice = pos.pop(0)
        realized += (price - oprice) if oside == "L" else (oprice - price)
        closed += 1
    else:                                   # 同向或空手 → 開倉
        pos.append((side, price))

open_desc = ",".join(f"{s}@{p:.0f}" for s, p in pos) if pos else "flat"
print(f"REAL_PNL_PTS={realized:+.0f} CLOSED={closed} OPEN={open_desc} FILLS={len(fills)}")
