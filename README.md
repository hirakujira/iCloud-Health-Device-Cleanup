# Merge duplicate device sources in Apple Health

English | [繁體中文](README_zh.md)

After a few iPhone or Apple Watch upgrades, the Health app's Sources list can fill up with old devices. The Devices page in your profile can also show many entries for the same iPhone or Apple Watch, because each OS update creates another hardware record. The app lets you delete all data from a device, but it doesn't let you merge devices while keeping their data.

These two scripts modify the Health databases directly to move records from old devices to your current ones, **without deleting any health records**. You need to extract the databases from an encrypted iPhone backup, put the modified files back into the backup, then restore it to your phone. The instructions below use [iMazing](https://imazing.com/).

| File | Purpose |
| --- | --- |
| `merge_health_sources.py` | Merge sources: move old iPhone records to your current iPhone, and old Apple Watch records to your current watch |
| `prune_empty_devices.py` | Clean up the Devices page: remove hardware records with no data; with `--collapse`, change the hardware references for all iPhone / Apple Watch data to the current devices |

Both scripts use only the Python 3 standard library. No additional packages are needed.

## Before you start

- **This is an unofficial method.** The database schema is undocumented and Apple may change it at any time. Incorrect changes can corrupt your Health databases. Keep an untouched original backup so you can restore it if anything goes wrong. These scripts have only been tested with iOS 27 databases.
- **Use an encrypted backup.** Unencrypted backups do not contain health data.
- **Turn off iCloud sync for Health and delete the health data stored in iCloud first.** Turn off Health sync on other devices, such as an iPad, too. Otherwise, the modified data may sync with the old cloud data. Turn sync back on only after checking the results on your phone.
- **Restoring returns your phone to its state at the time of the backup.** Any data added to the phone between the backup and the restore will be lost, not just health data. Try to complete the entire process in one session.
- The scripts open the original files read-only and modify only copies in the `--out` directory. The output directory must not already exist.

## How the current devices are selected

| Item | Default selection | Manual override |
| --- | --- | --- |
| Current iPhone | The device marked as local in the database (`sources.local_device`) | `--iphone-target` |
| Current Apple Watch | The most recently paired watch (the last entry in `nano_pairing`) | `--watch-target` |
| Source for overlapping data | The old device of the same type with the most records, renamed to `Old iPhone` or `Old Apple Watch` | `--iphone-legacy`, `--watch-legacy` |

The preview lists each device's `lid`, name, model, record count, and date of its most recent data. It also shows whether the target was detected automatically or specified manually. If the selection is wrong, use a `lid` from the list, such as `--watch-target 12`, or a bundle ID in the form `com.apple.health.<UUID>`. Use `--keep` to exclude a device from merging; you can repeat this option.

### Why keep Old iPhone and Old Apple Watch sources?

If two devices recorded data during the same period, moving everything into one source could double-count cumulative values such as steps and distance. For example, you might wear both watches for a few days when switching to a new one, or carry two phones that both record steps and sync to the same Health database through iCloud. Records that overlap with another device's records are therefore moved to the Old iPhone or Old Apple Watch source instead of the target. Health handles overlaps between separate sources according to source priority, rather than counting them twice.

**These sources contain real health records. Think carefully before deleting them in Health.** Choosing Delete All Data on a source or device permanently deletes all records under that entry.

The scripts' output and default legacy source names are in English. To use different source names, pass `--iphone-legacy-name` and `--watch-legacy-name` when running `merge_health_sources.py`.

## Instructions

### 1. Extract the databases with iMazing

1. Enable Backup Encryption in iMazing's device settings, then make a new backup of your iPhone.
2. Select that backup under Backups and choose Create an Editable Copy of this Backup. Keep the original backup untouched so you can restore it if needed.
3. Open File System in the editable copy and go to `HealthDomain/Health/`.
4. Copy `healthdb.sqlite` and `healthdb_secure.sqlite` to the same directory on your computer, such as `backup/`. If `healthdb.sqlite-wal` or `healthdb_secure.sqlite-wal` exists, copy it too. When applying changes, the scripts first checkpoint any pending changes in the `-wal` files into the databases.

### 2. Run the scripts

Run each command as a preview first. Check the plan, then add `--apply` to create the modified copies.

```sh
# Merge sources
python3 -B merge_health_sources.py --src backup --out merged
python3 -B merge_health_sources.py --src backup --out merged --apply

# Clean up devices (optional; skip this and use merged/ if not needed)
python3 -B prune_empty_devices.py --src merged --out final --collapse
python3 -B prune_empty_devices.py --src merged --out final --collapse --apply
```

- Each `--apply` run automatically validates its output. The final line must be `All checks passed.` If any check shows `[FAIL]`, do not use that output.
- If `prune_empty_devices.py` prints `No device records to clean up; no output created.`, use `merged/` in the next step instead.
- If the merge warns that an old watch `is still in nano_pairing (watch pairing records)`, confirm that you have unpaired it in the Watch app. Old pairing records can remain after unpairing; you can continue in that case.
- Without `--collapse`, `prune_empty_devices.py` only removes hardware records with no data. With `--collapse`, the model and OS version recorded for old data are replaced with those of the current devices. **This cannot be undone in the modified copy.**

Validation includes:

- Unchanged record counts in the checked data tables.
- Unchanged totals for each type of numeric data.
- Expected record counts for each source.
- No records pointing to missing sources or hardware records.
- No time overlaps between different devices within the merged target source.
- SQLite `quick_check` and `foreign_key_check` checks.

### 3. Put the files back and restore your phone

1. In the editable backup's `HealthDomain/Health/` directory, replace `healthdb.sqlite` and `healthdb_secure.sqlite` with the files from your output directory (`final/` or `merged/`).
2. Delete `healthdb.sqlite-wal`, `healthdb.sqlite-shm`, `healthdb_secure.sqlite-wal`, and `healthdb_secure.sqlite-shm` from that same directory. These files belong to the databases before modification; leaving them in place could corrupt the new databases.
3. Use iMazing to restore the editable backup to your iPhone. If Find My is enabled, iMazing will ask you to turn it off. You will also need the backup password.
4. After restoring, open Health and check that:
   - Sources contains only your current devices and the Old iPhone / Old Apple Watch sources.
   - Devices contains only your current iPhone, Apple Watch, and other accessories, such as AirPods.
5. Once everything looks correct, turn iCloud sync for Health back on.

If the results are wrong, restore the untouched original backup from step 1 to return your phone to its state before the changes.

## Recommended workflow

1. Follow the steps above to clean up your existing data.
2. For a device you no longer want to record data, such as a spare phone, turn off its iCloud sync for Health before deleting its local health data. Do not reverse this order: deleting data while sync is still on also deletes it from iCloud and your other devices.
3. Use `--keep` for any devices you still use and want to retain as separate sources.
4. Run the scripts again if duplicate device sources build up later.

## Known limitations

- Only Apple iPhone and Apple Watch device sources (`com.apple.health.<UUID>`) are processed. Sources written by apps and accessories such as AirPods are left unchanged.
- If multiple watches are paired, automatic selection picks the most recently paired one. Check the target in the preview.
- Each iOS or watchOS update can still create another hardware record. Run `prune_empty_devices.py` again if the Devices page fills up.

## Disclaimer

This project is not affiliated with Apple or iMazing. Modifying system databases carries a risk of data loss or corruption. Use your own judgment and keep an untouched original backup.
