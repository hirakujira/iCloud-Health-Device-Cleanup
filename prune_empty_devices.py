#!/usr/bin/env python3
"""Clean up hardware records (source_devices) in the Health app's Devices list.

Original databases are always opened read-only. Use --apply to copy them to --out
and modify only the copies.
- By default, remove only records not referenced by data_provenances or
  workout_builders, without deleting any health data.
- With --collapse, first change all iPhone / Apple Watch hardware references to
  the current devices, then remove unreferenced records. This discards the model
  and OS version originally recorded for old data.
  Current devices are detected as in merge_health_sources.py; override with
  --iphone-target / --watch-target.
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from merge_health_sources import (MAIN_DB, PROV_COLS, SECURE_DB, UNIQUE_COLS, detect_targets, log,
                                  make_snapshot, ro, ro_uri, to_bundle)

# healthd uses this hidden entry for "no device"; leave it untouched.
NONE_DEVICE = "__NONE__"
# (source_devices.name, source_devices.model, device kind, target source argument)
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
    """Return {old device_id: target device_id} and target records for each device type."""
    mapping, targets = {}, []
    detected = detect_targets(main)
    for name, model, kind, key in APPLE_KINDS:
        bundle = to_bundle(main, getattr(args, key)) or detected[kind]
        if not bundle:
            if main.execute("SELECT 1 FROM source_devices WHERE name = ? AND model = ?", (name, model)).fetchone():
                sys.exit(f"Cannot detect the current {name}; specify --{key.replace('_', '-')} (lid or bundle ID)")
            continue
        row = main.execute(
            "SELECT so.product_type FROM sources so JOIN logical_sources l ON l.ROWID = so.logical_source_id "
            "WHERE l.bundle_id = ? AND so.deleted = 0 ORDER BY so.ROWID DESC LIMIT 1", (bundle,)).fetchone()
        if not row:
            sys.exit(f"Target source not found: {bundle}")
        # The latest entry usually has the current OS version and is reused by healthd.
        target = main.execute(
            "SELECT ROWID, hardware, software FROM source_devices WHERE name = ? AND model = ? "
            "AND hardware = ? ORDER BY creation_date DESC, ROWID DESC LIMIT 1",
            (name, model, row[0])).fetchone()
        if not target:
            sys.exit(f"No hardware record found for {name} ({row[0]})")
        for (did,) in main.execute("SELECT ROWID FROM source_devices WHERE name = ? AND model = ? "
                                   "AND ROWID != ?", (name, model, target[0])):
            mapping[did] = target[0]
        targets.append((name, *target))
    return mapping, targets


def plan_provenances(sec, mapping):
    """Merge provenance with matching rewritten unique keys and repoint its records."""
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
        # Prefer entries already using the target device to avoid unique-key conflicts.
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
        log(f"{name}: change {len(olds):,} hardware records to {thw} ({sw}, source_devices {did})")
        log("  Original models: " + ", ".join(f"{m} x{n}" for m, n in sorted(models.items())))
    n = sum(prov_obj.get(pid, 0) for pid, did in sec.execute("SELECT ROWID, device_id FROM data_provenances")
            if did in mapping)
    log(f"Hardware references for {n:,} health records will be changed to the current devices")


def print_plan(main, orphans):
    total = main.execute("SELECT count(*) FROM source_devices").fetchone()[0]
    log(f"source_devices: {total:,} records; remove {len(orphans):,} unreferenced records:")
    for (name, hw), n in sorted(Counter((r[1], r[2] or "-") for r in orphans).items()):
        log(f"  {name:<28} {hw:<12} {n:>4} records")


def apply_changes(out: Path, prune_ids, updates, repoint):
    sec = sqlite3.connect(out / SECURE_DB, isolation_level=None)
    # Disable foreign keys so deleting provenance does not cascade to health records.
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
        # No references should appear after the snapshot, but recheck within the transaction.
        n = sec.execute(
            "SELECT (SELECT count(*) FROM data_provenances WHERE device_id IN (SELECT id FROM prune))"
            " + (SELECT count(*) FROM m.workout_builders WHERE device_id IN (SELECT id FROM prune))"
        ).fetchone()[0]
        if n:
            raise RuntimeError(f"{n} records still reference device records scheduled for deletion")
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
        log(f"[{'OK' if cond else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")

    s_src, s_out = ro(src / SECURE_DB), ro(out / SECURE_DB)
    m_src, m_out = ro(src / MAIN_DB), ro(out / MAIN_DB)
    s_out.execute("ATTACH DATABASE ? AS m", (ro_uri(out / MAIN_DB),))
    a = s_src.execute("SELECT count(*) FROM objects").fetchone()[0]
    b = s_out.execute("SELECT count(*) FROM objects").fetchone()[0]
    check("objects record count unchanged", a == b, f"{a:,} -> {b:,}")
    a = s_src.execute("SELECT count(*) FROM data_provenances").fetchone()[0]
    b = s_out.execute("SELECT count(*) FROM data_provenances").fetchone()[0]
    check("data_provenances record count matches expectation", b == a - n_merged, f"{a:,} -> {b:,} (merged {n_merged:,})")
    q = ("SELECT p.source_id, p.derived_flags, count(*) FROM objects o "
         "JOIN data_provenances p ON p.ROWID = o.provenance GROUP BY 1, 2")
    check("Record count unchanged for each source", s_src.execute(q).fetchall() == s_out.execute(q).fetchall())
    check("No records reference missing provenance", s_out.execute(
        "SELECT count(*) FROM objects o LEFT JOIN data_provenances p ON p.ROWID = o.provenance "
        "WHERE p.ROWID IS NULL").fetchone()[0] == 0)
    for t in ("sources", "workout_builders"):
        a = m_src.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        b = m_out.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        check(f"{t} record count unchanged", a == b, f"{a:,} -> {b:,}")
    devices = {r[0] for r in m_out.execute("SELECT ROWID FROM source_devices")}
    missing = used_device_ids(m_out, s_out) - devices
    check("All referenced device records still exist", not missing, sorted(missing)[:10] if missing else "")
    left = find_orphans(m_out, s_out)
    check("No unreferenced device records remain", not left, len(left))
    if targets:
        names = tuple(k[0] for k in APPLE_KINDS)
        remaining = {r[0] for r in s_out.execute(
            "SELECT DISTINCT p.device_id FROM data_provenances p JOIN m.source_devices d "
            f"ON d.ROWID = p.device_id WHERE d.name IN ({','.join('?' * len(names))})", names)}
        want = {t[1] for t in targets}
        check("iPhone / Apple Watch data references only current device hardware", remaining <= want, sorted(remaining))
    for name, conn in (("healthdb_secure", s_out), ("healthdb", m_out)):
        res = conn.execute("PRAGMA quick_check").fetchall()
        check(f"{name} quick_check", res == [("ok",)], res[:3] if res != [("ok",)] else "")
    for name, a_conn, b_conn in (("healthdb_secure", s_src, ro(out / SECURE_DB)), ("healthdb", m_src, m_out)):
        a = len(a_conn.execute("PRAGMA foreign_key_check").fetchall())
        b = len(b_conn.execute("PRAGMA foreign_key_check").fetchall())
        check(f"{name} foreign_key_check has no new violations", b <= a, f"{a} -> {b}")
    for p in out.iterdir():
        if p.name.endswith(("-wal", "-shm")):
            check(f"No remaining data in {p.name}", p.stat().st_size == 0, p.stat().st_size)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="directory containing healthdb.sqlite and healthdb_secure.sqlite")
    ap.add_argument("--out", type=Path, required=True, help="output directory (must not already exist)")
    ap.add_argument("--apply", action="store_true", help="write cleaned copies; otherwise only preview the plan")
    ap.add_argument("--collapse", action="store_true",
                    help="change all iPhone / Apple Watch hardware references to current devices (discards old models and OS versions)")
    for key, label in (("iphone_target", "iPhone"), ("watch_target", "Apple Watch")):
        ap.add_argument("--" + key.replace("_", "-"), metavar="DEVICE",
                        help=f"current {label} (lid or bundle ID); detected automatically by default")
    args = ap.parse_args()

    src = args.src.resolve()
    missing = [n for n in (MAIN_DB, SECURE_DB) if not (src / n).is_file()]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} in {src}.\n"
                 f"--src must point to the directory containing {MAIN_DB} and {SECURE_DB} extracted from the backup.")
    out = args.out.resolve()
    if args.apply:
        log(f"Creating a snapshot and copying databases to {out} ...")
        base = make_snapshot(src, out)
    else:
        for name in (MAIN_DB, SECURE_DB):
            wal = src / (name + "-wal")
            if wal.exists() and wal.stat().st_size > 0:
                log(f"[WARNING] Preview ignores pending changes in {wal.name}; --apply checkpoints them first")
        base = src

    main_ro, sec_ro = ro(base / MAIN_DB), ro(base / SECURE_DB)
    mapping, targets, updates, repoint, expected = {}, [], [], {}, 0
    if args.collapse:
        mapping, targets = plan_collapse(main_ro, args)
        updates, repoint, conflicts = plan_provenances(sec_ro, mapping)
        prov_obj = dict(sec_ro.execute("SELECT provenance, count(*) FROM objects GROUP BY 1"))
        print_collapse(main_ro, sec_ro, mapping, targets, prov_obj)
        expected = sum(prov_obj.get(p, 0) for p in repoint)
        log(f"provenance: update {len(updates):,} in place, merge {len(repoint):,} ({expected:,} records repointed)\n")
        if conflicts:
            if args.apply:
                shutil.rmtree(out)
            sys.exit(f"Stopped: {conflicts} provenance entries have conflicting derived_flags after merging.")
    orphans = find_orphans(main_ro, sec_ro, mapping)
    print_plan(main_ro, orphans)
    main_ro.close()
    sec_ro.close()
    if not args.apply:
        log("\nThis is a preview. Check the plan, then add --apply to create cleaned copies.")
        return 0
    if not orphans and not updates and not repoint:
        shutil.rmtree(out)
        log("\nNo device records to clean up; no output created.")
        return 0

    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(base / name, out / name)
    log("\nApplying changes ...")
    report = apply_changes(out, [r[0] for r in orphans], updates, repoint)
    for k, v in report.items():
        log(f"  {k}: {v:,}")
    log("\nValidating ...")
    ok = validate(base, out, len(repoint), targets)
    if repoint:
        got = report.get("objects_repointed", 0)
        ok &= got == expected
        log(f"[{'OK' if got == expected else 'FAIL'}] Repointed record count matches expectation: {got:,} (expected {expected:,})")
    if ok:
        shutil.rmtree(base)
        log("\nAll checks passed.")
    else:
        log(f"\nValidation failed. Do not use this output (comparison snapshot retained at {base}).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
