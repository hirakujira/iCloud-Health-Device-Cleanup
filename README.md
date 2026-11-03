# Merge duplicate devices in Apple Health

English | [繁體中文](README_zh.md)

After a few iPhone or Apple Watch upgrades, the Health app fills up with old devices:

- The sources list shows several iPhones and Apple Watches.
- The Devices page in your profile also shows several iPhones and Apple Watches.

The app only lets you delete all data from a device. It can't merge devices while keeping their data. The scripts in this project do exactly that: **they move records from old devices to your current devices without deleting any health records.**

The process is: extract the Health databases from an encrypted iPhone backup → modify them on your computer with the scripts → put them back into the backup → restore the backup to your phone. The instructions below use [iMazing](https://imazing.com/).

## Contents

1. [What each script does](#what-each-script-does)
2. [Terms used in this guide](#terms-used-in-this-guide)
3. [Before you start](#before-you-start)
4. [Overview of the process](#overview-of-the-process)
5. [Step-by-step instructions](#step-by-step-instructions)
6. [All options](#all-options)
7. [Common situations and examples](#common-situations-and-examples)
8. [What the messages mean](#what-the-messages-mean)
9. [Why are there Old iPhone and Old Apple Watch sources?](#why-are-there-old-iphone-and-old-apple-watch-sources)
10. [Known limitations](#known-limitations)
11. [Disclaimer](#disclaimer)

## What each script does

| Script | What it does | Does it change data? |
| --- | --- | --- |
| `list_health_sources.py` | **View**: list the sources and devices in the databases and how many records each one has | No, it only reads |
| `merge_health_sources.py` | **Merge sources**: move old iPhone records to your current iPhone, and old Apple Watch records to your current watch | Only the copies in the `--out` directory |
| `prune_empty_devices.py` | **Clean up the Devices page**: remove hardware records with no data (optional) | Only the copies in the `--out` directory |

Order of use: **view first → then merge → then clean up if needed**.

The scripts use only what comes with Python 3. No extra packages are needed. Type `python3 --version` in Terminal; if it prints a version number, you're ready.

Run every command **inside this project's directory**, because the scripts use code from each other. The `-B` in the commands stops Python from creating cache files; just type it as shown.

## Terms used in this guide

| Term | Meaning |
| --- | --- |
| **Source** | A device or app that writes health data. Open any data type (for example, Steps) and scroll to Data Sources & Access at the bottom to see them. Each iPhone, each Apple Watch, and each app is a source. |
| **lid** | A source's number in the database, such as `1` or `39`. This is the easiest way to name a device in an option. **Everyone's numbers are different**; use `list_health_sources.py` to find yours. |
| **Bundle ID** | Another name for a source, such as `com.apple.health.1A2B3C4D-...`. You can use it instead of a lid. |
| **Hardware record** | An entry on the Devices page in your profile. It stores a model and an OS version. A new one may appear every time an iPhone or watch updates its OS. |
| **Target device** | The iPhone or Apple Watch you **use now**. Records from old devices move here. |
| **Old device** | An iPhone or Apple Watch to be merged. After merging, it disappears from the sources list. |
| **Overlapping records** | Records from two devices for **the same period of time**, such as the days you wore both watches while switching. See [this section](#why-are-there-old-iphone-and-old-apple-watch-sources). |
| **Preview** | A command **without** `--apply`. It only shows what would happen and creates no files. |
| **Apply** | A command **with** `--apply`. It actually creates the modified database copies. |

Sources and devices are not the same thing:

- `merge_health_sources.py` works on **sources**.
- `prune_empty_devices.py` works on **hardware records** (the Devices page).

## Before you start

Check each item:

- [ ] **Keep an untouched original backup.** This is an unofficial method. Apple doesn't document the database format and may change it at any time. If something breaks, this backup is how you recover. The scripts have only been tested with iOS 27.
- [ ] **Use an encrypted backup.** Unencrypted backups **do not include** health data.
- [ ] **Turn off iCloud sync for Health and delete the health data stored in iCloud.** If another device, such as an iPad, also syncs Health, turn it off there too. Otherwise, the modified data may be overwritten by or mixed with the old cloud data. Turn sync back on only after checking the results on your phone.
- [ ] **Try to finish the whole process in one go.** Restoring returns your phone to the moment the backup was made. Everything added to the phone after the backup will be lost, not just health data.
- [ ] **Unpair old watches in the Watch app.**

## Overview of the process

| Step | What to do | Where |
| --- | --- | --- |
| 1 | Make an encrypted backup and copy the two database files to `backup/` | iMazing |
| 2 | See which devices you have and their lids | `list_health_sources.py` |
| 3 | Preview the merge → check it → apply it to create `merged/` | `merge_health_sources.py` |
| 4 | Check the merged result | `list_health_sources.py` |
| 5 | (Optional) Preview the cleanup → check it → apply it to create `final/` | `prune_empty_devices.py` |
| 6 | Put the modified files back into the backup and delete the old temporary files | iMazing |
| 7 | Restore the backup to your phone | iMazing |
| 8 | Check the results on your phone, then turn iCloud sync back on | iPhone |

## Step-by-step instructions

### Step 1: Extract the databases with iMazing

1. Turn on Backup Encryption in iMazing's device settings, then make a new backup of your iPhone.
2. Select that backup under Backups and choose Create an Editable Copy of this Backup to make an **editable copy**. Leave the original backup alone; it is your safety net.
3. Open File System in the editable copy and go to `HealthDomain/Health/`.
4. Copy these files into a `backup/` directory inside this project's directory on your computer:
   - `healthdb.sqlite` (required)
   - `healthdb_secure.sqlite` (required)

When you're done, the directory looks like this:

```text
iCloud-Health-Device-Cleanup/
├── backup/
│   ├── healthdb.sqlite
│   └── healthdb_secure.sqlite
├── list_health_sources.py
├── merge_health_sources.py
└── prune_empty_devices.py
```

### Step 2: See which devices you have

```sh
python3 -B list_health_sources.py --src backup --devices
```

The output looks like this (this is an example; your names, numbers, and counts will differ):

```text
 lid  Name                           Model           Records  Bundle ID
   1  My iPhone                      iPhone19,4        1,200  com.apple.health.1111...
  12  Previous iPhone                iPhone18,1      400,000  com.apple.health.2222...
  15  Previous Watch                 Watch8,5      5,000,000  com.apple.health.3333...
  39  Main Watch                     Watch8,5          3,000  com.apple.health.4444...
  40  Spare Watch                    Watch6,7            700  com.apple.health.5555...
```

Write down:

- The lids of the iPhone and Apple Watch you **use now**.
- Any device you **still use and don't want to merge**.

### Step 3: Merge sources

**Preview** first (this creates no files):

```sh
python3 -B merge_health_sources.py --src backup --out merged
```

Check these things in the output:

- Is the device after `Target:` the one you **use now**? If not, choose it with `--iphone-target` or `--watch-target`.
- The table below it lists the old devices that **will be merged**. Is there one you want to keep? If so, exclude it with `--keep`.
- The `To legacy` column shows how many overlapping records will go to `Old iPhone` or `Old Apple Watch`.

If something is wrong, add options and preview again until it looks right. See [All options](#all-options) and [Common situations and examples](#common-situations-and-examples).

When it looks right, run it again with **exactly the same options** plus `--apply`:

```sh
python3 -B merge_health_sources.py --src backup --out merged --apply
```

Wait for it to finish. **The last line must be `All checks passed.`** If you see any `[FAIL]`, do not use this output.

### Step 4: Check the merged result

```sh
python3 -B list_health_sources.py --src merged --devices
```

Check that the old devices are gone and their records were added to the target devices.

### Step 5: Clean up the Devices page (optional)

You can skip this step. If you do, use the files in `merged/` in step 6.

First, look at the hardware records:

```sh
python3 -B list_health_sources.py --src merged --hardware
```

There are two levels of cleanup. Choose one:

**A. Only remove hardware records with no data (safe)**

```sh
python3 -B prune_empty_devices.py --src merged --out final
python3 -B prune_empty_devices.py --src merged --out final --apply
```

**B. Combine all iPhone and Apple Watch hardware records into the current devices (`--collapse`)**

```sh
python3 -B prune_empty_devices.py --src merged --out final --collapse
python3 -B prune_empty_devices.py --src merged --out final --collapse --apply
```

Before using `--collapse`:

- The model and OS version recorded for old data are all replaced with those of the current devices. **This cannot be undone.**
- **`--collapse` ignores `--keep` from the merge.** If you kept an iPhone or Apple Watch with `--keep` in step 3, its hardware records will also be changed to the target device's model. In that case, it's best not to use `--collapse`.
- If you used `--iphone-target` or `--watch-target` in step 3, add **the same options** here. Otherwise, the script detects the current devices on its own and may pick a different one.

Again, the last line of an `--apply` run must be `All checks passed.` If it prints `No device records to clean up; no output created.`, there is nothing to clean up; use `merged/` in step 6.

### Step 6: Put the files back into the backup

1. In iMazing, open `HealthDomain/Health/` in the editable copy.
2. **Replace** the existing `healthdb.sqlite` and `healthdb_secure.sqlite` with the ones from your output directory:
   - If you did step 5, use the files in `final/`.
   - If you skipped step 5, or step 5 created no output, use the files in `merged/`.

### Step 7: Restore to your phone

Use iMazing to restore the editable copy to your iPhone.

- If Find My is on, iMazing will ask you to turn it off first.
- You will need the backup password.

### Step 8: Check on your phone

Open Health and check that:

- The sources list contains only your current devices, devices you kept with `--keep`, and `Old iPhone` / `Old Apple Watch` (these appear only when there were overlapping records).
- If you used `--collapse` in step 5: the Devices page contains only your current iPhone, Apple Watch, and other accessories such as AirPods. Without `--collapse`, hardware records for older models stay as long as they still have data. That is normal.

Once everything looks correct, turn iCloud sync for Health back on.

**If the results are wrong**, restore the untouched original backup from step 1 to return your phone to its state before the changes.

## All options

Wherever an option takes a "device", you can use a **lid** (such as `39`) or a **bundle ID** (such as `com.apple.health.1A2B...`). Find lids with `list_health_sources.py`.

### `list_health_sources.py` (view only, changes nothing)

| Option | Required? | Description |
| --- | --- | --- |
| `--src DIR` | Required | Directory containing `healthdb.sqlite` and `healthdb_secure.sqlite`. Can be `backup`, `merged`, or `final`. |
| `--devices` | Optional | List only iPhones and Apple Watches. Without `--hardware`, this excludes apps; with `--hardware`, it excludes AirPods and other accessories, and the `unused` count covers only the listed records. To see everything `prune_empty_devices.py` will remove, leave out `--devices`. |
| `--hardware` | Optional | List the hardware records on the Devices page instead. Entries marked `unused` have no data and will be removed by `prune_empty_devices.py`. The last column, `Source`, shows which iPhone or Apple Watch sources (name and lid) wrote data that uses the record, most records first; for AirPods this is the iPhone they were used with, and `-` means only apps wrote data. `Created` is when the hardware record was created, and `Last data` is the date of the latest record. If any data references hardware records that no longer exist, a `[WARNING]` at the end lists their IDs, record counts, and first and latest record dates. `__NONE__` is an internal entry; it is summarized on one line and never changed. |

Common commands:

```sh
python3 -B list_health_sources.py --src backup                       # all sources, including apps
python3 -B list_health_sources.py --src backup --devices             # only iPhones and Apple Watches
python3 -B list_health_sources.py --src merged --hardware            # hardware records on the Devices page
python3 -B list_health_sources.py --src merged --hardware --devices  # only iPhone and Apple Watch hardware records (no AirPods)
```

### `merge_health_sources.py` (merge sources)

| Option | Required? | Description | If omitted |
| --- | --- | --- | --- |
| `--src DIR` | Required | Directory with the original databases, usually `backup`. Files here are only read, never changed. | – |
| `--out DIR` | Required | Output directory, such as `merged`. **It must not already exist.** Required for previews too, but not created. | – |
| `--apply` | Optional | Actually create the modified copies. | Preview only; no files created |
| `--keep DEVICE` | Optional, repeatable | **Don't merge** this device; leave it as it is. Repeat for more devices, such as `--keep 39 --keep 40`. | All old devices are merged |
| `--iphone-target DEVICE` | Optional | Your **current iPhone**. Old iPhone records move to it. | The iPhone marked as local in the database |
| `--watch-target DEVICE` | Optional | Your **current Apple Watch**. Old watch records move to it. | The most recently paired watch |
| `--iphone-legacy DEVICE` | Optional | Which old iPhone's source holds overlapping records. Must be an old iPhone that is being merged. | The old iPhone with the most records |
| `--watch-legacy DEVICE` | Optional | Which old watch's source holds overlapping records. Must be an old watch that is being merged. | The old watch with the most records |
| `--iphone-legacy-name NAME` | Optional | New name for the source holding overlapping iPhone records. | `Old iPhone` |
| `--watch-legacy-name NAME` | Optional | New name for the source holding overlapping watch records. | `Old Apple Watch` |

### `prune_empty_devices.py` (clean up the Devices page, optional)

| Option | Required? | Description | If omitted |
| --- | --- | --- | --- |
| `--src DIR` | Required | Directory with the databases to clean up, usually `merged`. Files here are only read, never changed. | – |
| `--out DIR` | Required | Output directory, such as `final`. **It must not already exist.** Required for previews too, but not created. | – |
| `--apply` | Optional | Actually create the modified copies. | Preview only; no files created |
| `--collapse` | Optional | Combine all iPhone and Apple Watch hardware records into the current devices, then remove the records left empty. **Cannot be undone and ignores `--keep`.** | Only hardware records with no data are removed |
| `--drop-empty-provenance` | Optional | Some hardware records (such as some AirPods) have no health records but are still referenced by empty provenance entries, so they show 0 records without being marked `unused`. This deletes those empty entries first, then removes the hardware records too. No health records are deleted. | These hardware records are kept |
| `--relink OLD=NEW` | Optional, repeatable | Move data from hardware record OLD to the existing record NEW, for example `--relink 165=184`. OLD can be a record that no longer exists but is still referenced by data (listed in the `[WARNING]` at the end of `list_health_sources.py --hardware`), or an existing record, to combine several records for the same device (such as one pair of AirPods) into one. An existing OLD has no data afterwards and is removed; **its model and firmware details are lost and cannot be restored**. The preview shows both sides' name, model, firmware, and creation date, plus the record count and first and latest record dates; check that each pair is the same device before applying. Point every OLD directly to the record you want to keep. | This data is left unchanged |
| `--iphone-target DEVICE` | Optional | Your **current iPhone**. Only used with `--collapse`. Use the same value as in the merge. | The iPhone marked as local in the database |
| `--watch-target DEVICE` | Optional | Your **current Apple Watch**. Only used with `--collapse`. Use the same value as in the merge. | The most recently paired watch |

## Common situations and examples

The lids below are only examples. **Replace them with the numbers you found with `list_health_sources.py`.** In each example, preview first, then add `--apply` to the **end of the same command** once it looks right.

### Situation 1: The simplest case, all defaults

The detected targets are correct, and you want to merge every old device:

```sh
python3 -B merge_health_sources.py --src backup --out merged
python3 -B merge_health_sources.py --src backup --out merged --apply
```

### Situation 2: You still use a device and don't want to merge it

For example, lid 40 is a spare watch you still use:

```sh
python3 -B merge_health_sources.py --src backup --out merged --keep 40
python3 -B merge_health_sources.py --src backup --out merged --keep 40 --apply
```

### Situation 3: The detected target is wrong

For example, the preview's `Target:` isn't the watch you wear now, and the one you wear is lid 39:

```sh
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --apply
```

Without `--keep`, every other watch is merged into lid 39, including the most recently paired one.

### Situation 4: Merge old watches into another watch and leave the last paired one alone

For example, the preview picked the most recently paired watch (lid 40) as the target, but you want old watch records merged into lid 39 and lid 40 left as it is:

```sh
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --keep 40
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --keep 40 --apply
```

Result:

- Records from the other old watches move to lid 39.
- Records that overlap with lid 39 go to `Old Apple Watch`.
- Lid 40 keeps its name and records unchanged.
- The iPhone merge is unaffected.

In this case, it's best **not** to use `--collapse` in step 5, because it would change lid 40's hardware records to lid 39's model.

### Situation 5: Use different names for the overlapping-record sources

```sh
python3 -B merge_health_sources.py --src backup --out merged --iphone-legacy-name "Previous iPhone" --watch-legacy-name "Previous Watch"
```

Wrap names that contain spaces in straight double quotes `"`.

### Situation 6: Run it again

The `--out` directory must not already exist. To run again, either:

- Delete the old output directory (for example, `merged`) first, or
- Use a new name, such as `--out merged2`.

The original `backup/` is never changed, so you can reuse it as many times as you like.

## What the messages mean

| Message | Meaning | What to do |
| --- | --- | --- |
| `This is a preview. Check the plan, then add --apply ...` | This was only a preview. No files were created. | If the plan looks right, run the same command again with `--apply`. |
| `All checks passed.` | Every check passed. | You can use this output. |
| `[FAIL] ...` or `Validation failed. Do not use this output ...` | A check failed. | **Do not use this output.** Delete the output directory, check your options, and try again. |
| `[WARNING] ... is still in nano_pairing ...` | A watch being merged still has a pairing record. | Confirm you unpaired it in the Watch app. If you did, you can continue; old records often remain after unpairing. If you still use that watch, keep it with `--keep`. |
| `iPhone: no old devices to merge` or `Apple Watch: no old devices to merge` | There are no old devices of this type to merge. | Nothing to do. |
| `No devices to merge; no output created.` | There was nothing to merge, so no output was created. | Nothing to do. |
| `No device records to clean up; no output created.` | `prune_empty_devices.py` found no hardware records to clean up. | Use `merged/` in step 6. |
| `Cannot detect the current iPhone; specify --iphone-target ...` (or Apple Watch) | The script can't tell which device you use now. | Find the lid with `list_health_sources.py --devices`, then pass it with `--iphone-target` or `--watch-target`. |
| `Invalid target or legacy source for ...` | The target or the source for overlapping records is invalid. For example, the device type doesn't match, the target and that source are the same device, or that source was excluded with `--keep`. | Check the lids you passed to `--*-target`, `--*-legacy`, and `--keep`. |
| `No source found for lid ...` | The lid you passed doesn't exist. | Look it up again with `list_health_sources.py`. |
| `Missing healthdb.sqlite ...` | The `--src` directory doesn't contain the database files. | Check the directory name and make sure both files are in it. |
| `FileExistsError: ...` | The `--out` directory already exists. | Delete it or choose another name. |
| `Stopped: ... conflicting derived_flags ...` | The cleanup found data it can't combine safely. It stopped and created no output. | Clean up without `--collapse`, or skip step 5. |
| `[WARNING] Data references ... missing hardware records ...` | Some data references hardware records that no longer exist. | Use the listed dates to find the matching existing hardware records, then repair with `prune_empty_devices.py --relink OLD=NEW`. |

Every `--apply` run checks these automatically:

- Record counts in the data tables are unchanged.
- Totals for each type of numeric data are unchanged.
- Each source has the expected number of records.
- No records point to missing sources or hardware records.
- Within the merged target source, records from different devices don't overlap in time.
- SQLite `quick_check` and `foreign_key_check` pass.

## Why are there Old iPhone and Old Apple Watch sources?

If two devices recorded data **during the same period**, putting all of it into one source would **count** cumulative values such as steps and distance **twice**. For example:

- You wore both watches for a few days while switching.
- You carried two phones, and both were counting steps.

So the script leaves these overlapping records in the old device's original source and renames that source to `Old iPhone` or `Old Apple Watch`. Health handles overlaps between separate sources by source priority, so nothing is counted twice.

- If there are no overlapping records, `Old iPhone` and `Old Apple Watch` don't appear.
- To use different names, pass `--iphone-legacy-name` and `--watch-legacy-name`.

**These sources contain real health records. Don't delete them casually.** Choosing Delete All Data on a source or device permanently deletes every record under it.

## Afterwards

1. For a device that no longer needs to record data, such as a spare phone: **first** turn off iCloud sync for Health on that device, **then** delete its local health data. Don't reverse the order, or the data will also be deleted from iCloud and your other devices.
2. Exclude devices you still use and want to keep as separate sources with `--keep` when merging.
3. If old devices pile up again later, follow this guide again.

## Known limitations

- Only Apple iPhones and Apple Watches are processed. Sources written by apps are left unchanged.
- **AirPods and other accessories are not merged.** AirPods don't have their own source; their data is written by the iPhone and only appears as hardware records on the Devices page. `prune_empty_devices.py` removes an AirPods hardware record only if it has no data at all, and `--collapse` does not combine AirPods. To combine several hardware records for the same pair of AirPods, list each one with `--relink`. New hardware records may still appear later when the iPhone records headphone audio or heart rate, or after a firmware update.
- If several watches are paired, automatic detection picks the most recently paired one. Check `Target:` in the preview.
- Each iOS or watchOS update may still create another hardware record. Run `prune_empty_devices.py` again if the Devices page fills up.

## Disclaimer

This project is not affiliated with Apple or iMazing. Modifying system databases carries risk. Use your own judgment and keep an untouched original backup.
