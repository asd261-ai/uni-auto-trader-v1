# MTX ② 突破進場 — 全時段 Demote(trader-side per-code skip)— Design Spec

日期:2026-06-13(六)
決策人:Sean(6/13 per-signal 回顧決議,demote 走 ask-first 流程)
狀態:spec approved-pending-review → plan → implement → deploy(deploy 另行 ask-first)

## 1. 背景與動機

② 突破進場(Worker `getEnhancedSignal` code 2:站上 MA20 + 收盤破前 10 根 K 高點 + 量 >1.3× 20 根均量,機械上 long-only、~90% fire 在夜盤)是慢性軟失血訊號:

- 真實成交 5/15→6/9 夜多 n=31:mean −22.9/筆,90% CI [−41, −4.8] 上界 <0 排除零。
- 四個獨立窗口方向一致(archive 校正後 −89、6/6 baseline n13 −222、本週 real n5 −67)。
- 濾網救援路線已窮盡且全 NO-GO:over-extension skip(假說反向)、ATR/突破距離(反指標)、retest-entry(not-yet 不賺)。失血不是子集問題,是訊號整體期望值為負。
- 6/1 研究:日盤 ②(n=4)mean −52.8 更差 → 全時段訊號層級問題,故 demote 範圍=**全時段**,不做時段切割。

期望效益:−22.9/筆 × ~6 筆/週 ≈ +135 pts/週(停止失血)。

## 2. 決策摘要

| 項 | 決定 |
|---|---|
| 範圍 | ② 全時段(日盤+夜盤)新進場不下真倉單 |
| 機制 | trader 端 env-gated per-code skip(`MTX_DEMOTE_CODES`) |
| paper 延續 | Worker history 不受影響,② 照 fire 照記=自動 paper 帳(6/12 鎖定夜已實證此機制) |
| 通知 | Health Bot 每筆通知(Sean 6/13 選定,比照 ④ ATR skip 部署期慣例) |
| pyramid | demote gate 在加碼分支(strategy.py ~1503)之前 continue,故連加碼單(pyramid #2)一併擋掉,與 regime gate / ATR-skip gate 一致 |
| 可逆性 | unset env + restart 即還原 |

淘汰方案:Worker 端 shouldFire blackout(gate 會讓交易不進 history,paper 帳斷頭=盲飛);Worker tag + trader 認 tag(橫跨兩系統,改動面大)。

## 3. 實作設計

### 3.1 新模組 `demote_gate.py`(pure function,比照 `atr_gate.py`)

```python
def parse_demote_codes(raw: str | None) -> frozenset[int]:
    # "2" / "2,3" → {2}/{2,3};None/""/壞值(非整數 token)→ 忽略該 token
    # 全壞或未設 → frozenset() = disabled
def should_demote(sig_code, demote_codes) -> bool:
    # sig_code 非法(None/非 int 可轉)→ False(fail-open,壞資料不誤殺)
    # 回 True 僅當 int(sig_code) in demote_codes
```

設計哲學與 ATR skip 相同:**fail-open**——env 沒設、解析失敗、sigCode 缺失,一律不擋。

### 3.2 `strategy.py` 插閘

- 位置:訊號消費迴圈,regime gate 之後、④ ATR skip 之前。
- 條件:`source == "mtx"` 且 `should_demote(trade.get("sigCode"), DEMOTE_CODES)`。
- 動作:`logger.info` 一行(含 code/dir/entry/id/session)+ Health Bot 背景通知
  `🚫 Demoted | code{N} {dir} entry=X id=Y [session]`(dir 取自 trade dict,不寫死)+ `self._last_seen_id[source] = trade_id` + `continue`。
- 不分方向(全時段全方向——② 雖機械上 long-only,gate 不依賴此假設)。
- module-level 讀 env:`DEMOTE_CODES = parse_demote_codes(os.getenv("MTX_DEMOTE_CODES"))`,與 `SKIP_CODE_4_ATR_GT` 同模式(restart 生效,不熱載)。

### 3.3 環境變數

VPS `.env` 加 `MTX_DEMOTE_CODES=2`。未設/空 = gate 完全停用(預設行為不變,可安全先 deploy code 再開 env)。

## 4. 測試

pytest(`test_demote_gate.py`,比照 atr_gate 測試慣例,持久 commit 非 throwaway):

1. parse:未設/空/`"2"`/`"2,3"`/`" 2 , 3 "`/`"2,x,3"`(壞 token 忽略)/`"abc"`(全壞=空集合)
2. should_demote:命中/不命中/sigCode None/sigCode 字串 `"2"`/空集合永遠 False
3. strategy 整合:demote 命中時不進 `_enter`、last_seen 有推進;env 未設時行為與現行完全一致(回歸保護)
4. pyramid 加碼分支在此閘之後(strategy.py ~1503),demoted code 會在到達加碼分支前被 continue 擋掉=連加碼一併停;與 regime/ATR-skip gate 一致。

## 5. 部署與驗收(ask-first,另拿 GO)

1. scp `demote_gate.py` + `strategy.py` 至 VPS(先 `sha256sum` 對 drift,防蓋掉 VPS-only patch)
2. Sean 或 Bob 在 VPS `.env` 加 `MTX_DEMOTE_CODES=2`(非 secret,Bob 可動)
3. `trader-precheck.sh && systemctl restart uni-trader`(`&&` 不准 `;`)
4. 驗收:週一第一筆 ② fire → Health Bot 收到 Demoted 通知、orders.jsonl 無對應 `sent`、Worker history 照記該筆
5. 回滾:刪 env + precheck && restart

## 6. Re-promote 條件(防跨 session 漂移)

- 時點:② 在 Worker paper 帳累積 **≥30 筆或 ≥4 週** 後重評(約 7/11 後)。
- 門檻:demote 後僅剩 paper 證據,而 paper 有**滑價+wick 雙盲區**(6/12 實證:13:17 ④ real −102 vs paper +49),故要求 **paper mean ≥ +15/筆(滑價折讓)且 CI 下界 >0** 才進 re-promote 討論;屆時 ask-first。
- 觀察期間禁止:對 ② 重啟濾網研究(over-ext / ATR / 突破距離已 NO-GO,反例條件見 mtx-retest-entry-backtest memory)。

## 7. 風險與明文接受項

- **趨勢參與減少**:② 是唯一突破追強腿,demote 後強趨勢段只剩 ⑧ 回踩參與(沒回踩=空手)。接受:期望值為負的參與不是參與。
- **demote 全擋(含加碼)**:demote gate 在 pyramid 分支(strategy.py ~1503)之前 `continue`,故 demoted code 連加碼單(pyramid #2)一併擋掉,與 regime/ATR-skip gate 行為一致。設計取捨:既然判定該訊號期望值為負,任何形式的加碼(包含對既有獲利倉加碼)都不應放行;n 極小、無乾淨的「② 當加碼觸發」數據,從嚴。
- **證據窗口無真 OOS**:n=31 混平靜+panic 週;但 demote 是移除風險非新增規則,且完全可逆,接受較低證據門檻。
