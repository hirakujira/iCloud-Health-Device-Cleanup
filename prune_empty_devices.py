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
- With --drop-empty-provenance, also delete provenance entries that have no health
  records when their hardware record has no data, so that record is removed too.
- With --relink OLD=NEW, point data from hardware record OLD to the existing record
  NEW. OLD can be a missing record that data still references, or an existing
  record to merge into NEW; an existing OLD is then removed, discarding its model
  and firmware details.
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from merge_health_sources import (MAIN_DB, PROV_COLS, SECURE_DB, UNIQUE_COLS, apple_date, check_reclaimed,
                                  detect_targets, log, make_snapshot, pad, provenance_stats, reclaim_space, ro,
                                  ro_uri, to_bundle)

# healthd uses this hidden entry for "no device"; leave it untouched.
NONE_DEVICE = "__NONE__"
# (source_devices.name, source_devices.model, device kind, target source argument)
APPLE_KINDS = (("iPhone", "iPhone", "P", "iphone_target"), ("Apple Watch", "Watch", "W", "watch_target"))
DEVICE_COL = UNIQUE_COLS.index("device_id")


def used_device_ids(main, sec, mapping=None, ignore=()):
    """ignore: provenance IDs to skip, such as empty provenance scheduled for deletion."""
    mapping = mapping or {}
    used = {mapping.get(did, did) for pid, did in sec.execute("SELECT ROWID, device_id FROM data_provenances")
            if pid not in ignore}
    used |= {r[0] for r in main.execute(
        "SELECT DISTINCT device_id FROM workout_builders WHERE device_id IS NOT NULL")}
    return used


def find_orphans(main, sec, mapping=None, ignore=()):
    used = used_device_ids(main, sec, mapping, ignore)
    rows = main.execute("SELECT ROWID, name, hardware FROM source_devices WHERE name != ? "
                        "ORDER BY ROWID", (NONE_DEVICE,)).fetchall()
    return [r for r in rows if r[0] not in used]


def find_empty_provenances(main, sec, mapping=None):
    """Return {provenance ID: device_id} for provenance of hardware records with no health records.

    healthd can register provenance for an accessory (such as AirPods heart rate) without
    saving any samples; that provenance alone keeps the hardware record on the Devices page.
    """
    mapping = mapping or {}
    has_data = {r[0] for r in sec.execute("SELECT DISTINCT provenance FROM objects")}
    eligible = {r[0] for r in main.execute("SELECT ROWID FROM source_devices WHERE name != ?", (NONE_DEVICE,))}
    eligible -= {r[0] for r in main.execute(
        "SELECT DISTINCT device_id FROM workout_builders WHERE device_id IS NOT NULL")}
    # Collapse targets keep their provenance; it may be merged or updated in place.
    eligible -= set(mapping.values())
    by_device = defaultdict(list)
    for pid, did in sec.execute("SELECT ROWID, device_id FROM data_provenances"):
        by_device[mapping.get(did, did)].append(pid)
    return {pid: did for did, pids in by_device.items()
            if did in eligible and has_data.isdisjoint(pids) for pid in pids}


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


def plan_relink(main, sec, pairs, collapse_map=None):
    """Parse OLD=NEW pairs into {device_id: existing device_id}.

    OLD is either a missing record that data still references, or an existing record to merge into NEW.
    """
    collapse_map = collapse_map or {}
    names = dict(main.execute("SELECT ROWID, name FROM source_devices"))
    referenced = {r[0] for r in sec.execute("SELECT DISTINCT device_id FROM data_provenances")}
    relink = {}
    for pair in pairs:
        try:
            old, new = (int(v) for v in pair.split("="))
        except ValueError:
            sys.exit(f"Invalid --relink {pair}; expected OLD=NEW, such as 165=184")
        if old == new:
            sys.exit(f"--relink {pair}: OLD and NEW are the same record")
        if old not in referenced:
            sys.exit(f"--relink {pair}: no data references source_devices {old}")
        if new not in names:
            sys.exit(f"--relink {pair}: source_devices {new} does not exist")
        if NONE_DEVICE in (names.get(old), names[new]):
            sys.exit(f"--relink {pair}: {NONE_DEVICE} records cannot be relinked")
        if old in collapse_map:
            sys.exit(f"--relink {pair}: source_devices {old} is already changed by --collapse")
        if old in relink:
            sys.exit(f"--relink {pair}: {old} is specified more than once")
        relink[old] = new
    chained = sorted(set(relink) & set(relink.values()))
    if chained:
        sys.exit(f"--relink: {', '.join(map(str, chained))} used as both OLD and NEW; "
                 "point each OLD directly to the final record")
    return relink


def print_relink(main, sec, relink):
    stats = provenance_stats(sec)
    agg = {old: [0, None, None] for old in relink}
    for pid, did in sec.execute("SELECT ROWID, device_id FROM data_provenances"):
        if did in agg and pid in stats:
            n, first, last = stats[pid]
            cur = agg[did]
            cur[0] += n
            if first is not None and (cur[1] is None or first < cur[1]):
                cur[1] = first
            if last is not None and (cur[2] is None or last > cur[2]):
                cur[2] = last
    info = {r[0]: r[1:] for r in main.execute(
        "SELECT ROWID, name, coalesce(nullif(hardware, ''), model), firmware, creation_date FROM source_devices")}

    def describe(did):
        if did not in info:
            return "(missing)"
        name, model, fw, created = info[did]
        return f"{name} ({model or '-'}, {fw or '-'}, {apple_date(created)})"

    log("Relink data to other hardware records (check that each pair is the same device and the dates match):")
    log(f"{'From':>5}  {pad('Name (model, firmware, created)', 52)} {'Records':>10}  {'First data':<10} "
        f"{'Last data':<10}  ->  {'To':>5}  Name (model, firmware, created)")
    for old, new in sorted(relink.items()):
        n, first, last = agg[old]
        log(f"{old:>5}  {pad(describe(old), 52)} {n:>10,}  {apple_date(first):<10} {apple_date(last):<10}"
            f"  ->  {new:>5}  {describe(new)}")
    if any(old in info for old in relink):
        log("Existing records relinked here are removed afterwards; their model and firmware details are lost.")


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


def print_plan(main, orphans, empty=None):
    if empty:
        log(f"data_provenances: remove {len(empty):,} entries with no health records "
            f"(source_devices {', '.join(map(str, sorted(set(empty.values()))))})")
    total = main.execute("SELECT count(*) FROM source_devices").fetchone()[0]
    log(f"source_devices: {total:,} records; remove {len(orphans):,} unreferenced records:")
    for (name, hw), n in sorted(Counter((r[1], r[2] or "-") for r in orphans).items()):
        log(f"  {name:<28} {hw:<12} {n:>4} records")


def apply_changes(out: Path, prune_ids, updates, repoint, empty=()):
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
        if empty:
            sec.execute("CREATE TEMP TABLE empty_prov(id INTEGER PRIMARY KEY)")
            sec.executemany("INSERT INTO empty_prov VALUES (?)", ((i,) for i in empty))
            n = sec.execute("SELECT count(*) FROM objects WHERE provenance IN (SELECT id FROM empty_prov)").fetchone()[0]
            if n:
                raise RuntimeError(f"{n} records reference provenance scheduled for deletion")
            report["empty_provenances_deleted"] = sec.execute(
                "DELETE FROM data_provenances WHERE ROWID IN (SELECT id FROM empty_prov)").rowcount

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


def validate(src: Path, out: Path, space, n_merged, targets, n_empty=0, relinked=()):
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
    check("data_provenances record count matches expectation", b == a - n_merged - n_empty,
          f"{a:,} -> {b:,} (merged {n_merged:,}" + (f", empty removed {n_empty:,})" if n_empty else ")"))
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
    # Backups can already reference missing hardware records; only flag ones this run removed.
    before = used_device_ids(m_src, s_src) - {r[0] for r in m_src.execute("SELECT ROWID FROM source_devices")}
    new = missing - before
    check("No new references to missing device records", not new,
          sorted(new)[:10] if new else (f"{len(before)} -> {len(missing)}" if before else ""))
    left = find_orphans(m_out, s_out)
    check("No unreferenced device records remain", not left, len(left))
    if relinked:
        n = s_out.execute(f"SELECT count(*) FROM data_provenances WHERE device_id IN ({','.join('?' * len(relinked))})",
                          list(relinked)).fetchone()[0]
        check("Relinked hardware IDs are no longer referenced", n == 0, n)
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
    check_reclaimed(check, out, space)
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
    ap.add_argument("--drop-empty-provenance", action="store_true",
                    help="also delete provenance with no health records when its hardware record has no data, "
                         "so that hardware record is removed too")
    ap.add_argument("--relink", action="append", metavar="OLD=NEW",
                    help="point data from hardware record OLD (missing, or existing to merge) to existing record NEW "
                         "(IDs from list_health_sources.py --hardware); may be repeated; an existing OLD is removed")
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
    collapse_map, targets, updates, repoint, expected = {}, [], [], {}, 0
    if args.collapse:
        collapse_map, targets = plan_collapse(main_ro, args)
    relink = plan_relink(main_ro, sec_ro, args.relink or [], collapse_map)
    # A relink target may itself be collapsed into the current device.
    mapping = {**collapse_map, **{old: collapse_map.get(new, new) for old, new in relink.items()}}
    if args.collapse or relink:
        updates, repoint, conflicts = plan_provenances(sec_ro, mapping)
        prov_obj = dict(sec_ro.execute("SELECT provenance, count(*) FROM objects GROUP BY 1"))
        if args.collapse:
            print_collapse(main_ro, sec_ro, collapse_map, targets, prov_obj)
        if relink:
            print_relink(main_ro, sec_ro, relink)
        expected = sum(prov_obj.get(p, 0) for p in repoint)
        log(f"provenance: update {len(updates):,} in place, merge {len(repoint):,} ({expected:,} records repointed)\n")
        if conflicts:
            if args.apply:
                shutil.rmtree(out)
            sys.exit(f"Stopped: {conflicts} provenance entries have conflicting derived_flags after merging.")
    empty = find_empty_provenances(main_ro, sec_ro, mapping) if args.drop_empty_provenance else {}
    orphans = find_orphans(main_ro, sec_ro, mapping, empty)
    print_plan(main_ro, orphans, empty)
    main_ro.close()
    sec_ro.close()
    if not args.apply:
        log("\nThis is a preview. Check the plan, then add --apply to create cleaned copies.")
        return 0
    if not orphans and not updates and not repoint and not empty:
        shutil.rmtree(out)
        log("\nNo device records to clean up; no output created.")
        return 0

    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(base / name, out / name)
    log("\nApplying changes ...")
    report = apply_changes(out, [r[0] for r in orphans], updates, repoint, list(empty))
    for k, v in report.items():
        log(f"  {k}: {v:,}")
    log("Reclaiming free space ...")
    space = reclaim_space(out)
    log("\nValidating ...")
    ok = validate(base, out, space, len(repoint), targets, len(empty), list(relink))
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
