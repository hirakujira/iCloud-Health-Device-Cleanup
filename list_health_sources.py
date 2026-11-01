#!/usr/bin/env python3
"""List Health sources with their lid, name, model, and record count.

Databases are opened read-only. Use the lids shown here with --keep,
--iphone-target, --watch-target, and the other device options.
With --hardware, list hardware records (source_devices) shown on the Devices
page instead; records marked "unused" are what prune_empty_devices.py removes.
"""

import argparse
import sys
import unicodedata
from pathlib import Path

from merge_health_sources import DEVICE_PREFIX, MAIN_DB, SECURE_DB, apple_date, log, ro, ro_uri
from prune_empty_devices import APPLE_KINDS, NONE_DEVICE, used_device_ids


def pad(text, width):
    # Wide characters such as CJK take two terminal columns.
    cols = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - cols, 0)


def list_sources(sec, devices_only):
    rows = sec.execute(
        "WITH c AS (SELECT p.source_id sid, count(*) n FROM objects o "
        "           JOIN data_provenances p ON p.ROWID = o.provenance GROUP BY 1) "
        "SELECT l.ROWID, l.bundle_id, max(CASE WHEN so.deleted = 0 THEN so.name END), "
        "       group_concat(DISTINCT nullif(so.product_type, '')), total(c.n) "
        "FROM m.logical_sources l JOIN m.sources so ON so.logical_source_id = l.ROWID "
        "LEFT JOIN c ON c.sid = so.ROWID "
        "WHERE ? = 0 OR l.bundle_id LIKE ? "
        "GROUP BY l.ROWID ORDER BY l.ROWID",
        (int(devices_only), DEVICE_PREFIX + "%"),
    ).fetchall()

    log(f"{'lid':>4}  {'Name':<30} {'Model':<12} {'Records':>10}  Bundle ID")
    for lid, bundle, name, model, n in rows:
        # healthd moves deleted sources to a logical source with an empty bundle ID.
        if not bundle:
            name = "(deleted sources)"
        log(f"{lid:>4}  {pad(name or '-', 30)} {model or '-':<12} {int(n):>10,}  {bundle or '-'}")
    log(f"\n{len(rows)} sources; {sum(int(r[4]) for r in rows):,} records")


def list_hardware(main, sec, devices_only):
    used = used_device_ids(main, sec)
    counts = dict(sec.execute("SELECT p.device_id, count(*) FROM objects o "
                              "JOIN data_provenances p ON p.ROWID = o.provenance GROUP BY 1"))
    apple = {(name, model) for name, model, _, _ in APPLE_KINDS}
    shown, unused, none_rows, none_unused, none_n = 0, 0, 0, 0, 0
    log(f"{'ID':>5}  {'Name':<30} {'Model':<12} {'Software':<10} {'Created':<10} {'Records':>10}  Status")
    for did, name, model, hw, sw, created in main.execute(
            "SELECT ROWID, name, model, hardware, software, creation_date FROM source_devices ORDER BY ROWID"):
        n = counts.get(did, 0)
        if name == NONE_DEVICE:
            none_rows += 1
            none_unused += did not in used
            none_n += n
            continue
        if devices_only and (name, model) not in apple:
            continue
        shown += 1
        unused += did not in used
        log(f"{did:>5}  {pad(name or '-', 30)} {hw or model or '-':<12} {sw or '-':<10} "
            f"{apple_date(created):<10} {n:>10,}  {'unused' if did not in used else ''}".rstrip())
    log(f"\n{shown} hardware records; {unused} unused (removed by prune_empty_devices.py)")
    if none_rows:
        log(f"{NONE_DEVICE} (no device): {none_rows} entries, {none_n:,} records; "
            f"{none_unused} unused entries are left untouched")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="directory containing healthdb.sqlite and healthdb_secure.sqlite")
    ap.add_argument("--devices", action="store_true", help="list only iPhone / Apple Watch entries")
    ap.add_argument("--hardware", action="store_true",
                    help="list hardware records on the Devices page (source_devices) instead of sources")
    args = ap.parse_args()

    src = args.src.resolve()
    missing = [n for n in (MAIN_DB, SECURE_DB) if not (src / n).is_file()]
    if missing:
        sys.exit(f"Missing {', '.join(missing)} in {src}.\n"
                 f"--src must point to the directory containing {MAIN_DB} and {SECURE_DB}.")
    for name in (MAIN_DB, SECURE_DB):
        wal = src / (name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            log(f"[WARNING] Ignoring pending changes in {wal.name}; counts may be incomplete")

    sec = ro(src / SECURE_DB)
    sec.execute("ATTACH DATABASE ? AS m", (ro_uri(src / MAIN_DB),))
    if args.hardware:
        main_db = ro(src / MAIN_DB)
        list_hardware(main_db, sec, args.devices)
        main_db.close()
    else:
        list_sources(sec, args.devices)
    sec.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
