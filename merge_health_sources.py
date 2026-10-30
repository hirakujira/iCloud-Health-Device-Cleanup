#!/usr/bin/env python3
"""合併「健康」App 來源列表中重複的 iPhone / Apple Watch。

原始資料庫一律以唯讀方式開啟；加上 --apply 時才會複製到 --out 並修改複本。

規則：
- 舊 iPhone 的紀錄併入目前的 iPhone，舊 Apple Watch 的紀錄併入目前的手錶。
  未指定時，目前的 iPhone 是資料庫標記為本機（local_device）的裝置，
  目前的手錶是最後配對（nano_pairing）的手錶；可用 --iphone-target / --watch-target 指定。
- 和其他裝置在同一時段重疊的紀錄不併入目標，改放到同組的「舊裝置」來源
  （--iphone-legacy / --watch-legacy，預設為紀錄最多的舊裝置），
  避免同一來源內重複加總步數、距離等累計值。
- 其餘舊裝置來源比照 healthd 刪除來源的方式標記為 deleted，不刪除任何紀錄。
- 指定裝置的參數可以用預覽列表中的 lid，或 com.apple.health.<UUID> 形式的 bundle ID。
"""

import argparse
import bisect
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

APPLE_EPOCH = 978307200
DEVICE_PREFIX = "com.apple.health."
MAIN_DB = "healthdb.sqlite"
SECURE_DB = "healthdb_secure.sqlite"

PROV_COLS = [
    "sync_provenance", "origin_product_type", "origin_build", "local_product_type",
    "local_build", "source_id", "device_id", "contributor_id", "source_version", "tz_name",
    "origin_major_version", "origin_minor_version", "origin_patch_version", "sync_identity",
    "derived_flags",
]
UNIQUE_COLS = [c for c in PROV_COLS if c != "derived_flags"]

SAMPLES_BY_TYPE = (
    "SELECT sm.data_id, sm.start_date, sm.end_date, o.provenance, o.type "
    "FROM samples sm JOIN objects o ON o.data_id = sm.data_id WHERE sm.data_type = ?"
)

@dataclass
class Device:
    lid: int
    bundle: str
    kind: str
    rows: list  # [(sid, name, product_type, uuid_hex)]
    objects: int = 0
    last_seen: float = 0.0


@dataclass
class Group:
    kind: str
    label: str
    target_lid: int
    legacy_lid: int
    legacy_name: str
    target_sid: int = 0
    legacy_sid: int = 0
    old_lids: list = field(default_factory=list)  # 依優先順序排列（較新的裝置優先）
    moving_sids: set = field(default_factory=set)
    note: str = ""


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(ro_uri(path), uri=True)


def ro_uri(path: Path) -> str:
    # 路徑含空白、#、? 等字元時必須編碼，否則 SQLite 會把它當成 URI 參數
    return f"file:{quote(str(path))}?immutable=1&mode=ro"


def log(msg=""):
    print(msg, flush=True)


def apple_date(ts):
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d", time.gmtime(ts + APPLE_EPOCH))


def load_devices(main, sec):
    devices = {}
    for sid, lid, bundle, name, ptype, deleted, uuid_hex in main.execute(
        "SELECT so.ROWID, so.logical_source_id, l.bundle_id, so.name, so.product_type, "
        "so.deleted, hex(so.uuid) FROM sources so "
        "JOIN logical_sources l ON l.ROWID = so.logical_source_id "
        "WHERE l.bundle_id LIKE ? ORDER BY so.ROWID",
        (DEVICE_PREFIX + "%",),
    ):
        dev = devices.get(lid)
        if dev is None:
            dev = devices[lid] = Device(lid, bundle, "", [])
        dev.rows.append((sid, name, ptype, uuid_hex))
    for dev in devices.values():
        types = {r[2] for r in dev.rows}
        if any(t.startswith("Watch") for t in types):
            dev.kind = "W"
        elif any(t.startswith("iPhone") for t in types):
            dev.kind = "P"

    sid_to_lid = {r[0]: d.lid for d in devices.values() for r in d.rows}
    per_sid = {}
    now = time.time() - APPLE_EPOCH
    for sid, n, last in sec.execute(
        "SELECT p.source_id, count(*), max(CASE WHEN sm.end_date <= ? THEN sm.end_date END) "
        "FROM objects o JOIN data_provenances p ON p.ROWID = o.provenance "
        "JOIN samples sm ON sm.data_id = o.data_id GROUP BY p.source_id",
        (now,),
    ):
        per_sid[sid] = (n, last or 0.0)
        lid = sid_to_lid.get(sid)
        if lid is not None:
            devices[lid].objects += n
            devices[lid].last_seen = max(devices[lid].last_seen, last or 0.0)
    return devices, per_sid


def primary_sid(dev, per_sid):
    want = dev.bundle[len(DEVICE_PREFIX):].replace("-", "").upper()
    for sid, _, _, uuid_hex in dev.rows:
        if uuid_hex == want:
            return sid
    return max(dev.rows, key=lambda r: (per_sid.get(r[0], (0, 0))[0], -r[0]))[0]


def detect_targets(main):
    """回傳 {"P": 本機 iPhone 的 bundle ID, "W": 最後配對手錶的 bundle ID}，找不到時為 None。"""
    row = main.execute(
        "SELECT l.bundle_id FROM sources so JOIN logical_sources l ON l.ROWID = so.logical_source_id "
        "WHERE so.local_device = 1 AND so.deleted = 0 AND l.bundle_id LIKE ? "
        "ORDER BY so.ROWID DESC LIMIT 1", (DEVICE_PREFIX + "%",)).fetchone()
    phone = row[0] if row else None
    # 換錶後舊手錶的配對紀錄可能還留著，ROWID 最大的才是最後配對的
    row = main.execute(
        "SELECT n.source_bundle_id FROM nano_pairing n "
        "JOIN logical_sources l ON l.bundle_id = n.source_bundle_id "
        "ORDER BY n.ROWID DESC LIMIT 1").fetchone()
    return {"P": phone, "W": row[0] if row else None}


def to_bundle(main, value):
    """裝置參數可以是 lid 或 bundle ID，一律轉成 bundle ID。"""
    if value is None or not value.isdigit():
        return value
    row = main.execute("SELECT bundle_id FROM logical_sources WHERE ROWID = ?", (int(value),)).fetchone()
    if not row:
        sys.exit(f"找不到 lid {value} 的來源")
    return row[0]


def build_plan(devices, per_sid, args, detected):
    by_bundle = {d.bundle: d for d in devices.values()}
    keep = set(args.keep or [])
    groups = {}
    specs = [("P", "iPhone", "iphone", "本機"), ("W", "Apple Watch", "watch", "最後配對")]
    for kind, label, opt, auto in specs:
        target, note = getattr(args, f"{opt}_target"), "手動指定"
        if target is None:
            target, note = detected[kind], f"自動判斷：{auto}"
        candidates = [d for d in devices.values() if d.kind == kind and d.bundle not in keep]
        t = by_bundle.get(target)
        if t is None:
            if len(candidates) > 1:
                sys.exit(f"無法判斷目前的 {label}，請用 --{opt}-target 指定（lid 或 bundle ID）")
            log(f"{label}：沒有需要合併的舊裝置")
            continue
        olds = [d for d in candidates if d.lid != t.lid]
        if not olds:
            log(f"{label}：沒有需要合併的舊裝置")
            continue
        legacy = getattr(args, f"{opt}_legacy")
        # 重疊資料借用一個舊裝置的來源存放，不必另外建立來源
        lg = by_bundle.get(legacy) if legacy else max(olds, key=lambda d: (d.objects, -d.lid))
        if lg is None or t.kind != kind or lg.kind != kind or lg.lid == t.lid or lg.bundle in keep:
            sys.exit(f"{label} 的目標或舊裝置來源設定不正確")
        g = Group(kind, label, t.lid, lg.lid, getattr(args, f"{opt}_legacy_name"), note=note)
        g.target_sid = primary_sid(t, per_sid)
        g.legacy_sid = primary_sid(lg, per_sid)
        olds.sort(key=lambda d: d.last_seen, reverse=True)
        g.old_lids = [d.lid for d in olds]
        g.moving_sids = {r[0] for d in olds for r in d.rows}
        groups[kind] = g
    return groups


def merge_union(starts, ends, new):
    if not new:
        return starts, ends
    combined = list(zip(starts, ends))
    combined.extend(new)
    combined.sort()
    out_s, out_e = [], []
    for s, e in combined:
        if out_s and s <= out_e[-1]:
            if e > out_e[-1]:
                out_e[-1] = e
        else:
            out_s.append(s)
            out_e.append(e)
    return out_s, out_e


def overlaps(starts, ends, s, e):
    i = bisect.bisect_right(ends, s)
    return i < len(starts) and starts[i] < e


def assign(sec, devices, groups):
    """回傳要放到「舊裝置」來源的 data_id 集合（依組別）與統計。"""
    lid_group = {}
    for g in groups.values():
        lid_group[g.target_lid] = g.kind
        for lid in g.old_lids:
            lid_group[lid] = g.kind
    sid_info = {}
    for d in devices.values():
        if d.lid in lid_group:
            for r in d.rows:
                sid_info[r[0]] = (lid_group[d.lid], d.lid)
    prov_to = {}
    for pid, sid in sec.execute("SELECT ROWID, source_id FROM data_provenances"):
        if sid in sid_info:
            prov_to[pid] = sid_info[sid]

    legacy = {k: set() for k in groups}
    moved_by_lid = defaultdict(int)
    stacked = {k: 0 for k in groups}
    types = [r[0] for r in sec.execute("SELECT DISTINCT data_type FROM samples ORDER BY 1")]
    for dt in types:
        buckets = {k: defaultdict(list) for k in groups}
        for did, s, e, pid, otype in sec.execute(SAMPLES_BY_TYPE, (dt,)):
            info = prov_to.get(pid)
            # 瞬時樣本（start == end）不會被重複加總，所以不參與重疊判斷
            if info is None or otype != 1 or s is None or e is None or e <= s:
                continue
            buckets[info[0]][info[1]].append((s, e, did))
        for k, g in groups.items():
            b = buckets[k]
            if not b:
                continue
            starts, ends = merge_union([], [], [(s, e) for s, e, _ in b.get(g.target_lid, ())])
            l_starts, l_ends = [], []
            for lid in g.old_lids:
                items = b.get(lid)
                if not items:
                    continue
                kept, moved = [], []
                for s, e, did in items:
                    if overlaps(starts, ends, s, e):
                        legacy[k].add(did)
                        moved.append((s, e))
                    else:
                        kept.append((s, e))
                for s, e in moved:
                    if overlaps(l_starts, l_ends, s, e):
                        stacked[k] += 1
                moved_by_lid[lid] += len(moved)
                starts, ends = merge_union(starts, ends, kept)
                l_starts, l_ends = merge_union(l_starts, l_ends, moved)
    return legacy, moved_by_lid, stacked


def print_plan(devices, per_sid, groups, legacy, moved_by_lid, stacked):
    for g in groups.values():
        t = devices[g.target_lid]
        log(f"\n=== {g.label} ===")
        lg = devices[g.legacy_lid]
        log(f"目標：lid {t.lid} {t.rows[-1][1]}（{t.rows[-1][2]}，{t.objects:,} 筆，{g.note}）")
        log(f"重疊資料放到：lid {lg.lid} {lg.rows[-1][1]} → 改名為「{g.legacy_name}」")
        log(f"{'lid':>4}  {'名稱':<28} {'型號':<11} {'紀錄數':>10} {'移到舊裝置':>10}  最後資料")
        for lid in g.old_lids:
            d = devices[lid]
            name = d.rows[-1][1] or "(無名稱)"
            log(f"{lid:>4}  {name:<28} {d.rows[-1][2]:<11} {d.objects:>10,} {moved_by_lid.get(lid, 0):>10,}  {apple_date(d.last_seen or None)}")
        total = sum(devices[l].objects for l in g.old_lids)
        log(f"合計：{total:,} 筆舊紀錄，其中 {len(legacy[g.kind]):,} 筆因重疊放到「{g.legacy_name}」，"
            f"{stacked[g.kind]:,} 筆和其他舊裝置也重疊")


def make_snapshot(src: Path, out: Path) -> Path:
    """複製原始檔並把 -wal 內尚未寫回的交易併入主檔，之後才能用 immutable 模式讀取。"""
    out.mkdir(parents=True, exist_ok=False)
    snap = out / ".snapshot"
    snap.mkdir()
    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(src / name, snap / name)
        wal = src / (name + "-wal")
        if wal.exists():
            # 不複製 -shm：它只是索引，SQLite 會從 -wal 重建
            shutil.copy2(wal, snap / wal.name)
        conn = sqlite3.connect(snap / name)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for suffix in ("-wal", "-shm"):
            p = snap / (name + suffix)
            if p.exists() and p.stat().st_size == 0:
                p.unlink()
    return snap


def apply_changes(out: Path, groups, legacy, devices):
    sec = sqlite3.connect(out / SECURE_DB, isolation_level=None)
    # objects → data_provenances 是 ON DELETE CASCADE，必須關閉外鍵避免刪 provenance 時連帶刪除紀錄
    sec.execute("PRAGMA foreign_keys=OFF")
    sec.execute("PRAGMA cache_size=-524288")
    sec.execute("PRAGMA temp_store=MEMORY")
    sec.execute("ATTACH DATABASE ? AS m", (str(out / MAIN_DB),))
    report = {}
    sec.execute("BEGIN")
    try:
        for g in groups.values():
            if not legacy[g.kind]:
                # 沒有重疊資料時不需要保留舊裝置來源
                g.legacy_sid = g.target_sid

        sec.execute("CREATE TEMP TABLE legacy_objs(data_id INTEGER PRIMARY KEY)")
        sec.executemany("INSERT INTO legacy_objs VALUES (?)",
                        ((d,) for k in legacy for d in legacy[k]))
        sec.execute("CREATE TEMP TABLE moving(sid INTEGER PRIMARY KEY, target_sid INTEGER NOT NULL, "
                    "legacy_sid INTEGER NOT NULL)")
        for g in groups.values():
            sec.executemany("INSERT INTO moving VALUES (?,?,?)",
                            ((sid, g.target_sid, g.legacy_sid) for sid in g.moving_sids))

        sec.execute("CREATE TEMP TABLE pmap(old_pid INTEGER NOT NULL, kind TEXT NOT NULL, "
                    "new_pid INTEGER, PRIMARY KEY(old_pid, kind))")
        sec.execute(
            "INSERT INTO pmap(old_pid, kind) "
            "SELECT DISTINCT o.provenance, CASE WHEN l.data_id IS NULL THEN 'T' ELSE 'L' END "
            "FROM objects o JOIN data_provenances p ON p.ROWID = o.provenance "
            "JOIN moving mv ON mv.sid = p.source_id "
            "LEFT JOIN legacy_objs l ON l.data_id = o.data_id")
        # 沒有紀錄的 provenance 也要轉移，否則會留下指向已刪除來源的資料列
        sec.execute(
            "INSERT OR IGNORE INTO pmap(old_pid, kind) "
            "SELECT p.ROWID, 'T' FROM data_provenances p JOIN moving mv ON mv.sid = p.source_id "
            "WHERE NOT EXISTS (SELECT 1 FROM pmap x WHERE x.old_pid = p.ROWID)")

        where = " AND ".join(f"{c} = ?" for c in UNIQUE_COLS)
        rows = sec.execute(
            "SELECT pm.old_pid, pm.kind, mv.target_sid, mv.legacy_sid, "
            + ", ".join("p." + c for c in PROV_COLS)
            + " FROM pmap pm JOIN data_provenances p ON p.ROWID = pm.old_pid "
              "JOIN moving mv ON mv.sid = p.source_id").fetchall()
        inserted = reused = flag_diff = 0
        for old_pid, kind, tsid, lsid, *vals in rows:
            rec = dict(zip(PROV_COLS, vals))
            rec["source_id"] = tsid if kind == "T" else lsid
            hit = sec.execute(f"SELECT ROWID, derived_flags FROM data_provenances WHERE {where}",
                              [rec[c] for c in UNIQUE_COLS]).fetchone()
            if hit:
                new_pid = hit[0]
                reused += 1
                if hit[1] != rec["derived_flags"]:
                    flag_diff += 1
            else:
                cur = sec.execute(
                    f"INSERT INTO data_provenances ({', '.join(PROV_COLS)}) "
                    f"VALUES ({', '.join('?' * len(PROV_COLS))})",
                    [rec[c] for c in PROV_COLS])
                new_pid = cur.lastrowid
                inserted += 1
            sec.execute("UPDATE pmap SET new_pid = ? WHERE old_pid = ? AND kind = ?",
                        (new_pid, old_pid, kind))
        report["provenance_inserted"] = inserted
        report["provenance_reused"] = reused
        report["provenance_flag_diff"] = flag_diff

        cur = sec.execute(
            "UPDATE objects SET provenance = ("
            " SELECT pm.new_pid FROM pmap pm WHERE pm.old_pid = objects.provenance AND pm.kind ="
            "  CASE WHEN EXISTS (SELECT 1 FROM legacy_objs l WHERE l.data_id = objects.data_id)"
            "  THEN 'L' ELSE 'T' END)"
            " WHERE provenance IN (SELECT old_pid FROM pmap)")
        report["objects_updated"] = cur.rowcount

        stale = sec.execute(
            "SELECT count(*) FROM objects WHERE provenance IN ("
            " SELECT p.ROWID FROM data_provenances p JOIN moving mv ON mv.sid = p.source_id"
            " WHERE p.source_id != mv.legacy_sid)").fetchone()[0]
        if stale:
            raise RuntimeError(f"仍有 {stale} 筆紀錄指向要刪除的來源")
        cur = sec.execute("DELETE FROM data_provenances WHERE source_id IN "
                          "(SELECT sid FROM moving WHERE sid != legacy_sid)")
        report["provenance_deleted"] = cur.rowcount

        cur = sec.execute(
            "UPDATE external_sync_ids SET source_id = ("
            " SELECT p.source_id FROM objects o JOIN data_provenances p ON p.ROWID = o.provenance"
            " WHERE o.data_id = external_sync_ids.object_id)"
            " WHERE source_id IN (SELECT sid FROM moving)"
            " AND EXISTS (SELECT 1 FROM objects o WHERE o.data_id = external_sync_ids.object_id)")
        report["external_sync_ids_updated"] = cur.rowcount

        # 這些都是可重建的統計快取，來源歸屬改變後舊快取會失準
        for t in ("sample_aggregate_caches", "cached_queries", "sleep_day_summary_cache"):
            report[f"cleared_{t}"] = sec.execute(f"DELETE FROM {t}").rowcount

        for t, col in (("workout_builders", "source_id"), ("workout_sessions", "source_id"),
                       ("HealthRecordsPlugin_api_reminders", "source_id")):
            n = sec.execute(f"SELECT count(*) FROM m.{t} WHERE {col} IN (SELECT sid FROM moving "
                            f"WHERE sid != legacy_sid)").fetchone()[0]
            if n:
                raise RuntimeError(f"m.{t} 還有 {n} 筆引用要刪除的來源，請先結束進行中的體能訓練")
        for t, col in (("authorization", "source_id"), ("datatype_source_order", "source")):
            report[f"deleted_{t}"] = sec.execute(
                f"DELETE FROM m.{t} WHERE {col} IN (SELECT sid FROM moving WHERE sid != legacy_sid)"
            ).rowcount

        now = time.time() - APPLE_EPOCH
        row = sec.execute("SELECT ROWID FROM m.logical_sources WHERE bundle_id = ''").fetchone()
        trash_lid = row[0] if row else sec.execute(
            "INSERT INTO m.logical_sources (bundle_id) VALUES ('')").lastrowid
        for g in groups.values():
            if g.legacy_sid != g.target_sid:
                sec.execute("UPDATE m.sources SET name = ?, mod_date = ? WHERE ROWID = ?",
                            (g.legacy_name, now, g.legacy_sid))
        # 比照 healthd 刪除來源後留下的樣子：清空名稱並移到 bundle_id 為空字串的 logical source
        cur = sec.execute(
            "UPDATE m.sources SET deleted = 1, name = '', product_type = '', source_options = 0, "
            "logical_source_id = ?, mod_date = ? WHERE ROWID IN "
            "(SELECT sid FROM moving WHERE sid != legacy_sid)", (trash_lid, now))
        report["sources_soft_deleted"] = cur.rowcount

        dead = [lid for g in groups.values() for lid in g.old_lids
                if not sec.execute("SELECT 1 FROM m.sources WHERE logical_source_id = ?",
                                   (lid,)).fetchone()]
        if dead:
            marks = ",".join("?" * len(dead))
            report["deleted_logical_source_order"] = sec.execute(
                f"DELETE FROM m.logical_source_order WHERE logical_source IN ({marks})", dead).rowcount
            report["deleted_logical_sources"] = sec.execute(
                f"DELETE FROM m.logical_sources WHERE ROWID IN ({marks})", dead).rowcount
        sec.execute("COMMIT")
    except Exception:
        sec.execute("ROLLBACK")
        raise
    sec.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
    sec.execute("PRAGMA m.wal_checkpoint(TRUNCATE)")
    sec.close()
    return report


def cross_device_overlaps(items):
    """items: [(start, end, lid)]，回傳不同裝置之間互相重疊的樣本數。"""
    items.sort()
    best = []  # 最多保留兩個不同 lid 的 (end, lid)
    hits = 0
    for s, e, lid in items:
        if any(be > s and bl != lid for be, bl in best):
            hits += 1
        cand = {bl: be for be, bl in best}
        cand[lid] = max(cand.get(lid, e), e)
        best = sorted(((be, bl) for bl, be in cand.items()), reverse=True)[:2]
    return hits


def validate(src: Path, out: Path, groups, devices, legacy):
    ok = True
    s_src, s_out = ro(src / SECURE_DB), ro(out / SECURE_DB)
    m_out = ro(out / MAIN_DB)
    s_out.execute("ATTACH DATABASE ? AS m", (ro_uri(out / MAIN_DB),))

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        log(f"[{'OK' if cond else 'FAIL'}] {label}{('：' + str(detail)) if detail else ''}")

    for t in ("objects", "samples", "quantity_samples", "category_samples", "workouts",
              "data_series", "associations", "metadata_values"):
        a = s_src.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        b = s_out.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        check(f"{t} 筆數不變", a == b, f"{a:,} → {b:,}")
    q = "SELECT data_type, total(q.quantity) FROM quantity_samples q JOIN samples s USING (data_id) GROUP BY 1"
    check("每種數值資料的加總不變", dict(s_src.execute(q)) == dict(s_out.execute(q)))
    check("沒有紀錄指向不存在的 provenance", s_out.execute(
        "SELECT count(*) FROM objects o LEFT JOIN data_provenances p ON p.ROWID = o.provenance "
        "WHERE p.ROWID IS NULL").fetchone()[0] == 0)
    n = s_out.execute("SELECT count(*) FROM data_provenances p LEFT JOIN m.sources so "
                      "ON so.ROWID = p.source_id WHERE so.ROWID IS NULL OR so.deleted = 1").fetchone()[0]
    check("沒有 provenance 指向已刪除或不存在的來源", n == 0, n)
    n = s_out.execute("SELECT count(*) FROM external_sync_ids e JOIN m.sources so "
                      "ON so.ROWID = e.source_id WHERE so.deleted = 1").fetchone()[0]
    check("external_sync_ids 沒有指向已刪除的來源", n == 0, n)
    dup = s_out.execute("SELECT count(*) FROM (SELECT 1 FROM external_sync_ids WHERE deleted = 0 "
                        "GROUP BY source_id, object_code, sid HAVING count(*) > 1)").fetchone()[0]
    dup0 = s_src.execute("SELECT count(*) FROM (SELECT 1 FROM external_sync_ids WHERE deleted = 0 "
                         "GROUP BY source_id, object_code, sid HAVING count(*) > 1)").fetchone()[0]
    check("external_sync_ids 沒有新增重複的同步識別碼", dup <= dup0, f"{dup0} → {dup}")

    for g in groups.values():
        moving_objs = sum(devices[l].objects for l in g.old_lids)
        t_before = devices[g.target_lid].objects
        t_after = s_out.execute("SELECT count(*) FROM objects o JOIN data_provenances p ON "
                                "p.ROWID = o.provenance WHERE p.source_id = ?",
                                (g.target_sid,)).fetchone()[0]
        if g.legacy_sid != g.target_sid:
            l_after = s_out.execute("SELECT count(*) FROM objects o JOIN data_provenances p ON "
                                    "p.ROWID = o.provenance WHERE p.source_id = ?",
                                    (g.legacy_sid,)).fetchone()[0]
        else:
            l_after = 0
        other_target_rows = sum(n for sid, n in s_src.execute(
            "SELECT p.source_id, count(*) FROM objects o JOIN data_provenances p ON "
            "p.ROWID = o.provenance GROUP BY 1") if sid in
            {r[0] for r in devices[g.target_lid].rows} - {g.target_sid})
        exp_t = t_before - other_target_rows + moving_objs - len(legacy[g.kind])
        check(f"{g.label} 目標來源筆數符合預期", t_after == exp_t, f"{t_after:,}（預期 {exp_t:,}）")
        check(f"{g.label}「{g.legacy_name}」筆數符合預期", l_after == len(legacy[g.kind]),
              f"{l_after:,}（預期 {len(legacy[g.kind]):,}）")

    # 重新從原始檔取得每筆紀錄原本的裝置，確認目標來源內不同裝置之間沒有時間重疊
    sid_lid = {r[0]: d.lid for d in devices.values() for r in d.rows}
    prov_lid = {pid: sid_lid[sid] for pid, sid in
                s_src.execute("SELECT ROWID, source_id FROM data_provenances") if sid in sid_lid}
    target_sids = {g.target_sid: g for g in groups.values()}
    out_prov_sid = {pid: sid for pid, sid in
                    s_out.execute("SELECT ROWID, source_id FROM data_provenances") if sid in target_sids}
    overlaps_found = {g.kind: 0 for g in groups.values()}
    for (dt,) in s_src.execute("SELECT DISTINCT data_type FROM samples").fetchall():
        orig = {}
        for did, s, e, pid, otype in s_src.execute(SAMPLES_BY_TYPE, (dt,)):
            if otype == 1 and pid in prov_lid and s is not None and e is not None and e > s:
                orig[did] = (s, e, prov_lid[pid])
        if not orig:
            continue
        per_group = defaultdict(list)
        for did, _, _, pid, _ in s_out.execute(SAMPLES_BY_TYPE, (dt,)):
            sid = out_prov_sid.get(pid)
            if sid is not None and did in orig:
                per_group[target_sids[sid].kind].append(orig[did])
        for k, items in per_group.items():
            overlaps_found[k] += cross_device_overlaps(items)
    for g in groups.values():
        check(f"{g.label} 目標來源內沒有跨裝置的時間重疊", overlaps_found[g.kind] == 0,
              overlaps_found[g.kind])

    for name, conn, src_conn in (("healthdb_secure", s_out, s_src), ("healthdb", m_out, ro(src / MAIN_DB))):
        res = conn.execute("PRAGMA quick_check").fetchall()
        check(f"{name} quick_check", res == [("ok",)], res[:3] if res != [("ok",)] else "")
        a = len(src_conn.execute("PRAGMA main.foreign_key_check").fetchall())
        b = len(conn.execute("PRAGMA main.foreign_key_check").fetchall())
        check(f"{name} foreign_key_check 沒有新增問題", b <= a, f"{a} → {b}")

    log("\n合併後留下的裝置來源：")
    for lid, bundle, name, ptype, n in m_out.execute(
        "SELECT l.ROWID, l.bundle_id, so.name, so.product_type, count(*) FROM logical_sources l "
        "JOIN sources so ON so.logical_source_id = l.ROWID WHERE l.bundle_id LIKE ? "
        "GROUP BY l.ROWID ORDER BY l.ROWID", (DEVICE_PREFIX + "%",)):
        log(f"  lid {lid:>3}  {name:<30} {ptype:<11} {bundle}")
    for p in out.iterdir():
        if p.name.endswith(("-wal", "-shm")):
            check(f"沒有殘留 {p.name}", p.stat().st_size == 0, p.stat().st_size)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="含 healthdb.sqlite 與 healthdb_secure.sqlite 的資料夾")
    ap.add_argument("--out", type=Path, required=True, help="輸出資料夾（必須不存在）")
    ap.add_argument("--apply", action="store_true", help="實際輸出合併後的複本；未指定時只顯示計畫")
    ap.add_argument("--keep", action="append", metavar="DEVICE",
                    help="保留為獨立來源的裝置（lid 或 bundle ID），可重複指定")
    for opt, label in (("iphone", "iPhone"), ("watch", "Apple Watch")):
        ap.add_argument(f"--{opt}-target", metavar="DEVICE", help=f"目前的 {label}（lid 或 bundle ID）；預設自動判斷")
        ap.add_argument(f"--{opt}-legacy", metavar="DEVICE",
                        help="存放重疊資料的舊裝置來源（lid 或 bundle ID）；預設為紀錄最多的舊裝置")
        ap.add_argument(f"--{opt}-legacy-name", default=f"舊 {label}", metavar="NAME", help="存放重疊資料的來源改用的名稱")
    args = ap.parse_args()

    src = args.src.resolve()
    missing = [n for n in (MAIN_DB, SECURE_DB) if not (src / n).is_file()]
    if missing:
        sys.exit(f"在 {src} 找不到 {'、'.join(missing)}。\n"
                 f"--src 要指向放著從備份匯出的 {MAIN_DB} 和 {SECURE_DB} 的資料夾。")
    out = args.out.resolve()
    if args.apply:
        log(f"建立快照並複製資料庫到 {out} ……")
        base = make_snapshot(src, out)
    else:
        for name in (MAIN_DB, SECURE_DB):
            wal = src / (name + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                log(f"[警告] 預覽不會讀取 {wal.name} 內尚未寫回的變更；加上 --apply 時會先併入")
        base = src
    main_ro, sec_ro = ro(base / MAIN_DB), ro(base / SECURE_DB)
    for key in ("iphone_target", "watch_target", "iphone_legacy", "watch_legacy"):
        setattr(args, key, to_bundle(main_ro, getattr(args, key)))
    args.keep = [to_bundle(main_ro, v) for v in args.keep or []]
    devices, per_sid = load_devices(main_ro, sec_ro)
    groups = build_plan(devices, per_sid, args, detect_targets(main_ro))
    if not groups:
        main_ro.close()
        sec_ro.close()
        if args.apply:
            shutil.rmtree(out)
        log("沒有需要合併的裝置，未產生輸出。")
        return 0

    moving_bundles = {devices[l].bundle for g in groups.values() for l in g.old_lids}
    for bundle, sp in main_ro.execute("SELECT source_bundle_id, sync_provenance FROM nano_pairing"):
        if bundle in moving_bundles:
            log(f"[警告] {bundle} 仍在 nano_pairing（手錶配對紀錄）中，請確認已在 Watch App 解除配對")

    log("計算重疊時段……")
    legacy, moved_by_lid, stacked = assign(sec_ro, devices, groups)
    print_plan(devices, per_sid, groups, legacy, moved_by_lid, stacked)
    if not args.apply:
        log("\n這是預覽。確認無誤後加上 --apply 產生合併後的複本。")
        return 0

    main_ro.close()
    sec_ro.close()
    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(base / name, out / name)
    log("\n套用變更……")
    report = apply_changes(out, groups, legacy, devices)
    for k, v in report.items():
        log(f"  {k}: {v:,}")
    log("\n驗證……")
    ok = validate(base, out, groups, devices, legacy)
    if ok:
        shutil.rmtree(base)
        log("\n全部驗證通過。")
    else:
        log(f"\n驗證有失敗項目，請不要使用這份輸出（比對用的快照保留在 {base}）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
