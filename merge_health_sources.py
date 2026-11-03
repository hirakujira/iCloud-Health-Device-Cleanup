#!/usr/bin/env python3
"""Merge duplicate iPhone / Apple Watch sources in the Health app.

Original databases are always opened read-only. Use --apply to copy them to --out
and modify only the copies.

Rules:
- Move old iPhone records to the current iPhone, and old Apple Watch records to
  the current watch. By default, use the local iPhone (local_device) and the most
  recently paired watch (nano_pairing); override with --iphone-target / --watch-target.
- Move overlapping records to a legacy source of the same device type instead
  (--iphone-legacy / --watch-legacy, defaulting to the old device with the most records)
  to avoid double-counting cumulative values such as steps and distance.
- Mark the remaining old sources as deleted, following healthd's source deletion
  behavior, without deleting any health records.
- Carry old devices' places in the data source priority lists over to the target
  where the target is not already listed.
- Specify devices using a lid from the preview or a com.apple.health.<UUID> bundle ID.
"""

import argparse
import bisect
import shutil
import sqlite3
import sys
import time
import unicodedata
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
    old_lids: list = field(default_factory=list)  # Priority order, newer devices first.
    moving_sids: set = field(default_factory=set)
    note: str = ""
    # logical_source_order ROWIDs of removed devices: moved to the target, or dropped as duplicates.
    order_moves: list = field(default_factory=list)
    order_drops: list = field(default_factory=list)
    order_split: int = 0  # Data types where other sources ranked between the target and an old device.


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(ro_uri(path), uri=True)


def ro_uri(path: Path) -> str:
    # Encode spaces, #, and ? so SQLite does not interpret the path as URI parameters.
    return f"file:{quote(str(path))}?immutable=1&mode=ro"


def log(msg=""):
    print(msg, flush=True)


def pad(text, width):
    # Wide characters such as CJK take two terminal columns.
    cols = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - cols, 0)


def apple_date(ts):
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d", time.gmtime(ts + APPLE_EPOCH))


def provenance_stats(sec):
    """Return {provenance ID: (records, first start date, last end date)}.

    Future end dates are ignored for the last date, as in load_devices.
    """
    now = time.time() - APPLE_EPOCH
    return {pid: (n, first, last) for pid, n, first, last in sec.execute(
        "SELECT o.provenance, count(*), min(sm.start_date), max(CASE WHEN sm.end_date <= ? THEN sm.end_date END) "
        "FROM objects o LEFT JOIN samples sm ON sm.data_id = o.data_id GROUP BY 1", (now,))}


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
    """Return bundle IDs for the local iPhone (P) and last paired watch (W), or None."""
    row = main.execute(
        "SELECT l.bundle_id FROM sources so JOIN logical_sources l ON l.ROWID = so.logical_source_id "
        "WHERE so.local_device = 1 AND so.deleted = 0 AND l.bundle_id LIKE ? "
        "ORDER BY so.ROWID DESC LIMIT 1", (DEVICE_PREFIX + "%",)).fetchone()
    phone = row[0] if row else None
    # Old pairing records may remain; the highest ROWID is the most recently paired watch.
    row = main.execute(
        "SELECT n.source_bundle_id FROM nano_pairing n "
        "JOIN logical_sources l ON l.bundle_id = n.source_bundle_id "
        "ORDER BY n.ROWID DESC LIMIT 1").fetchone()
    return {"P": phone, "W": row[0] if row else None}


def to_bundle(main, value):
    """Resolve a device argument, either a lid or bundle ID, to a bundle ID."""
    if value is None or not value.isdigit():
        return value
    row = main.execute("SELECT bundle_id FROM logical_sources WHERE ROWID = ?", (int(value),)).fetchone()
    if not row:
        sys.exit(f"No source found for lid {value}")
    return row[0]


def build_plan(devices, per_sid, args, detected):
    by_bundle = {d.bundle: d for d in devices.values()}
    keep = set(args.keep or [])
    groups = {}
    specs = [("P", "iPhone", "iphone", "local device"), ("W", "Apple Watch", "watch", "last paired")]
    for kind, label, opt, auto in specs:
        target, note = getattr(args, f"{opt}_target"), "manually specified"
        if target is None:
            target, note = detected[kind], f"auto-detected: {auto}"
        candidates = [d for d in devices.values() if d.kind == kind and d.bundle not in keep]
        t = by_bundle.get(target)
        if t is None:
            if len(candidates) > 1:
                sys.exit(f"Cannot detect the current {label}; specify --{opt}-target (lid or bundle ID)")
            log(f"{label}: no old devices to merge")
            continue
        olds = [d for d in candidates if d.lid != t.lid]
        if not olds:
            log(f"{label}: no old devices to merge")
            continue
        legacy = getattr(args, f"{opt}_legacy")
        # Reuse an old device's source for overlapping data instead of creating a source.
        lg = by_bundle.get(legacy) if legacy else max(olds, key=lambda d: (d.objects, -d.lid))
        if lg is None or t.kind != kind or lg.kind != kind or lg.lid == t.lid or lg.bundle in keep:
            sys.exit(f"Invalid target or legacy source for {label}")
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
    """Return data_id sets for legacy sources by group, along with statistics."""
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
            # Instantaneous samples (start == end) are not cumulative; skip overlap checks.
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


def plan_source_order(main, groups, legacy):
    """Carry removed devices' places in the data source priority lists over to the target.

    For each data type, the target keeps its own entry if it has one; otherwise the
    highest-ranked removed device's entry moves to the target. Other entries of removed
    devices are dropped. Entries keep their ROWIDs, which set the order, so all other
    sources keep their relative order.
    """
    order = defaultdict(list)
    for rid, dt, lid in main.execute("SELECT ROWID, data_type, logical_source FROM logical_source_order ORDER BY ROWID"):
        order[dt].append((rid, lid))
    for g in groups.values():
        gone = set(g.old_lids)
        if legacy[g.kind]:
            # The legacy device keeps its logical source for the overlapping records.
            gone.discard(g.legacy_lid)
        for rows in order.values():
            ours = [i for i, (_, lid) in enumerate(rows) if lid == g.target_lid or lid in gone]
            if not ours:
                continue
            keep = next((i for i in ours if rows[i][1] == g.target_lid), ours[0])
            if rows[keep][1] != g.target_lid:
                g.order_moves.append(rows[keep][0])
            g.order_drops += [rows[i][0] for i in ours if i != keep]
            g.order_split += len(ours) > 1 and ours[-1] - ours[0] + 1 != len(ours)


def print_plan(devices, per_sid, groups, legacy, moved_by_lid, stacked):
    for g in groups.values():
        t = devices[g.target_lid]
        log(f"\n=== {g.label} ===")
        lg = devices[g.legacy_lid]
        log(f"Target: lid {t.lid} {t.rows[-1][1]} ({t.rows[-1][2]}, {t.objects:,} records, {g.note})")
        log(f"Overlapping data: lid {lg.lid} {lg.rows[-1][1]} -> renamed to '{g.legacy_name}'")
        log(f"{'lid':>4}  {'Name':<28} {'Model':<11} {'Records':>10} {'To legacy':>10}  Last data")
        for lid in g.old_lids:
            d = devices[lid]
            name = d.rows[-1][1] or "(unnamed)"
            log(f"{lid:>4}  {name:<28} {d.rows[-1][2]:<11} {d.objects:>10,} {moved_by_lid.get(lid, 0):>10,}  {apple_date(d.last_seen or None)}")
        total = sum(devices[l].objects for l in g.old_lids)
        log(f"Total: {total:,} old records; {len(legacy[g.kind]):,} overlapping records moved to '{g.legacy_name}'; "
            f"{stacked[g.kind]:,} also overlap with other old devices")
        log(f"Source priority: {len(g.order_moves):,} entries of old devices move to the target; "
            f"{len(g.order_drops):,} dropped where the target or a higher-ranked old device is already listed"
            + (f" ({g.order_split:,} data types had other sources ranked between them)" if g.order_split else ""))


def make_snapshot(src: Path, out: Path) -> Path:
    """Copy originals and checkpoint pending WAL transactions before immutable reads."""
    out.mkdir(parents=True, exist_ok=False)
    snap = out / ".snapshot"
    snap.mkdir()
    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(src / name, snap / name)
        wal = src / (name + "-wal")
        if wal.exists():
            # Skip -shm: SQLite rebuilds this index from the WAL.
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
    # Disable foreign keys so deleting provenance does not cascade to health records.
    sec.execute("PRAGMA foreign_keys=OFF")
    sec.execute("PRAGMA cache_size=-524288")
    sec.execute("PRAGMA temp_store=MEMORY")
    sec.execute("ATTACH DATABASE ? AS m", (str(out / MAIN_DB),))
    report = {}
    sec.execute("BEGIN")
    try:
        for g in groups.values():
            if not legacy[g.kind]:
                # No legacy source is needed when there are no overlapping records.
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
        # Move unused provenance too, or it would still reference deleted sources.
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
            raise RuntimeError(f"{stale} records still reference sources scheduled for deletion")
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

        # These caches can be rebuilt and become stale when source ownership changes.
        for t in ("sample_aggregate_caches", "cached_queries", "sleep_day_summary_cache"):
            report[f"cleared_{t}"] = sec.execute(f"DELETE FROM {t}").rowcount

        for t, col in (("workout_builders", "source_id"), ("workout_sessions", "source_id"),
                       ("HealthRecordsPlugin_api_reminders", "source_id")):
            n = sec.execute(f"SELECT count(*) FROM m.{t} WHERE {col} IN (SELECT sid FROM moving "
                            f"WHERE sid != legacy_sid)").fetchone()[0]
            if n:
                raise RuntimeError(f"m.{t} has {n} references to sources scheduled for deletion; end any active workouts first")
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
        # Match healthd's deletion behavior: clear names and move to the empty bundle ID.
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
            moved = sum(sec.execute(f"UPDATE m.logical_source_order SET logical_source = ? "
                                    f"WHERE ROWID = ? AND logical_source IN ({marks})",
                                    (g.target_lid, rid, *dead)).rowcount
                        for g in groups.values() for rid in g.order_moves)
            dropped = sec.execute(
                f"DELETE FROM m.logical_source_order WHERE logical_source IN ({marks})", dead).rowcount
            planned = [sum(len(getattr(g, a)) for g in groups.values()) for a in ("order_moves", "order_drops")]
            if [moved, dropped] != planned:
                raise RuntimeError(f"Source priority entries changed differently than planned: "
                                   f"moved {moved} / dropped {dropped}, expected {planned[0]} / {planned[1]}")
            report["moved_logical_source_order"] = moved
            report["deleted_logical_source_order"] = dropped
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


def reclaim_space(out: Path):
    """Return free pages to the file system, as healthd does with auto_vacuum = INCREMENTAL.

    Unlike VACUUM, this keeps every row and ROWID in place.
    Returns {file name: (auto_vacuum, page_size, page_count)} from before.
    """
    before = {}
    for name in (MAIN_DB, SECURE_DB):
        conn = sqlite3.connect(out / name, isolation_level=None)
        before[name] = tuple(conn.execute(f"PRAGMA {p}").fetchone()[0] for p in ("auto_vacuum", "page_size", "page_count"))
        if before[name][0] == 2:
            conn.execute("PRAGMA incremental_vacuum").fetchall()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    return before


def check_reclaimed(check, out: Path, before):
    for name, (mode, size, pages) in before.items():
        if mode != 2:
            continue
        conn = ro(out / name)
        n, free = (conn.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "freelist_count"))
        conn.close()
        check(f"{name}: free space returned to the file system",
              free == 0 and (out / name).stat().st_size == n * size,
              f"{pages * size / 1e6:,.1f} -> {n * size / 1e6:,.1f} MB")


def cross_device_overlaps(items):
    """Count cross-device overlaps in items: [(start, end, lid)]."""
    items.sort()
    best = []  # Keep at most two (end, lid) entries with distinct lids.
    hits = 0
    for s, e, lid in items:
        if any(be > s and bl != lid for be, bl in best):
            hits += 1
        cand = {bl: be for be, bl in best}
        cand[lid] = max(cand.get(lid, e), e)
        best = sorted(((be, bl) for bl, be in cand.items()), reverse=True)[:2]
    return hits


def validate(src: Path, out: Path, groups, devices, legacy, space):
    ok = True
    s_src, s_out = ro(src / SECURE_DB), ro(out / SECURE_DB)
    m_src, m_out = ro(src / MAIN_DB), ro(out / MAIN_DB)
    s_out.execute("ATTACH DATABASE ? AS m", (ro_uri(out / MAIN_DB),))
    s_src.execute("ATTACH DATABASE ? AS m", (ro_uri(src / MAIN_DB),))

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        log(f"[{'OK' if cond else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")

    for t in ("objects", "samples", "quantity_samples", "category_samples", "workouts",
              "data_series", "associations", "metadata_values"):
        a = s_src.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        b = s_out.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        check(f"{t} record count unchanged", a == b, f"{a:,} -> {b:,}")
    q = "SELECT data_type, total(q.quantity) FROM quantity_samples q JOIN samples s USING (data_id) GROUP BY 1"
    check("Totals unchanged for each numeric data type", dict(s_src.execute(q)) == dict(s_out.execute(q)))
    check("No records reference missing provenance", s_out.execute(
        "SELECT count(*) FROM objects o LEFT JOIN data_provenances p ON p.ROWID = o.provenance "
        "WHERE p.ROWID IS NULL").fetchone()[0] == 0)
    # healthd itself can leave data under deleted sources, so only flag references added by the merge.
    q = ("SELECT p.ROWID, p.source_id FROM data_provenances p LEFT JOIN m.sources so "
         "ON so.ROWID = p.source_id WHERE so.ROWID IS NULL OR so.deleted = 1")
    a, b = set(s_src.execute(q)), set(s_out.execute(q))
    check("No new provenance references deleted or missing sources", b <= a, f"{len(a):,} -> {len(b):,}")
    q = ("SELECT e.ROWID, e.source_id FROM external_sync_ids e JOIN m.sources so "
         "ON so.ROWID = e.source_id WHERE so.deleted = 1")
    a, b = set(s_src.execute(q)), set(s_out.execute(q))
    check("external_sync_ids has no new references to deleted sources", b <= a, f"{len(a):,} -> {len(b):,}")
    dup = s_out.execute("SELECT count(*) FROM (SELECT 1 FROM external_sync_ids WHERE deleted = 0 "
                        "GROUP BY source_id, object_code, sid HAVING count(*) > 1)").fetchone()[0]
    dup0 = s_src.execute("SELECT count(*) FROM (SELECT 1 FROM external_sync_ids WHERE deleted = 0 "
                         "GROUP BY source_id, object_code, sid HAVING count(*) > 1)").fetchone()[0]
    check("No new duplicate sync identifiers in external_sync_ids", dup <= dup0, f"{dup0} -> {dup}")

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
        check(f"{g.label} target source record count matches expectation", t_after == exp_t, f"{t_after:,} (expected {exp_t:,})")
        check(f"{g.label} '{g.legacy_name}' record count matches expectation", l_after == len(legacy[g.kind]),
              f"{l_after:,} (expected {len(legacy[g.kind]):,})")

    # Recover original device IDs to check for cross-device overlaps in the target source.
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
        check(f"{g.label} target source has no cross-device time overlaps", overlaps_found[g.kind] == 0,
              overlaps_found[g.kind])

    q = "SELECT ROWID, data_type, logical_source, user_preferred FROM logical_source_order ORDER BY ROWID"
    moved = {rid: g.target_lid for g in groups.values() for rid in g.order_moves}
    dropped = {rid for g in groups.values() for rid in g.order_drops}
    before = m_src.execute(q).fetchall()
    after = m_out.execute(q).fetchall()
    check("Source priority order changed only as planned",
          after == [(rid, dt, moved.get(rid, lid), up) for rid, dt, lid, up in before if rid not in dropped],
          f"{len(before):,} -> {len(after):,} entries")

    for name, conn, src_conn in (("healthdb_secure", s_out, s_src), ("healthdb", m_out, m_src)):
        res = conn.execute("PRAGMA quick_check").fetchall()
        check(f"{name} quick_check", res == [("ok",)], res[:3] if res != [("ok",)] else "")
        a = len(src_conn.execute("PRAGMA main.foreign_key_check").fetchall())
        b = len(conn.execute("PRAGMA main.foreign_key_check").fetchall())
        check(f"{name} foreign_key_check has no new violations", b <= a, f"{a} -> {b}")
    check_reclaimed(check, out, space)

    log("\nDevice sources remaining after merging:")
    for lid, bundle, name, ptype, n in m_out.execute(
        "SELECT l.ROWID, l.bundle_id, so.name, so.product_type, count(*) FROM logical_sources l "
        "JOIN sources so ON so.logical_source_id = l.ROWID WHERE l.bundle_id LIKE ? "
        "GROUP BY l.ROWID ORDER BY l.ROWID", (DEVICE_PREFIX + "%",)):
        log(f"  lid {lid:>3}  {name:<30} {ptype:<11} {bundle}")
    for p in out.iterdir():
        if p.name.endswith(("-wal", "-shm")):
            check(f"No remaining data in {p.name}", p.stat().st_size == 0, p.stat().st_size)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True, help="directory containing healthdb.sqlite and healthdb_secure.sqlite")
    ap.add_argument("--out", type=Path, required=True, help="output directory (must not already exist)")
    ap.add_argument("--apply", action="store_true", help="write merged copies; otherwise only preview the plan")
    ap.add_argument("--keep", action="append", metavar="DEVICE",
                    help="keep a device as a separate source (lid or bundle ID); may be repeated")
    for opt, label in (("iphone", "iPhone"), ("watch", "Apple Watch")):
        ap.add_argument(f"--{opt}-target", metavar="DEVICE", help=f"current {label} (lid or bundle ID); detected automatically by default")
        ap.add_argument(f"--{opt}-legacy", metavar="DEVICE",
                        help="legacy source for overlapping data (lid or bundle ID); defaults to the old device with the most records")
        ap.add_argument(f"--{opt}-legacy-name", default=f"Old {label}", metavar="NAME", help="new name for the source holding overlapping data")
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
        log("No devices to merge; no output created.")
        return 0

    moving_bundles = {devices[l].bundle for g in groups.values() for l in g.old_lids}
    for bundle, sp in main_ro.execute("SELECT source_bundle_id, sync_provenance FROM nano_pairing"):
        if bundle in moving_bundles:
            log(f"[WARNING] {bundle} is still in nano_pairing (watch pairing records); confirm it has been unpaired in the Watch app")

    log("Calculating overlapping intervals ...")
    legacy, moved_by_lid, stacked = assign(sec_ro, devices, groups)
    plan_source_order(main_ro, groups, legacy)
    print_plan(devices, per_sid, groups, legacy, moved_by_lid, stacked)
    if not args.apply:
        log("\nThis is a preview. Check the plan, then add --apply to create merged copies.")
        return 0

    main_ro.close()
    sec_ro.close()
    for name in (MAIN_DB, SECURE_DB):
        shutil.copy2(base / name, out / name)
    log("\nApplying changes ...")
    report = apply_changes(out, groups, legacy, devices)
    for k, v in report.items():
        log(f"  {k}: {v:,}")
    log("Reclaiming free space ...")
    space = reclaim_space(out)
    log("\nValidating ...")
    ok = validate(base, out, groups, devices, legacy, space)
    if ok:
        shutil.rmtree(base)
        log("\nAll checks passed.")
    else:
        log(f"\nValidation failed. Do not use this output (comparison snapshot retained at {base}).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
