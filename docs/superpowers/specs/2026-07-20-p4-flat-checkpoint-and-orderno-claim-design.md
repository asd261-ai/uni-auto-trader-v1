# P4 設計:flat-checkpoint FIFO 重錨 + live orderno 認領過濾(2026-07-20)

7/19 雙倉審計兩項 deferred 設計題的正式設計。兩者共同主題:**共用帳號 + 無 client token 環境下,把「bot 自己的事件」與「帳號上其他事件」隔離**。

---

## P4-1 flat-checkpoint:孤兒 close 不再毒化 carry-lookback FIFO

### 問題(審計 pnl_calc.py:223)
carry fix 把 FIFO 掃描起點推前 5 天。若 lookback 內有一筆「開倉腿缺漏、只剩平倉腿」的孤兒 close(斷線時 match 事件永遠沒進 orders.jsonl),FIFO 把孤兒 S 當 open 腿,與其後的 B 錯配,毒化持續到孤兒滑出 5 天窗為止。熔斷輸入(conservative min())兩引擎可同時失真。

### 設計:orders.jsonl 增 `flat` 事件 + FIFO 在 flat 點 reset
- **寫入端(strategy)**:poll loop 每輪檢查 `_position_is_flat()`(P3 已加鎖版);由非 flat → flat 的**轉換瞬間**寫一筆 `{"event":"flat","ts":...}`(order_log.log_event,append-only、與現有事件同管道)。boot 時若 flat 也寫一筆(boot checkpoint)。
- **讀取端(pnl_calc.realized_day_pts)**:掃描範圍內收集 flat 事件 ts,合併進每個 productid 的 fill 流(排序 tie-break:同秒時 flat 排在 fill 之後,因為轉換偵測必然晚於成交);`_fifo` 遇 flat 標記時,**若 open-legs 非空 → 全部丟棄**(= 資料洞被吸收,該些腿不計任何損益),並回報丟棄數供 caller log WARNING。
- **效果**:孤兒腿的毒化半徑從「最多 5 天」縮到「單一 flat-to-flat episode」(MTX bot 每次完整平倉即 flat,episode 通常以小時計)。丟棄=不計損益,與既有 min() 保守哲學一致(缺腿本來就該算 missing,不該亂配)。

### 取捨(誠實記錄)
- **Zombie 情境**:bot 自認 flat 但 broker 實際有殘留(歷史上有,recon 會大聲告警)→ checkpoint 寫錯位置,可能把一組真實 pair 從中切斷 → 該 pair 不計(undercount,保守方向)。接受:罕見 + recon 已有獨立告警 + 失真方向與 min() 同向。
- flat 判定含 FVG 單位(paper FVG 開倉期間不寫 checkpoint)→ checkpoint 較稀疏 = 更接近舊行為,安全側。
- 舊 orders.jsonl 無 flat 事件 → 行為完全等同現狀(向後相容,無遷移)。

## P4-2 live orderno 認領:手動同商品事件與 bot 隔離

### 問題(審計 strategy.py:705 + on_fill 696)
帳號 0239174 的 reply/match callback 是整帳號廣播。on_fill 只比對 productid+隊首 bs 就 pop pend → Sean 手動同商品同向成交會被錯配進 bot unit(entry_fill 記手動價,FILL_ANCHOR 把 Worker 停損錨到手動價);手動拒單也會進 on_order_rejected(P0 的 15s freshness 窗只縮小、未消除)。

### 設計:把 pnl_calc.bot_ordernos 的認領規則搬到 live(新純模組 `orderno_claim.py`)
沿用 6/19 provenance 設計已驗證的語意,零新發明:
- `note_sent(ts, productid, bs)`:_send_order issend 成功後登記(對應 orders.jsonl 的 sent 事件)。
- `note_reply(ts, productid, bs, orderno, ...)`:**每個 orderno 只有第一筆 reply 可認領**(7/6 dup-reply 事故的同款防護);認領條件 = 同 productid+bs、sent 之後 `LINK_WINDOW_SEC`(3s,與 pnl_calc._LINK_WINDOW_SEC 一致,真實資料 send→reply 同秒)內、最舊優先。認領成功 → orderno 進 bot 集合、消耗該 sent。拒單 reply 同樣認領+消耗(該單已死,不會再有 match)。回傳是否認領成功=「這筆 reply 是不是我們的」。
- `is_bot_fill(orderno)`:match 到達時查集合。
- 集合/佇列有界(sent 超窗清除、orderno 集合 cap 200)。

### 接線(trader.py)+ 三段式 arm(env `FILL_ORDERNO_FILTER`,預設 **observe**)
| mode | _on_match(fill 歸因) | _on_reply(拒單路由) |
|---|---|---|
| `off` | 現行為 | 現行為 |
| `observe`(預設) | 外來 fill 照舊進 on_fill,但 log `[orderno-filter OBSERVE] would-skip` | 外來拒單照舊進 on_order_rejected,log would-skip |
| `on` | 外來 fill 不進 on_fill(orders.jsonl 照記,pnl_calc 不受影響) | 外來拒單不觸發 rollback |

- **為何 observe 先行**:這是 fill 歸因主路徑,[[feedback-no-paper-env-validate-trader-live]] 紀律=live observe-first。observe 期收集「would-skip 且事後被證明真是手動單」的 log,≥1 週零誤判再提 arm(ask-first)。
- **邊界已知**:①our reply 遲到 >10s / restart 橫跨 in-flight → 認領失敗,on-mode 會把自己的事件當外來(→ 漏 rollback=回到 6/1 前行為,phantom 由 recon+null-fill 網接);restart 情境現狀本就不處理(pends 是 memory-only),無迴歸。②bot 送單與手動單同秒同向:視窗內雙 sent 排隊,認領可能錯抓——與 pnl_calc 既有假設同構,不新增風險。

## 實作順序(TDD)
1. RED:test_pnl_calc(flat reset 4 例)、test_orderno_claim(新檔 7 例)、test_audit_p4_wiring(flat 轉換 hook + trader match/reply 過濾 observe/on)
2. GREEN:order_log 無改動(log_event 通用)、pnl_calc `_fifo`+realized_day_pts、strategy `_log_flat_transition()`、orderno_claim.py、trader 接線
3. 全套迴歸 → commit。部署照舊 needs-sean(併入待部署 trader 包)。
