#!/usr/bin/env python3
"""清理「健康」App「裝置」列表中的硬體紀錄（source_devices）。

原始資料庫一律以唯讀方式開啟；加上 --apply 時才會複製到 --out 並修改複本。
- 預設只刪除沒有被 data_provenances 或 workout_builders 引用的紀錄，不刪除任何健康資料。
- 加上 --collapse 時，先把所有 iPhone / Apple Watch 資料的硬體紀錄改成目前裝置，
  再刪除因此不再被引用的紀錄。舊資料原本的型號與系統版本會因此遺失。
  目前裝置的判斷方式和 merge_health_sources.py 相同，也可以用 --iphone-target / --watch-target 指定。
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from merge_health_sources import (MAIN_DB, PROV_COLS, SECURE_DB, UNIQUE_COLS, detect_targets, log,
                                  make_snapshot, ro, ro_uri, to_bundle)

# healthd 以這個名稱代表「沒有裝置」，不會出現在列表中，保留不動
NONE_DEVICE = "__NONE__"
# (source_devices.name, source_devices.model, 裝置種類, 目標來源參數)
APPLE_KINDS = (("iPhone", "iPhone", "P", "iphone_target"), ("Apple Watch", "Watch", "W", "watch_target"))
DEVICE_COL = UNIQUE_COLS.index("device_id")


def used_device_ids(main, sec, mapping=None):
    mapping = mapping or {}
    used = {mapping.get(r[0], r[0]) for r in sec.execute("SELECT DISTINCT device_id FROM data_provenances")}
    used |= {r[0] for r in main.execute(
        "SELECT DISTINCT device_id FROM workout_builders WHERE device_id IS NOT NULL")}
    return used


def find_orphans(main, sec, mapping=None):
    used = used_device_ids(main, sec, mapping)
    rows = main.execute("SELECT ROWID, name, hardware FROM source_devices WHERE name != ? "
                        "ORDER BY ROWID", (NONE_DEVICE,)).fetchall()
    return [r for r in rows if r[0] not in used]


def plan_collapse(main, args):
    """回傳 {舊 device_id: 目標 device_id} 與每種裝置的目標紀錄。"""
    mapping, targets = {}, []
    detected = detect_targets(main)
    for name, model, kind, key in APPLE_KINDS:
        bundle = to_bundle(main, getattr(args, key)) or detected[kind]
        if not bundle:
            if main.execute("SELECT 1 FROM source_devices WHERE name = ? AND model = ?", (name, model)).fetchone():
                sys.exit(f"無法判斷目前的 {name}，請用 --{key.replace('_', '-')} 指定（lid 或 bundle ID）")
            continue
        row = main.execute(
            "SELECT so.product_type FROM sources so JOIN logical_sources l ON l.ROWID = so.logical_source_id "
            "WHERE l.bundle_id = ? AND so.deleted = 0 ORDER BY so.ROWID DESC LIMIT 1", (bundle,)).fetchone()
        if not row:
            sys.exit(f"找不到目標來源 {bundle}")
        # 取最新的一筆，通常就是目前系統版本，healthd 之後寫入新資料時會沿用
        target = main.execute(
            "SELECT ROWID, hardware, software FROM source_devices WHERE name = ? AND model = ? "
            "AND hardware = ? ORDER BY creation_date DESC, ROWID DESC LIMIT 1",
            (name, model, row[0])).fetchone()
        if not target:
            sys.exit(f"找不到 {name}（{row[0]}）的硬體紀錄")
        for (did,) in main.execute("SELECT ROWID FROM source_devices WHERE name = ? AND model = ? "
                                   "AND ROWID != ?", (name, model, target[0])):
            mapping[did] = target[0]
        targets.append((name, *target))
    return mapping, targets


def plan_provenances(sec, mapping):
    """改寫後唯一鍵相同的 provenance 只留一筆，其餘的紀錄改指向留下的那筆。"""
    groups = defaultdict(list)
    for pid, *vals in sec.execute(f"SELECT ROWID, {', '.join(PROV_COLS)} FROM data_provenances"):
        rec = dict(zip(PROV_COLS, vals))
        moved = rec["device_id"] in mapping
        rec["device_id"] = mapping.get(rec["device_id"], rec["device_id"])
        groups[tuple(rec[c] for c in UNIQUE_COLS)].append((moved, pid, rec["derived_flags"]))
    updates, repoint, conflicts = [], {}, 0
    for key, items in groups.items():
        if not any(m for m, _, _ in items):
            continue
        # 已經指向目標裝置的那筆優先保留，原地更新其他筆時才不會違反唯一限制
        items.sort()
        keep_moved, keep_pid, keep_flags = items[0]
        if keep_moved:
            updates.append((key[DEVICE_COL], keep_pid))
        for _, pid, flags in items[1:]:
            repoint[pid] = keep_pid
            conflicts += flags != keep_flags
    return updates, repoint, conflicts


def print_collapse(main, sec, mapping, targets, prov_obj):
    hw = dict(main.execute("SELECT ROWID, hardware FROM source_devices"))
    for name, did, thw, sw in targets:
        olds = [d for d, t in mapping.items() if t == did]
        models = Counter(hw[d] for d in olds)
        log(f"{name}：{len(olds):,} 筆硬體紀錄改成 {thw}（{sw}，source_devices {did}）")
        log("  原本的型號：" + "、".join(f"{m} ×{n}" for m, n in sorted(models.items())))
    n = sum(prov_obj.get(pid, 0) for pid, did in sec.execute("SELECT ROWID, device_id FROM data_provenances")
            if did in mapping)
    log(f"共 {n:,} 筆健康資料的硬體資訊會改成目前裝置")


def print_plan(main, orphans):
    total = main.execute("SELECT count(*) FROM source_devices").fetchone()[0]
    log(f"source_devices 共 {total:,} 筆，將刪除 {len(orphans):,} 筆沒有資料的紀錄：")
    for (name, hw), n in sorted(Counter((r[1], r[2] or "-") for r in orphans).items()):
        log(f"  {name:<28} {hw:<12} {n:>4} 筆")


def apply_changes(out: Path, prune_ids, updates, repoint):
    sec = sqlite3.connect(out / SECURE_DB, isolation_level=None)
    # objects → data_provenances 是 ON DELETE CASCADE，必須關閉外鍵避免刪 provenance 時連帶刪除紀錄
    sec.execute("PRAGMA foreign_keys=OFF")
    sec.execute("ATTACH DATABASE ? AS m", (str(out / MAIN_DB),))
    report = {}
    sec.execute("BEGIN")
    try:
        if repoint:
            sec.execute("CREATE TEMP TABLE pmap(old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
            sec.executemany("INSERT INTO pmap VALUES (?, ?)", repoint.items())
            report["objects_repointed"] = sec.execute(
                "UPDATE objects SET provenance = (SELECT new FROM pmap WHERE old = objects.provenance) "
                "WHERE provenance IN (SELECT old FROM pmap)").rowcount
            report["provenances_merged"] = sec.execute(
                "DELETE FROM data_provenances WHERE ROWID IN (SELECT old FROM pmap)").rowcount
        if updates:
            sec.executemany("UPDATE data_provenances SET device_id = ? WHERE ROWID = ?", updates)
            report["provenances_updated"] = len(updates)

        sec.execute("CREATE TEMP TABLE prune(id INTEGER PRIMARY KEY)")
        sec.executemany("INSERT INTO prune VALUES (?)", ((i,) for i in prune_ids))
        # 快照之後才寫入的引用不應存在，但仍在同一個交易內再確認一次
        n = sec.execute(
            "SELECT (SELECT count(*) FROM data_provenances WHERE device_id IN (SELECT id FROM prune))"
            " + (SELECT count(*) FROM m.workout_builders WHERE device_id IN (SELECT id FROM prune))"
        ).fetchone()[0]
        if n:
            raise RuntimeError(f"有 {n} 筆資料仍引用要刪除的裝置紀錄")
        report["deleted_source_devices"] = sec.execute(
            "DELETE FROM m.source_devices WHERE ROWID IN (SELECT id FROM prune)").rowcount
        sec.execute("COMMIT")
    except Exception:
        sec.execute("ROLLBACK")
        raise
    sec.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
    sec.execute("PRAGMA m.wal_checkpoint(TRUNCATE)")
    sec.close()
    return report


def validate(src: Path, out: Path, n_merged, targets):
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        log(f"[{'OK' if cond else 'FAIL'}] {label}{('：' + str(detail)) if detail else ''}")

    s_src, s_out = ro(src / SECURE_DB), ro(out / SECURE_DB)
    m_src, m_out = ro(src / MAIN_DB), ro(out / MAIN_DB)
    s_out.execute("ATTACH DATABASE ? AS m", (ro_uri(out / MAIN_DB),))
    a = s_src.execute("SELECT count(*) FROM objects").fetchone()[0]
    b = s_out.execute("SELECT count(*) FROM objects").fetchone()[0]
    check("objects 筆數不變", a == b, f"{a:,} → {b:,}")
    a = s_src.execute("SELECT count(*) FROM data_provenances").fetchone()[0]
    b = s_out.execute("SELECT count(*) FROM data_provenances").fetchone()[0]
    check("data_provenances 筆數符合預期", b == a - n_merged, f"{a:,} → {b:,}（合併 {n_merged:,}）")
    q = ("SELECT p.source_id, p.derived_flags, count(*) FROM objects o "
         "JOIN data_provenances p ON p.ROWID = o.provenance GROUP BY 1, 2")
    check("每個來源的紀錄筆數不變", s_src.execute(q).fetchall() == s_out.execute(q).fetchall())
    check("沒有紀錄指向不存在的 provenance", s_out.execute(
        "SELECT count(*) FROM objects o LEFT JOIN data_provenances p ON p.ROWID = o.provenance "
        "WHERE p.ROWID IS NULL").fetchone()[0] == 0)
    for t in ("sources", "workout_builders"):
        a = m_src.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        b = m_out.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        check(f"{t} 筆數不變", a == b, f"{a:,} → {b:,}")
    devices = {r[0] for r in m_out.execute("SELECT ROWID FROM source_devices")}
    missing = used_device_ids(m_out, s_out) - devices
    check("所有資料引用的裝置紀錄都還在", not missing, sorted(missing)[:10] if missing else "")
    left = find_orphans(m_out, s_out)
    check("沒有剩下無資料的裝置紀錄", not left, len(left))
    if targets:
        names = tuple(k[0] for k in APPLE_KINDS)
        remaining = {r[0] for r in s_out.execute(
            "SELECT DISTINCT p.device_id FROM data_provenances p JOIN m.source_devices d "
            f"ON d.ROWID = p.device_id WHERE d.name IN ({','.join('?' * len(names))})", names)}
        want = {t[1] for t in targets}
        check("iPhone / Apple Watch 資料只剩目前裝置的硬體紀錄", remaining <= want, sorted(remaining))
    for name, conn in (("healthdb_secure", s_out), ("healthdb", m_out)):
        res = conn.execute("PRAGMA quick_check").fetchall()
        check(f"{name} quick_check", res == [("ok",)], res[:3] if res != [("ok",)] else "")
    for name, a_conn, b_conn in (("healthdb_secure", s_src, ro(out / SECURE_DB)), ("healthdb", m_src, m_out)):
        a = len(a_conn.execute("PRAGMA foreign_key_check").fetchall())
        b = len(b_conn.execute("PRAGMA foreign_key_check").fetchall())
        check(f"{name} foreign_key_check 沒有新增問題", b <= a, f"{a} → {b}")
    for p in out.iterdir():
        if p.name.endswith(("-wal", "-shm")):
            check(f"沒有殘留 {p.name}", p.stat().st_size == 0, p.stat().st_size)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="含 healthdb.sqlite 與 healthdb_secure.sqlite 的資料夾")
    ap.add_argument("--out", type=Path, required=True, help="輸出資料夾（必須不存在）")
    ap.add_argument("--apply", action="store_true", help="實際輸出清理後的複本；未指定時只顯示計畫")
    ap.add_argument("--collapse", action="store_true",
                    help="把所有 iPhone / Apple Watch 資料的硬體紀錄改成目前裝置（舊型號與系統版本會遺失）")
    for key, label in (("iphone_target", "iPhone"), ("watch_target", "Apple Watch")):
        ap.add_argument("--" + key.replace("_", "-"), metavar="DEVICE",
                        help=f"目前的 {label}（lid 或 bundle ID）；預設自動判斷")
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
    mapping, targets, updates, repoint, expected = {}, [], [], {}, 0
    if args.collapse:
        mapping, targets = plan_collapse(main_ro, args)
        updates, repoint, conflicts = plan_provenances(sec_ro, mapping)
        prov_obj = dict(sec_ro.execute("SELECT provenance, count(*) FROM objects GROUP BY 1"))
        print_collapse(main_ro, sec_ro, mapping, targets, prov_obj)
        expected = sum(prov_obj.get(p, 0) for p in repoint)
        log(f"provenance：原地更新 {len(updates):,} 筆，合併 {len(repoint):,} 筆（{expected:,} 筆紀錄改指向）\n")
        if conflicts:
            if args.apply:
                shutil.rmtree(out)
            sys.exit(f"有 {conflicts} 筆 provenance 合併後 derived_flags 不一致，已停止。")
    orphans = find_orphans(main_ro, sec_ro, mapping)
    print_plan(main_ro, orphans)
    main_ro.close()
    sec_ro.close()
    if not args.apply:
        log("\n這是預覽。確認無誤後加上 --apply 產生清理後的複本。")
        return 0
    if not orphans and not updates and not repoint:
        shutil.rmtree(out)
        log("\n沒有需要清理的裝置紀錄，未產生輸出。")
        return 0

    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(base / name, out / name)
    log("\n套用變更……")
    report = apply_changes(out, [r[0] for r in orphans], updates, repoint)
    for k, v in report.items():
        log(f"  {k}: {v:,}")
    log("\n驗證……")
    ok = validate(base, out, len(repoint), targets)
    if repoint:
        got = report.get("objects_repointed", 0)
        ok &= got == expected
        log(f"[{'OK' if got == expected else 'FAIL'}] 改指向的紀錄數符合預期：{got:,}（預期 {expected:,}）")
    if ok:
        shutil.rmtree(base)
        log("\n全部驗證通過。")
    else:
        log(f"\n驗證有失敗項目，請不要使用這份輸出（比對用的快照保留在 {base}）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
