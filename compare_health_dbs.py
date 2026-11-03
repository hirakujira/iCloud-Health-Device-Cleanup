#!/usr/bin/env python3
"""Compare original Health databases with a cleaned-up copy.

Databases are opened read-only. First checks that every health record is still
present and unchanged: the same records (data_id, UUID, type, creation date),
identical rows in every table holding record contents, and the same record count
and quantity total for each data type. Only the source and hardware record each
record belongs to may change; those moves are summarized. Deletion records
(objects.type = 2), which only tell synced devices to delete data, are counted
separately; removing them is reported but not treated as a failure. Then lists
everything else that changed, such as deleted or renamed sources, removed hardware
records, merged provenance, dropped priority entries, and cleared caches.
"""

import argparse
import math
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

from merge_health_sources import MAIN_DB, SECURE_DB, log, pad, ro_uri

# Columns that only say which source a record belongs to; merging sources changes them.
SOURCE_COLS = {"objects": {"provenance"}, "external_sync_ids": {"source_id"}}
RECORD_ID_COLS = {"data_id", "object_id", "owner_id"}
DELETION_RECORD = 2  # objects.type of markers that tell synced devices to delete a sample
# Tables without a record ID column whose rows record contents depend on.
RECORD_EXTRA = {"associations", "metadata_keys", "unit_strings"}


def q(name):
    return '"' + name.replace('"', '""') + '"'


def readable(path: Path, tmp: Path):
    """Return read-only URIs for both databases, applying pending WAL changes to a temporary copy."""
    uris = {}
    for name in (MAIN_DB, SECURE_DB):
        db, wal = path / name, path / (name + "-wal")
        if wal.exists() and wal.stat().st_size:
            log(f"Applying pending changes in {wal} to a temporary copy ...")
            tmp.mkdir(exist_ok=True)
            shutil.copy2(db, tmp / name)
            shutil.copy2(wal, tmp / wal.name)
            conn = sqlite3.connect(tmp / name)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            db = tmp / name
        uris[name] = ro_uri(db)
    return uris


def tables(conn, schema):
    """Return {table: (columns, key columns)}; the key is the rowid unless the table is WITHOUT ROWID."""
    res = {}
    for name, sql in conn.execute(f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table'"):
        sql = (sql or "").upper()
        if sql.startswith("CREATE VIRTUAL"):
            continue
        info = conn.execute(f"PRAGMA {schema}.table_info({q(name)})").fetchall()
        key = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]] if "WITHOUT ROWID" in sql else ["rowid"]
        res[name] = ([r[1] for r in info], key)
    return res


def record_tables(conn, schema, info):
    """Secure tables holding record contents: keyed by a record ID, or tied to such a table by a foreign key."""
    found = {t for t, (cols, _) in info.items() if RECORD_ID_COLS & set(cols)} | (RECORD_EXTRA & set(info))
    refs = {t: {r[2] for r in conn.execute(f"PRAGMA {schema}.foreign_key_list({q(t)})")} for t in info}
    while True:
        more = {t for t in info if t not in found and refs[t] & found}
        if not more:
            return found
        found |= more


def diff_table(conn, a, b, table, info_a, info_b):
    """Compare rows matched by key. Returns a dict with row counts and changed rows per column.

    removed_deletion counts removed rows that belong to removed deletion records (temp.gone_deletions).
    """
    t = q(table)
    if table not in info_a or table not in info_b:
        side = a if table in info_a else b
        n = conn.execute(f"SELECT count(*) FROM {side}.{t}").fetchone()[0]
        return {"before": n if table in info_a else 0, "after": n if table in info_b else 0,
                "removed": n if table in info_a else 0, "added": n if table in info_b else 0,
                "changed": 0, "cols": {}, "removed_deletion": 0, "note": "only before" if table in info_a else "only after"}
    (cols_a, key), (cols_b, _) = info_a[table], info_b[table]
    cols = [c for c in cols_a if c in cols_b and c not in key]
    on = " AND ".join(f"y.{q(k)} = x.{q(k)}" for k in key)
    hit = f"y.{q(key[0])} IS NOT NULL"
    diffs = [f"x.{q(c)} IS NOT y.{q(c)}" for c in cols]
    sel = ["count(*)", f"total(y.{q(key[0])} IS NULL)", f"total({hit} AND ({' OR '.join(diffs) or '0'}))"]
    ref = next((c for c in ("data_id", "object_id") if c in cols_a), None)
    sel.append(f"total(y.{q(key[0])} IS NULL AND x.{q(ref)} IN (SELECT data_id FROM temp.gone_deletions))"
               if ref else "0")
    sel += [f"total({hit} AND {d})" for d in diffs]
    before, removed, changed, removed_deletion, *per_col = conn.execute(
        f"SELECT {', '.join(sel)} FROM {a}.{t} x LEFT JOIN {b}.{t} y ON {on}").fetchone()
    after = conn.execute(f"SELECT count(*) FROM {b}.{t}").fetchone()[0]
    removed = int(removed)
    return {"before": before, "after": after, "removed": removed, "added": after - (before - removed),
            "changed": int(changed), "cols": {c: int(n) for c, n in zip(cols, per_col) if n},
            "removed_deletion": int(removed_deletion), "note": "columns differ" if set(cols_a) != set(cols_b) else ""}


def per_type(conn, s):
    return {dt: (n, total) for dt, n, total in conn.execute(
        f"SELECT s.data_type, count(*), total(qs.quantity) FROM {s}.samples s "
        f"JOIN {s}.objects o ON o.data_id = s.data_id AND o.type != {DELETION_RECORD} "
        f"LEFT JOIN {s}.quantity_samples qs ON qs.data_id = s.data_id GROUP BY 1")}


def owners(conn, m):
    """Return {source ROWID: (uuid, label)} and {hardware ROWID: (uuid, label)}."""
    sources = {}
    for sid, uuid, name, ptype, deleted, lid, bundle in conn.execute(
            f"SELECT so.ROWID, so.uuid, so.name, so.product_type, so.deleted, l.ROWID, l.bundle_id "
            f"FROM {m}.sources so LEFT JOIN {m}.logical_sources l ON l.ROWID = so.logical_source_id"):
        if deleted:
            # healthd clears the name and moves deleted sources to an empty bundle ID.
            label = f"deleted source {sid}" + (f" ({name})" if name else "")
        else:
            label = f"{name or bundle} ({ptype + ', ' if ptype else ''}lid {lid})"
        sources[sid] = (uuid, label)
    hardware = {did: (uuid, f"{name} ({hw or model or '-'}, {sw or '-'})") for did, uuid, name, hw, model, sw in
                conn.execute(f"SELECT ROWID, uuid, name, hardware, model, software FROM {m}.source_devices")}
    return sources, hardware


def print_moves(title, moves):
    if not moves:
        log(f"{title}: none")
        return
    log(f"{title}: {sum(moves.values()):,}")
    for (before, after), n in sorted(moves.items(), key=lambda kv: (-kv[1], kv[0])):
        log(f"  {n:>10,}  {pad(before, 44)} -> {after}" + (" (another record with the same details)"
                                                           if before == after else ""))


def print_sources(conn):
    def load(m):
        return {uuid: (deleted, f"{name or '-'} ({ptype or '-'}) {bundle or '-'}") for uuid, name, ptype, deleted, bundle
                in conn.execute(f"SELECT so.uuid, so.name, so.product_type, so.deleted, l.bundle_id FROM {m}.sources so "
                                f"LEFT JOIN {m}.logical_sources l ON l.ROWID = so.logical_source_id")}

    a, b = load("a_m"), load("b_m")
    gone = Counter(lab for u, (d, lab) in a.items() if not d and (u not in b or b[u][0]))
    added = Counter(lab for u, (d, lab) in b.items() if u not in a)
    changed = sorted((a[u][1], lab) for u, (d, lab) in b.items() if u in a and not d and not a[u][0] and a[u][1] != lab)
    for title, items in (("Sources deleted", gone), ("Sources added", added)):
        if items:
            log(f"{title}: {sum(items.values())}")
            for lab, n in sorted(items.items()):
                log(f"  {lab}" + (f" x{n}" if n > 1 else ""))
    if changed:
        log(f"Sources renamed or moved: {len(changed)}")
        for before, after in changed:
            log(f"  {before}\n    -> {after}")


def print_hardware(conn):
    q_hw = "SELECT uuid, name, coalesce(nullif(hardware, ''), model) FROM {}.source_devices"
    a = {u: (n, hw or "-") for u, n, hw in conn.execute(q_hw.format("a_m"))}
    b = {u: (n, hw or "-") for u, n, hw in conn.execute(q_hw.format("b_m"))}
    for title, mine, other in (("Hardware records removed", a, b), ("Hardware records added", b, a)):
        items = Counter(v for u, v in mine.items() if u not in other)
        if items:
            log(f"{title}: {sum(items.values()):,}")
            for (name, hw), n in sorted(items.items()):
                log(f"  {pad(name, 28)} {hw:<12} {n:>4}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", type=Path, required=True, help="directory with the original databases")
    ap.add_argument("--after", type=Path, required=True, help="directory with the cleaned-up databases")
    args = ap.parse_args()
    dirs = {"before": args.before.resolve(), "after": args.after.resolve()}
    for d in dirs.values():
        missing = [n for n in (MAIN_DB, SECURE_DB) if not (d / n).is_file()]
        if missing:
            sys.exit(f"Missing {', '.join(missing)} in {d}")

    tmp = Path(tempfile.mkdtemp(prefix="health-compare-"))
    try:
        conn = sqlite3.connect(":memory:", uri=True)
        for (side, d), prefix in zip(dirs.items(), "ab"):
            uris = readable(d, tmp / side)
            for name, suffix in ((MAIN_DB, "m"), (SECURE_DB, "s")):
                schema = f"{prefix}_{suffix}"
                conn.execute("ATTACH DATABASE ? AS " + schema, (uris[name],))
                conn.execute(f"PRAGMA {schema}.cache_size = -262144")
        conn.execute("CREATE TEMP TABLE gone_deletions(data_id INTEGER PRIMARY KEY)")
        conn.execute(f"INSERT INTO gone_deletions SELECT x.data_id FROM a_s.objects x WHERE x.type = {DELETION_RECORD} "
                     "AND NOT EXISTS (SELECT 1 FROM b_s.objects y WHERE y.data_id = x.data_id)")
        return compare(conn, dirs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare(conn, dirs):
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        log(f"[{'OK' if cond else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")

    log(f"Before: {dirs['before']}\nAfter:  {dirs['after']}")
    results = {}
    info = {s: tables(conn, s) for s in ("a_m", "b_m", "a_s", "b_s")}
    records = record_tables(conn, "a_s", info["a_s"]) | record_tables(conn, "b_s", info["b_s"])
    for name, a, b in ((MAIN_DB, "a_m", "b_m"), (SECURE_DB, "a_s", "b_s")):
        names = sorted(set(info[a]) | set(info[b]))
        log(f"Comparing {len(names)} tables in {name} ...")
        for t in names:
            results[name, t] = diff_table(conn, a, b, t, info[a], info[b])

    log("\n=== Health records ===")
    bad = []
    for t in sorted(records):
        r = results[SECURE_DB, t]
        if (r["removed"] - r["removed_deletion"] or r["added"] or r["note"]
                or set(r["cols"]) - SOURCE_COLS.get(t, set())):
            bad.append(t)
    count = "SELECT count(*), total(type = {0}) FROM {1}.objects".format(DELETION_RECORD, "{}")
    (na, da), (nb, db) = (conn.execute(count.format(s)).fetchone() for s in ("a_s", "b_s"))
    kept = [c for c in info["a_s"]["objects"][0] if c not in SOURCE_COLS["objects"]]
    check("objects: same health records; " + ", ".join(kept) + " unchanged", "objects" not in bad,
          f"{na - int(da):,} -> {nb - int(db):,}")
    rows = sum(results[SECURE_DB, t]["before"] for t in records if t != "objects")
    check(f"Record contents unchanged in {len(records) - 1} other tables", not set(bad) - {"objects"},
          f"{rows:,} rows")
    for t in bad:
        r = results[SECURE_DB, t]
        log(f"  {t}: {r['before']:,} -> {r['after']:,} rows; removed {r['removed']:,}, added {r['added']:,}, "
            f"changed {r['changed']:,}" + (f" ({', '.join(f'{c} {n:,}' for c, n in r['cols'].items())})"
                                            if r["cols"] else "") + (f"; {r['note']}" if r["note"] else ""))
    ta, tb = per_type(conn, "a_s"), per_type(conn, "b_s")
    diff = sorted(dt for dt in set(ta) | set(tb) if dt not in ta or dt not in tb or ta[dt][0] != tb[dt][0]
                  or not math.isclose(ta[dt][1], tb[dt][1], rel_tol=1e-12))
    check(f"Record count and quantity total unchanged for each of {len(ta)} data types", not diff,
          f"differs for data types {diff[:20]}" if diff else "")
    gone = {t: r["removed_deletion"] for (n, t), r in results.items() if n == SECURE_DB and r["removed_deletion"]}
    log(f"Deletion records (they only tell synced devices to delete data): {int(da):,} -> {int(db):,}"
        + ("; removed rows: " + ", ".join(f"{t} {n:,}" for t, n in sorted(gone.items())) if gone else ""))
    for t, cols in SOURCE_COLS.items():
        r = results[SECURE_DB, t]
        n = sum(r["cols"].get(c, 0) for c in cols)
        if n:
            log(f"{t}: {', '.join(sorted(cols))} changed for {n:,} rows (records moved to other sources)")

    log("")
    src_a, hw_a = owners(conn, "a_m")
    src_b, hw_b = owners(conn, "b_m")
    src_moves, hw_moves = Counter(), Counter()
    for sa, sb, da, db, n in conn.execute(
            "SELECT pa.source_id, pb.source_id, pa.device_id, pb.device_id, count(*) "
            "FROM a_s.objects x JOIN b_s.objects y ON y.data_id = x.data_id "
            "LEFT JOIN a_s.data_provenances pa ON pa.ROWID = x.provenance "
            "LEFT JOIN b_s.data_provenances pb ON pb.ROWID = y.provenance GROUP BY 1, 2, 3, 4"):
        for moves, ids, ma, mb in ((src_moves, (sa, sb), src_a, src_b), (hw_moves, (da, db), hw_a, hw_b)):
            # Data can reference a missing record; it stays put if the ID is unchanged.
            (ua, la), (ub, lb) = (m.get(i, (("missing", i), f"(missing {i})")) for m, i in zip((ma, mb), ids))
            if ua != ub:
                moves[la, lb] += n
    print_moves("Records moved to another source", src_moves)
    print_moves("Records moved to another hardware record", hw_moves)

    log("\n=== Other changes ===")
    print_sources(conn)
    print_hardware(conn)
    for name in (MAIN_DB, SECURE_DB):
        changed = [(t, r) for (n, t), r in sorted(results.items()) if n == name and t not in
                   (records if name == SECURE_DB else ())
                   and (r["removed"] or r["added"] or r["changed"] or r["note"])]
        total = sum(1 for n, t in results if n == name)
        log(f"{name}: {len(changed)} of {total} tables changed" + (":" if changed else ""))
        if changed:
            log(f"  {'Table':<40} {'Before':>10} {'After':>10} {'Removed':>9} {'Added':>9} {'Changed':>9}")
        for t, r in changed:
            log(f"  {t:<40} {r['before']:>10,} {r['after']:>10,} {r['removed']:>9,} {r['added']:>9,} "
                f"{r['changed']:>9,}" + (f"  ({r['note']})" if r["note"] else ""))

    log("\nAll health records are unchanged." if ok else "\nSome health records differ; see [FAIL] above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
