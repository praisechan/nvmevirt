# Session Report — Block-Level Read Reclaim in NVMeVirt

**Date:** 2026-05-31 · **Module:** `nvmev_rr` (renamed for coexistence) · **Base config:** `SAMSUNG_970PRO` (`CONFIG_NVMEVIRT_SSD := y`) · **Kernel:** 6.8.0-111-generic

> Purpose: record what was added to stock NVMeVirt to implement **read-disturb-driven read reclaim**, so a later session can follow up / extend. Design background: `read_reclaim_research.md` (§6). Staged runbook: `experiment.md`. Turnkey build/run: `QUICKSTART.md`.

---

## 1. Brief summary

Implemented **block-level read reclaim** in the `conv_ftl` FTL:
- A **per-physical-block read counter** (`nand_block.read_cnt`) is bumped on every NAND sense.
- When a block's counter reaches `READ_RECLAIM_THRESHOLD`, its **whole line (superblock) is reclaimed** — valid pages relocated + every block erased — reusing the existing GC machinery (research doc §6.2 **Option A**: per-block counting, line-granular reclaim).
- Reclaim is **deferred to the end of each `conv_read` partition** (never mid-sense) and a `printk` + counter give observability.

**Verified (A/B, same binary, only `READ_RECLAIM_THRESHOLD` changed):**

| Run | reclaim events | seq-read BW |
|---|---|---|
| RRT=8 (on) | 45,057+/instance in 180 s | avg 954, **dips to 652 MiB/s** (−36%) |
| RRT=1e9 (off) | 0 | flat **1006 MiB/s** (min 983) |

Baseline (Stage A, reclaim-free) = 1003 MiB/s. Bandwidth dips appear **only** with reclaim enabled and **only after blocks reach the threshold** → causation confirmed. 0 fio errors throughout. Two independent code-review passes gated the kernel load (2 CRITICAL bugs found + fixed before first insmod).

---

## 2. Critical code changes vs. original NVMeVirt

All changes are additive; no stock behavior was removed. Referenced by function (line numbers drift).

### 2.1 `ssd.h` — per-block counter
```c
struct nand_block {
    ...
    int wp;
    unsigned int read_cnt; /* reads since last erase (read-disturb / read-reclaim) */
};
```
Rationale: read disturb is a **physical-block** property; one counter per `(ch,lun,pl,blk)`. `unsigned` avoids signed-overflow UB.

### 2.2 `ssd_config.h` — threshold (always-compiled region, after the per-model `#endif`)
```c
#ifndef READ_RECLAIM_THRESHOLD
#define READ_RECLAIM_THRESHOLD (8)   /* 1000000000 = effectively disabled (A/B control) */
#endif
```
`#ifndef`-guarded so the A/B run can override via `-D`. **RRT is reads/block and is geometry-sensitive** — see §4.

### 2.3 `conv_ftl.h` — observability counter
```c
struct conv_ftl {
    ...
    unsigned long read_reclaim_cnt; /* # of read-reclaim events on this instance */
};
```
Initialized to 0 in `conv_init_ftl()`.

### 2.4 `conv_ftl.c` — counting, reset, and reclaim

**(a) Reset on erase** — in `mark_block_free()`:
```c
blk->erase_cnt++;
blk->read_cnt = 0; /* erase wipes accumulated read disturb */
```

**(b) Count per sense** — new inline `rr_account_read()` (per **flash-page sense**, not per host cmd / per 4KB page; fires exactly-once at the threshold):
```c
static inline void rr_account_read(struct conv_ftl *conv_ftl, struct ppa *ppa,
                                   struct line **rc_lines, int *rc_n) {
    struct nand_block *blk = get_blk(conv_ftl->ssd, ppa);
    ...
    if (++blk->read_cnt != READ_RECLAIM_THRESHOLD) return;   /* "==" => no retry-spam */
    line = get_line(conv_ftl, ppa);
    /* dedup into rc_lines[] (cap RR_MAX_PER_CMD=64) */
}
```

**(c) Hook into the read path** — in `conv_read()`:
- declared `struct line *rc_lines[RR_MAX_PER_CMD]; int rc_n, rc_i;`
- `rc_n = 0;` reset per partition iteration
- call `rr_account_read(conv_ftl, &prev_ppa, rc_lines, &rc_n)` at **both** `ssd_advance_nand(NAND_READ)` sense sites (the in-loop flush and the "remaining io" tail)
- **after** all senses for the partition, the deferred reclaim:
```c
for (rc_i = 0; rc_i < rc_n; rc_i++)
    read_reclaim_line(conv_ftl, rc_lines[rc_i]);
```

**(d) The reclaim** — new `read_reclaim_line()`, mirrors `do_gc()` but for one externally-selected line:
```c
if (line == wp.curline || line == gc_wp.curline) return;          /* never the active write line */
if (lm->free_line_cnt <= conv_ftl->cp.gc_thres_lines) return;     /* GC-margin free-line guard */
if (line->pos) { pqueue_remove(victim_line_pq, line); line->pos = 0; victim_line_cnt--; }
else           { if (line->vpc != pgs_per_line) return; list_del_init(&line->entry); full_line_cnt--; }
/* per flashpg/ch/lun: clean_one_flashpg() (relocate valid pages); last flashpg: mark_block_free + NAND_ERASE */
mark_line_free(conv_ftl, &ppa);
conv_ftl->read_reclaim_cnt++;
NVMEV_INFO("read-reclaim: line=%d copied=%d erased_blks=%u RRT=%d total_reclaims=%lu\n", ...);
```

### 2.5 Why these exact choices (from the two review passes — important for follow-up)
- **Detach by `line->pos`, NOT by `ipc`.** `pqueue_remove()` (pqueue.c) is unguarded — a `pos==0` line would write to sentinel slot `q->d[0]` and corrupt the heap. `line->pos != 0` is the codebase's authoritative "in victim_pq" flag (`mark_page_invalid`). Must also manually `line->pos = 0` after removal (pqueue_remove doesn't clear it; `select_victim_line` does on pop).
- **Free-line guard uses `gc_thres_lines`**, not `> 0` — relocation may roll `gc_wp` onto a fresh line; running dry would trip the `prepare_write_pointer` assert (panic).
- **Deferred (post-sense) reclaim** — relocating mid-loop would invalidate the maptbl entries the read loop is still about to sense (consecutive pages share a line).
- **Single-IO-worker assumption is load-bearing** (`CPUS=22,23` → 1 dispatcher + 1 worker): `conv_read` and `do_gc` never interleave, so no locking was added (matches stock). The defensive `vpc != pgs_per_line` check declines (rather than panics) if that invariant is ever broken by a future multi-worker change.

---

## 3. Supporting (non-FTL) changes

| File | Change | Why |
|---|---|---|
| `Kbuild`, `main.c`, `reload.sh` | module renamed `nvmev` → **`nvmev_rr`** (obj/objs, `/proc/nvmev_rr`, `MODNAME`) | coexist with another user's stock `nvmev` on this shared host (`experiment.md` §1e) |
| `reload.sh` | unload check `grep -q "^nvmev_rr\b"` → **`grep -qw nvmev_rr`** | stale-module detection failed once → `insmod: File exists` |
| `nvmev-evaluation/common/set_perf_rr.py` | **new** (copy of `set_perf.py` targeting `/proc/nvmev_rr`) | stock `set_perf.py` hardcodes `/proc/nvmev` → silently no-ops for our renamed module |
| `rr_run.sh` | **new** one-shot: rmmod→reload→pin-our-device-by-size→set_perf→prep-write→seq-read→report reclaims+BW | repeatable Stage-C runs; auto-pins the 3.7 GiB node |
| `_grub_add_region.py` | **new** helper used once to append `memmap=4G$64G` to grub | reserve our own physical region (one-time) |

---

## 4. Notes for the next session

- **Device identity, not node number.** Our device is the **3827 MiB** node with `/proc/nvmev_rr` present, in the `memmap=4G$64G` (64G) region. When the other user's `nvmev` isn't loaded, the kernel names ours `nvme1n1` (not the doc's expected `nvme2n1`). The model string `CSL_Virt_MN_01` is shared by both modules — useless to disambiguate; **use size + `/proc/nvmev_rr`**. Never touch `nvme0n1` (1.8 TB real Crucial disk) or the other user's 15 GiB node.
- **Block geometry is small *by configuration*.** With `memmap=4G` and fixed `BLKS_PER_PLN=8192`, the derived `pgs_per_blk = 8` (block = 1 flash page = 32 KB). The **counting/reclaim logic is geometry-independent** (per-block, incremented per sense); only **RRT** is tuned to this size. For physically-realistic blocks (hundreds of pages), enlarge the `memmap` region (e.g. 64 GiB → `pgs_per_blk=128`, `flashpgs_per_blk=16`) and rescale RRT (→ 10⁴–10⁶). See the 2026-05-31 explanation / `read_reclaim_research.md` §6.
- **Module is currently left loaded at RRT=8** (working reclaim state).
- **Possible extensions:** (1) true per-physical-block reclaim (research doc §6.2 Option B — needs a block-level free pool); (2) WL-level reclaim (STRAW); (3) export reclaim metrics (count, page-copies, extra erases → WA, p99/p999 read latency) via `/proc/nvmev_rr/`; (4) re-arm reclaim for blocks skipped because their line was the active write pointer (current `==` trigger drops them until the next erase — benign for verification, see §2.5).
- **Build/run:** `gcc-12`, no `KCFLAGS` on 6.8. sudo across agent shells needs `/etc/sudoers.d/nvmev-timeout` (`timestamp_timeout=120` + `!tty_tickets`).
