# trades.jsonl 真實成交 P&L 欄位（任務 B）— 設計

**日期**：2026-06-06
**狀態**：設計已核可（Sean 6/6），待 writing-plans
**前置背景**：[[feedback-real-pnl-orders-not-trades-jsonl]]（6/5 立的鐵律）、[[project-trades-jsonl-real-fill-pnl]]、[[project-fill-anchor-stop-study]]

---

## 1. 問題

`trades.jsonl` 每筆 record 的 `pnl_pts = exit_signal − entry_signal`（`strategy.py:1644`、`_close_unit`），是 **signal 基準**，系統性少報真實滑價。6/5 實證：整夜 signal 報 −91，真實成交 −325（telescoping 證 Σ賣−Σ買=−325），滑價 −234。

真錢分析的鐵律已改為「一律認 orders.jsonl 真實成交 FIFO」，但 orders.jsonl 是逐筆掛單流水、無 per-trade 歸因。目標是讓 `trades.jsonl` 每筆**內建**真實進出場 fill 與真實 P&L，使單筆歸因可直接用真實基準（在共用佇列限制內）。

## 2. 目標

`trades.jsonl` 每筆 record **additive** 新增 3 欄，既有欄位完全不動：

| 欄位 | 意義 |
|---|---|
| `entry_fill` | 真實進場 broker matchprice（無則 `null`） |
| `exit_fill` | 真實出場 broker matchprice（無則 `null`） |
| `pnl_pts_real` | 真實成交基準 P&L；`entry_fill` 與 `exit_fill` **兩者皆在**才算，缺一即 `null` |

**非目標（YAGNI）**：
- 不加 `entry_slip`/`exit_slip` 欄位（可由 `fill − signal` 推導）。
- 不改既有 `entry`/`exit`/`pnl_pts`（signal 基準，熔斷靠 pnl_calc Layer ① 不靠它）。
- 不改任何下單流程。
- 不追求帳戶 netted 情境下的完美單筆歸因（物理限制，見 §6）。

## 3. 既有 code 事實（已讀 `strategy.py` @ `ba8546a`）

- 寫檔：`_record_trade`（line 681）→ append `TRADES_LOG_PATH`，同時 `trade_log_emit.send`（Worker 雲端備份，dedup by `(id, reason)`）+ 更新月度計數器。
- `_close_unit`（line 1623）：送出場單（1633/1635）→ 註冊一筆 exit `_pending_fills`（1640，**目前不帶 unit reference**）→ 立刻算 signal `pnl_pts`（1644）→ `_session_trades.append`（1652）→ **立刻** `_record_trade`（1662）→ 從 `_units` 移除 unit（1673）→ persist state（1676-1679）。
- 真實**進場** fill：`on_fill`（line 514）entry 分支已把 `price` 存進 `unit["entry_fill"]`（531）。→ **寫檔時 `entry_fill` 已可得，零時序問題**。
- 真實**出場** fill：`on_fill` exit 分支（526-527）目前 pop 完即 return（「nothing to anchor」），**price 被丟棄**。exit pending 不帶 unit/record reference。→ **這是唯一的非同步難點**。
- `_open_unit`（1528）：entry pending 帶 `"unit": <unit>`（1570），record 結構含 `"entry_fill": None`（1564）。
- paper 來源（FVG observe，`place_order=False`）：`_close_unit` 不送真單（1629/1631），**永遠不會有真實 exit fill**。

## 4. 設計方案（已核可）

**全 live、內建 `trades.jsonl`、機制 = 延後寫（deferred-write）。**
（淘汰的方案：先寫後改 in-place patch — append-only JSONL 改單行太脆、hot-path race、會動到重寫權威交易日誌，真錢不採。）

### 4.1 entry_fill（零風險）

`_record_trade` 寫檔時 record 直接帶入 `unit.get("entry_fill")`（close 時讀，已填則有值、未填則 `null`）。

### 4.2 exit_fill / pnl_pts_real（延後寫）

**真實單路徑（`place_order=True`）**：

1. `_close_unit` **不立刻**呼叫 `_record_trade`。改為：
   - 備好完整 record dict（含 `entry_fill`、signal 欄位、`exit_fill=None`、`pnl_pts_real=None`）。
   - 把該 record 掛到 exit `_pending_fills` 那筆（鏡像 entry pending 帶 `unit` 的做法），並加入一個 `_pending_exit_records` 結構供超時/重啟掃描。
   - 照舊從 `_units` 移除 unit + persist state（持倉已平，狀態要即時反映）。
2. 真實 exit fill 回 `on_fill` exit 分支：
   - 取出掛在該 pending 的 record → 填 `exit_fill = price`。
   - 若 `entry_fill` 也在 → 算 `pnl_pts_real`（long: `exit_fill − entry_fill`；short: `entry_fill − exit_fill`）；任一缺 → `pnl_pts_real = None`（**絕不用 signal 值頂替**）。
   - **此時才** append `trades.jsonl` + `trade_log_emit.send` + 更新月度計數器（搬到落地時點，淨效果相同，延後約 1-2s）。
   - 從 `_pending_exit_records` 移除。

**Paper 路徑（`place_order=False`）**：維持 `_close_unit` 立刻寫，`exit_fill=null`、`pnl_pts_real=null`（永無真 fill）。

### 4.3 兩道安全網

1. **超時補寫**：poll loop（`_poll_loop`，週末也跑）每輪掃 `_pending_exit_records`，凡掛載超過 **60 秒**仍無 fill → 用 `exit_fill=null`、`pnl_pts_real=null` 補寫該 record + `logger.warning`（loud），並從 pending 移除。最壞情況等同今日資訊量，不漏整筆。
2. **重啟安全**：`_pending_exit_records` 序列化進現有 mtx/fvg state 檔（close 時本來就 persist）。重啟載入時，先把未決的 pending records 以 `exit_fill=null` flush 落地，再恢復交易。

> 超時後才到的「遲到 fill」：pending 已移除 → 直接忽略（orders.jsonl 仍是真實成交的權威備援）。可接受。

## 5. 影響分析

- **熔斷**：`_check_daily_loss_lock` 讀 pnl_calc Layer ①（orders.jsonl），**不讀**這些月度計數器（code 註解 1668-1672 明載）→ 延後寫不影響熔斷。
- **月度計數器**：display-only（monthly summary heartbeat），延後 ~1-2s 落地，淨值相同。
- **record 順序**：改為 fill-到達順序（≈ 送單 FIFO 順序）而非 close 順序；超時補寫的 record 可能略微亂序。record 帶 `ts`/`trading_day`，分析端本就排序 → 可接受，文件註明。
- **Worker dedup**：`(id, reason)` 去重；每筆只 append 一次（超時 flush 與遲到真寫互斥，pending 移除後不再寫）→ 無重複。

## 6. 歸因限制（誠實標註）

MTX 與 FVG 共用 MXFF6，broker 端 FIFO netted。`exit_fill` 靠 `on_fill` 既有的 FIFO + `bs` 對齊（與 fill-anchor 進場同機制）。`on_fill` line 523 已有防呆：佇列 front 的 `bs` 與來單不符 → 視為手動/外來單、保持佇列不動。手動單與 bot 單交錯時，單筆 `exit_fill` 歸因仍可能錯配；**帳戶 netted 時單筆歸因不完美是已知物理限制**，日/總額層級才精確（與 6/5 結論一致）。

## 7. 測試（TDD，先寫測試後實作）

1. **back-compat**：signal `entry`/`exit`/`pnl_pts` 在所有路徑不變。
2. **entry_fill**：entry fill 已填 → record 帶正確值；entry fill 缺 → `null`。
3. **exit fill 非同步回補**：真實單 close 後 record **不**立刻落地；exit fill 到 `on_fill` 才 append，`exit_fill` 正確。
4. **pnl_pts_real**：雙 fill 皆在 → 算式正確（long/short 各一）；缺任一 → `null`（不頂替 signal）。
5. **超時補寫**：掛載 >60s 無 fill → `exit_fill=null` 落地 + warning。
6. **重啟 flush**：state 檔含 pending → 重啟以 `exit_fill=null` 補寫。
7. **paper 路徑**：`place_order=False` → 立刻寫、`exit_fill=null`、`pnl_pts_real=null`。

測試需持久化（commit），不用 throwaway heredoc（[[feedback-journal-test-claims-not-evidence]]）。

## 8. 部署規範（真錢 code）

- **observe-first**：新欄位 additive、暫無 consumer，先前向觀察 live 真實單欄位填得對（無 paper env，[[feedback-no-paper-env-validate-trader-live]]）。
- **ask-first**：動 `strategy.py` 下單/log 流程前問 Sean（[[feedback-irreversible-ask-first]]）。
- **precheck SOP**：restart 前跑 `000_Agent/scripts/trader-precheck.sh && restart`（[[feedback-trader-service-precheck-sop]]）。
- **deploy 經 scp**：VPS HEAD 永久 stale，working tree 手動複製 + sha256sum 驗 drift（[[feedback-vps-trader-deploy-scp]]）。

## 9. 退路

若延後寫 live 整合在實作期暴露無法接受的風險，退回 **C（nightly 對帳）**：archive sync 時由 orders.jsonl 重算每筆真實 P&L，產對帳版 trade log，零 live 風險（但真實欄位不在 trades.jsonl 內）。
