# trades.jsonl 真實成交 P&L 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** trades.jsonl 每筆 additive 加 `entry_fill`/`exit_fill`/`pnl_pts_real` 三欄真實成交基準 P&L，不動既有 signal 欄位與下單流程。

**Architecture:** 把可純測的邏輯（真實 P&L 算式、延後 record 的 finalize、超時掃描、state 序列化）抽進新純模組 `real_fill_pnl.py`，用 `python3 -m unittest` 全測（沿用 repo `order_reject.py`/`feed_schema.py` 慣例）。strategy.py 只做機械式 wiring（延後寫 + on_fill 回補 + poll 超時 + state 持久化），無 paper env 故走 observe-first live 驗證。

**Tech Stack:** Python 3（stdlib unittest，無第三方依賴）；strategy.py（broker SDK 在 trader 層，本模組不碰）。

**Spec:** `docs/superpowers/specs/2026-06-06-trades-jsonl-real-fill-pnl-design.md`

---

## 檔案結構

- **Create** `real_fill_pnl.py` — 純函式：`compute_pnl_pts_real`、`finalize_exit`、`due_records`、`serialize_pending`/`deserialize_pending`。無 I/O、無 SDK、`now_ms` 由呼叫端傳入（可純測）。
- **Create** `test_real_fill_pnl.py` — 純 unittest，覆蓋 spec §7 可純測的 1/4/5 + 序列化 round-trip。
- **Modify** `strategy.py`：
  - `_record_trade`（681）— 新增 `entry_fill`/`exit_fill`/`pnl_pts_real` 三個 kwarg，寫進 record dict。
  - `_close_unit`（1623）— 真實單（`place_order=True`）改延後寫；paper 立刻寫（nulls）。
  - `on_fill`（514）exit 分支 — 回補 fill + finalize + 落地寫。
  - `_poll_loop`（556）— 每輪掃超時 pending 補寫。
  - mtx/fvg state persist/restore — 含 `_pending_exit_records`。

> ⚠️ strategy.py 五處 wiring 無法本地單測（需 broker SDK + trader）。本計畫的 TDD 紅綠燈全在 `real_fill_pnl.py` 純模組；strategy.py 整合靠 observe-first live 驗證（spec §8）。動 strategy.py 前 **ask-first**、deploy 走 scp + precheck SOP。

---

### Task 1: `compute_pnl_pts_real` — 真實 P&L 算式（純）

**Files:**
- Create: `real_fill_pnl.py`
- Test: `test_real_fill_pnl.py`

- [ ] **Step 1: Write the failing test**

```python
"""Pure unittest for real_fill_pnl. No broker-SDK / no I/O.
Run:  python3 -m unittest test_real_fill_pnl -v
WHY: real-fill P&L must NEVER fall back to signal values — a missing fill must
read as null, not a fabricated number, or real-money attribution silently lies.
"""
import unittest
import real_fill_pnl as rfp


class ComputePnlPtsReal(unittest.TestCase):
    def test_long_uses_exit_minus_entry(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100, 46160), 60)

    def test_short_uses_entry_minus_exit(self):
        self.assertEqual(rfp.compute_pnl_pts_real("short", 46470, 46450), 20)

    def test_missing_entry_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, 46160))

    def test_missing_exit_fill_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("short", 46470, None))

    def test_both_missing_is_none(self):
        self.assertIsNone(rfp.compute_pnl_pts_real("long", None, None))

    def test_result_is_rounded_int(self):
        self.assertEqual(rfp.compute_pnl_pts_real("long", 46100.4, 46160.4), 60)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_real_fill_pnl.ComputePnlPtsReal -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'real_fill_pnl'`

- [ ] **Step 3: Write minimal implementation**

```python
# real_fill_pnl.py
"""Pure helpers for trades.jsonl real-fill P&L (task B). No I/O, no broker SDK.
Caller passes now_ms so timeout logic stays deterministic and unit-testable."""
from typing import Optional, List, Dict, Any


def compute_pnl_pts_real(dir_: str, entry_fill, exit_fill) -> Optional[int]:
    """Real-fill P&L in points. Returns None if EITHER fill is missing —
    never substitute a signal value (real-money attribution must not lie)."""
    if entry_fill is None or exit_fill is None:
        return None
    diff = (exit_fill - entry_fill) if dir_ == "long" else (entry_fill - exit_fill)
    return round(diff)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_real_fill_pnl.ComputePnlPtsReal -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add real_fill_pnl.py test_real_fill_pnl.py
git commit -m "feat(real-fill-pnl): pure compute_pnl_pts_real (null when fill missing)"
```

---

### Task 2: `finalize_exit` — 把 exit fill 寫進延後 record（純）

**Files:**
- Modify: `real_fill_pnl.py`
- Test: `test_real_fill_pnl.py`

- [ ] **Step 1: Write the failing test**

```python
class FinalizeExit(unittest.TestCase):
    def _record(self, dir_="long", entry_fill=46100):
        # mirrors the trades.jsonl record dict shape (key "dir", "entry_fill")
        return {"dir": dir_, "entry_fill": entry_fill,
                "exit_fill": None, "pnl_pts_real": None}

    def test_sets_exit_fill_and_pnl_when_both_present(self):
        rec = self._record(dir_="long", entry_fill=46100)
        out = rfp.finalize_exit(rec, 46160)
        self.assertEqual(out["exit_fill"], 46160)
        self.assertEqual(out["pnl_pts_real"], 60)

    def test_short_direction(self):
        rec = self._record(dir_="short", entry_fill=46470)
        rfp.finalize_exit(rec, 46450)
        self.assertEqual(rec["pnl_pts_real"], 20)

    def test_missing_entry_fill_leaves_pnl_none(self):
        rec = self._record(dir_="long", entry_fill=None)
        rfp.finalize_exit(rec, 46160)
        self.assertEqual(rec["exit_fill"], 46160)
        self.assertIsNone(rec["pnl_pts_real"])

    def test_timeout_flush_exit_fill_none(self):
        # poll-loop timeout path: no real fill arrived → exit_fill=None, pnl None
        rec = self._record(dir_="long", entry_fill=46100)
        rfp.finalize_exit(rec, None)
        self.assertIsNone(rec["exit_fill"])
        self.assertIsNone(rec["pnl_pts_real"])

    def test_mutates_in_place_and_returns_same_object(self):
        rec = self._record()
        self.assertIs(rfp.finalize_exit(rec, 46160), rec)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_real_fill_pnl.FinalizeExit -v`
Expected: FAIL — `AttributeError: module 'real_fill_pnl' has no attribute 'finalize_exit'`

- [ ] **Step 3: Write minimal implementation**

Append to `real_fill_pnl.py`:

```python
def finalize_exit(record: Dict[str, Any], exit_fill) -> Dict[str, Any]:
    """Stamp exit_fill + pnl_pts_real onto a deferred trade record, in place.
    exit_fill=None is the timeout-flush case → pnl_pts_real stays None."""
    record["exit_fill"] = exit_fill
    record["pnl_pts_real"] = compute_pnl_pts_real(
        record.get("dir"), record.get("entry_fill"), exit_fill)
    return record
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_real_fill_pnl.FinalizeExit -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add real_fill_pnl.py test_real_fill_pnl.py
git commit -m "feat(real-fill-pnl): finalize_exit stamps exit_fill + pnl_pts_real"
```

---

### Task 3: `due_records` — poll-loop 超時掃描（純）

**Files:**
- Modify: `real_fill_pnl.py`
- Test: `test_real_fill_pnl.py`

- [ ] **Step 1: Write the failing test**

```python
class DueRecords(unittest.TestCase):
    def _pe(self, deadline_ms):
        # a "pending exit" awaiting fill: carries record + flush deadline
        return {"record": {"id": deadline_ms}, "deadline_ms": deadline_ms}

    def test_returns_only_past_deadline(self):
        pending = [self._pe(100), self._pe(200), self._pe(300)]
        due = rfp.due_records(pending, now_ms=200)
        self.assertEqual([p["deadline_ms"] for p in due], [100, 200])

    def test_inclusive_boundary(self):
        pending = [self._pe(200)]
        self.assertEqual(len(rfp.due_records(pending, now_ms=200)), 1)

    def test_none_due_returns_empty(self):
        pending = [self._pe(500)]
        self.assertEqual(rfp.due_records(pending, now_ms=200), [])

    def test_missing_deadline_treated_as_due(self):
        # defensive: a malformed entry (no deadline) should flush, not linger forever
        pending = [{"record": {"id": 1}}]
        self.assertEqual(len(rfp.due_records(pending, now_ms=0)), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_real_fill_pnl.DueRecords -v`
Expected: FAIL — `AttributeError: module 'real_fill_pnl' has no attribute 'due_records'`

- [ ] **Step 3: Write minimal implementation**

Append to `real_fill_pnl.py`:

```python
def due_records(pending: List[Dict[str, Any]], now_ms: int) -> List[Dict[str, Any]]:
    """Pending-exit entries whose flush deadline has passed (timeout candidates).
    A missing deadline_ms is treated as due (0) so malformed entries never linger."""
    return [p for p in pending if p.get("deadline_ms", 0) <= now_ms]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_real_fill_pnl.DueRecords -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add real_fill_pnl.py test_real_fill_pnl.py
git commit -m "feat(real-fill-pnl): due_records selects timed-out pending exits"
```

---

### Task 4: `serialize_pending`/`deserialize_pending` — state 持久化 round-trip（純）

**Files:**
- Modify: `real_fill_pnl.py`
- Test: `test_real_fill_pnl.py`

- [ ] **Step 1: Write the failing test**

```python
class SerializePending(unittest.TestCase):
    def test_round_trip_preserves_records(self):
        pending = [
            {"record": {"id": 1, "dir": "long", "entry_fill": 46100,
                        "exit_fill": None, "pnl_pts_real": None}, "deadline_ms": 123},
            {"record": {"id": 2, "dir": "short", "entry_fill": None,
                        "exit_fill": None, "pnl_pts_real": None}, "deadline_ms": 456},
        ]
        blob = rfp.serialize_pending(pending)
        # must be plain JSON-able (goes into the mtx/fvg state file)
        import json
        restored = rfp.deserialize_pending(json.loads(json.dumps(blob)))
        self.assertEqual(restored, pending)

    def test_deserialize_none_is_empty_list(self):
        self.assertEqual(rfp.deserialize_pending(None), [])

    def test_deserialize_drops_entries_without_record(self):
        self.assertEqual(rfp.deserialize_pending([{"deadline_ms": 1}]), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_real_fill_pnl.SerializePending -v`
Expected: FAIL — `AttributeError: module 'real_fill_pnl' has no attribute 'serialize_pending'`

- [ ] **Step 3: Write minimal implementation**

Append to `real_fill_pnl.py`:

```python
def serialize_pending(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Plain JSON-able snapshot of pending-exit records for the state file."""
    return [{"record": p["record"], "deadline_ms": p.get("deadline_ms", 0)}
            for p in pending if p.get("record") is not None]


def deserialize_pending(blob) -> List[Dict[str, Any]]:
    """Rebuild pending-exit list from state-file blob (None → empty).
    Entries without a record are dropped (corruption-safe)."""
    if not blob:
        return []
    return [{"record": e["record"], "deadline_ms": e.get("deadline_ms", 0)}
            for e in blob if isinstance(e, dict) and e.get("record") is not None]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_real_fill_pnl -v`
Expected: PASS (all classes; full module green)

- [ ] **Step 5: Commit**

```bash
git add real_fill_pnl.py test_real_fill_pnl.py
git commit -m "feat(real-fill-pnl): serialize/deserialize pending exits for state file"
```

---

> **Tasks 5–9 動 strategy.py（真錢 hot-path）。無本地單測（無 SDK/paper），逐步 wiring 後 ask-first 部署、observe-first 驗證。每個 Task 仍各自 commit。**

### Task 5: `_record_trade` 新增三欄（additive，最低風險）

**Files:**
- Modify: `strategy.py:681` (`_record_trade` 簽名 + record dict)

- [ ] **Step 1: 在 `_record_trade` 簽名加三個 kwarg（預設 None，向後相容）**

```python
    def _record_trade(self, *, source: str, label: str, dir_: str, entry, exit_price,
                       stop, target, pnl_pts: float, reason: str, sig_id, opened_at_ms,
                       entry_fill=None, exit_fill=None, pnl_pts_real=None):
```

- [ ] **Step 2: 在 record dict（line 694-710）三個既有欄位之後、`reason` 之前插入三欄**

在 `"pnl_ntd": ...,` 之後加：

```python
                "entry_fill":   entry_fill,
                "exit_fill":    exit_fill,
                "pnl_pts_real": pnl_pts_real,
```

- [ ] **Step 3: import 純模組**（strategy.py top，line 24 `import order_reject` 之後）

```python
import real_fill_pnl
```

- [ ] **Step 4: 本地 import sanity（不啟動交易）**

Run: `python3 -c "import real_fill_pnl; import ast; ast.parse(open('strategy.py').read()); print('ok')"`
Expected: `ok`（語法 + 純模組 import 通過；strategy 全 import 需 SDK 故只做語法檢查）

- [ ] **Step 5: Commit**

```bash
git add strategy.py
git commit -m "feat(trades-log): _record_trade accepts entry_fill/exit_fill/pnl_pts_real (additive)"
```

---

### Task 6: `_close_unit` 延後寫（真實單）+ paper 立刻寫

**Files:**
- Modify: `strategy.py:1623` (`_close_unit`)
- Modify: `strategy.py:303` (`__init__`：新增 `self._pending_exit_records`)

- [ ] **Step 1: `__init__` 加 pending 容器**（line 303 `self._pending_fills` 旁）

```python
        self._pending_exit_records: List[dict] = []   # task B: deferred trades.jsonl records awaiting real exit fill
```

- [ ] **Step 2: 加常數**（strategy.py 常數區，POINT_VALUE line 114 附近）

```python
EXIT_FILL_TIMEOUT_MS = 60_000   # task B: flush deferred trade record (exit_fill=null) if no real fill in 60s
```

- [ ] **Step 3: 改 `_close_unit` 末段的寫檔邏輯**

把現有「立刻 `_record_trade`」（line 1662-1667）替換為「真實單延後 / paper 立刻」。先備 record kwargs，再分流：

```python
        record_kwargs = dict(
            source=source, label=unit["sig_label"], dir_=unit["dir"],
            entry=unit["entry"], exit_price=exit_price, stop=unit["stop"],
            target=unit["target"], pnl_pts=pnl_pts, reason=reason,
            sig_id=unit["id"], opened_at_ms=unit.get("opened_at"),
            entry_fill=unit.get("entry_fill"),
        )
        if place_order:
            # Real order: defer the write until on_fill stamps the real exit_fill.
            # Attach the record to the already-registered exit pending_fill (last
            # appended at line ~1640) AND track it for timeout/restart flush.
            pe = {"record": record_kwargs, "deadline_ms": int(time.time() * 1000) + EXIT_FILL_TIMEOUT_MS}
            self._pending_fills[-1]["pe"] = pe          # the exit pending registered above
            self._pending_exit_records.append(pe)
        else:
            # Paper (FVG observe): no broker fill will ever come → write now with nulls.
            self._record_trade(**record_kwargs)
```

> 注意：`record_kwargs` 用 `dir_`/`exit_price`/`sig_id` 對應 `_record_trade` 簽名；entry/exit/pnl_pts 仍是 signal 基準（不變）。`exit_fill`/`pnl_pts_real` 不放進 kwargs → 由 `_record_trade` 預設 None；真實單由 on_fill finalize 後才補。

- [ ] **Step 4: 確認 state persist 仍在延後寫之後**（line 1676-1679 不動；unit 已從 `_units` 移除，pending record 獨立持有資料）

- [ ] **Step 5: 語法檢查 + commit**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('ok')"`
Expected: `ok`

```bash
git add strategy.py
git commit -m "feat(trades-log): defer real-order trade write until exit fill (paper writes immediately)"
```

---

### Task 7: `on_fill` exit 分支回補真實 fill + 落地寫

**Files:**
- Modify: `strategy.py:514` (`on_fill`，exit 分支 line 526-527)

- [ ] **Step 1: 改 exit 分支**（目前 `if pend["kind"] != "entry": return` 直接丟棄 exit fill）

把 line 526-527：

```python
            if pend["kind"] != "entry":
                return  # exit fill — nothing to anchor
```

替換為：

```python
            if pend["kind"] != "entry":
                # exit fill: stamp the real exit price onto the deferred record (task B),
                # then write trades.jsonl now (Layer ① still owns realised P&L from orders.jsonl).
                pe = pend.get("pe")
                if pe is not None and pe in self._pending_exit_records:
                    self._pending_exit_records.remove(pe)
                    rec = real_fill_pnl.finalize_exit(pe["record"], price)
                    self._record_trade(**rec)
                return
```

> `pe["record"]` 是 Task 6 存進去的 `record_kwargs`；`finalize_exit` 加上 `exit_fill`/`pnl_pts_real` 後它就是完整的 `_record_trade(**kwargs)`。`record_kwargs` 已含這兩個 key（finalize 寫入），與簽名相容。

- [ ] **Step 2: 確認 `_record_trade` 在 `on_fill`（broker thread）呼叫安全**

`on_fill` 已在 `with self._lock` 內（line 522）；`_record_trade` 不另取 lock、純檔案 append + in-memory 計數，安全。

- [ ] **Step 3: 語法檢查 + commit**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('ok')"`
Expected: `ok`

```bash
git add strategy.py
git commit -m "feat(trades-log): on_fill stamps real exit_fill and writes deferred record"
```

---

### Task 8: poll-loop 超時補寫安全網

**Files:**
- Modify: `strategy.py:556` (`_poll_loop`)

- [ ] **Step 1: 在 `_poll_loop` 的 try 區塊內（line 562-565 那組 `_check_*` 之後）加超時掃描**

```python
                self._flush_due_exit_records()
```

- [ ] **Step 2: 新增 method（放在 `on_fill` 附近，需取 `self._lock`）**

```python
    def _flush_due_exit_records(self):
        """Safety net: flush deferred trade records whose real exit fill never
        arrived within EXIT_FILL_TIMEOUT_MS → write with exit_fill=null + warn.
        Worst case is the same information as before this feature (no lost row)."""
        with self._lock:
            now_ms = int(time.time() * 1000)
            due = real_fill_pnl.due_records(self._pending_exit_records, now_ms)
            for pe in due:
                self._pending_exit_records.remove(pe)
                rec = real_fill_pnl.finalize_exit(pe["record"], None)
                self._record_trade(**rec)
                logger.warning(
                    f"[real-fill] exit fill timeout (>{EXIT_FILL_TIMEOUT_MS//1000}s) "
                    f"src={rec.get('source')} id={rec.get('id')} reason={rec.get('reason')} "
                    f"→ wrote exit_fill=null"
                )
```

- [ ] **Step 3: 語法檢查 + commit**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('ok')"`
Expected: `ok`

```bash
git add strategy.py
git commit -m "feat(trades-log): poll-loop flushes timed-out deferred exit records (exit_fill=null)"
```

---

### Task 9: state persist/restore pending records（重啟安全網）

**Files:**
- Modify: `strategy.py` `_save_mtx_state`/`_save_fvg_state` 與對應 restore 路徑（grep 確認實際函式名/欄位）

- [ ] **Step 1: 先定位 state 寫/讀**

Run: `grep -nE 'def _save_mtx_state|def _save_fvg_state|def _load|mtx_restore|reconcile_restore|save_mtx_state' strategy.py`
Expected: 找到 save/restore 函式與 state dict 組裝處。

- [ ] **Step 2: save 時把 pending 序列化進 state dict**

在 mtx state dict（與 fvg 對應）組裝處加一欄：

```python
            "pending_exit_records": real_fill_pnl.serialize_pending(self._pending_exit_records),
```

> 若 mtx/fvg 共用同一 `_pending_exit_records`（混在一起），只在 mtx state 存一次即可，避免雙寫重複；restore 也只從 mtx state 讀一次。實作時依 Step 1 結果決定單寫點。

- [ ] **Step 3: restore 時讀回並立即 flush（exit_fill=null）**

在 restore 路徑（reconcile 之後、恢復交易之前）：

```python
        restored_pe = real_fill_pnl.deserialize_pending(state.get("pending_exit_records"))
        for pe in restored_pe:
            rec = real_fill_pnl.finalize_exit(pe["record"], None)
            self._record_trade(**rec)
            logger.warning(f"[real-fill] restart flush deferred record "
                           f"src={rec.get('source')} id={rec.get('id')} → exit_fill=null")
```

> 重啟即 flush（不等 fill）：重啟後原 broker fill 已無對應 in-memory pending，等不到；持倉本身由 reconcile 處理。寧可 exit_fill=null 也不漏整筆。

- [ ] **Step 4: 語法檢查 + commit**

Run: `python3 -c "import ast; ast.parse(open('strategy.py').read()); print('ok')"`
Expected: `ok`

```bash
git add strategy.py
git commit -m "feat(trades-log): persist+restart-flush deferred exit records (no lost row)"
```

---

### Task 10: 全綠 + 部署前關卡（ask-first）

- [ ] **Step 1: 純模組全測綠**

Run: `python3 -m unittest test_real_fill_pnl -v`
Expected: PASS（Task 1-4 全部，~18 tests）

- [ ] **Step 2: 既有測試無回歸**（確認沒動到別的）

Run: `python3 -m unittest test_order_reject test_fill_schema test_atr_gate test_exit_reason -v`
Expected: PASS（全綠，本功能未觸碰這些模組）

- [ ] **Step 3: diff 自審 + 請 code-reviewer**

走 `superpowers:requesting-code-review`（真錢 code，重點：延後寫的 lost-row 風險、lock 正確性、FIFO `pe` 對齊、paper/real 分流、月度計數器搬移時點）。

- [ ] **Step 4: ⛔ STOP — ask-first 部署**

**不要自行 deploy。** 向 Sean 報告：純測全綠 + review 結果 + 「我要 scp strategy.py + real_fill_pnl.py 到 VPS、跑 precheck.sh && restart，影響真倉 trade-log 寫入路徑、不影響下單與熔斷。可不可以？」拿到明確 yes 才動（[[feedback-irreversible-ask-first]]）。

- [ ] **Step 5: 部署（核可後）**

`scp` strategy.py + real_fill_pnl.py + test_real_fill_pnl.py 到 VPS → `sha256sum` 驗 drift（[[feedback-vps-trader-deploy-scp]]）→ `000_Agent/scripts/trader-precheck.sh && systemctl restart uni-trader`（[[feedback-trader-service-precheck-sop]]）。

- [ ] **Step 6: observe-first 前向驗證**

部署後盯首批真實單的 trades.jsonl：`entry_fill`/`exit_fill` 有值、`pnl_pts_real` 與 orders.jsonl FIFO 對得上、無 60s timeout warning 異常洪水、signal 欄位不變。惡化即 revert（git + scp 舊版）。

---

## Self-Review（plan vs spec）

- **spec §2 三欄 additive** → Task 5（record dict）✓
- **spec §4.1 entry_fill 零風險** → Task 5（`unit.get("entry_fill")`）✓
- **spec §4.2 延後寫真實單 / paper 立刻** → Task 6 ✓
- **spec §4.2 on_fill finalize + 落地** → Task 7 ✓
- **spec §4.3 安全網①超時 60s** → Task 8 ✓
- **spec §4.3 安全網②重啟 flush** → Task 9 ✓
- **spec §7 測試 1（back-compat）** → Task 10 Step 2（既有測試無回歸）+ signal 欄位未改 ✓
- **spec §7 測試 2（entry_fill null）** → Task 1 `test_missing_entry_fill_is_none` + Task 2 `test_missing_entry_fill_leaves_pnl_none` ✓
- **spec §7 測試 3（async 回補）** → Task 7 wiring（observe-first live；純層由 finalize 測覆蓋）✓
- **spec §7 測試 4（pnl_pts_real 缺一即 null）** → Task 1/2 ✓
- **spec §7 測試 5（超時補寫）** → Task 2 `test_timeout_flush` + Task 3 due_records ✓
- **spec §7 測試 6（重啟 flush）** → Task 4 round-trip + Task 9 wiring ✓
- **spec §7 測試 7（paper 立刻寫）** → Task 6 else 分支（observe-first live）✓
- **spec §8 部署規範** → Task 10 Step 4-6 ✓
- **型別一致**：`finalize_exit`/`compute_pnl_pts_real`/`due_records`/`serialize_pending`/`deserialize_pending` 在 Task 5-9 引用與 Task 1-4 定義一致 ✓
- **placeholder 掃描**：無 TBD/TODO；strategy.py wiring 步驟均附實際 code；Task 9 Step 1 明確要求先 grep 定位（因 save/restore 確切函式名待確認，非 placeholder 而是 discovery step）✓
