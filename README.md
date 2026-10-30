# 合併「健康」App 重複的裝置來源

換過幾支 iPhone 或 Apple Watch 之後，「健康」App 的「來源」列表會留下一堆舊裝置。個人資料頁的「裝置」也會出現很多個 iPhone 和 Apple Watch，因為裝置每換一次系統版本，就會多一筆硬體紀錄。App 裡只能「刪除所有資料」，沒辦法保留資料、只把裝置合併。

這裡的兩支腳本會直接修改「健康」的資料庫，把舊裝置的紀錄併到目前的裝置上，**不刪除任何健康紀錄**。資料庫要從 iPhone 的加密備份取出，修改後放回備份，再還原到手機。下面以 [iMazing](https://imazing.com/) 說明操作方式。

| 檔案 | 用途 |
| --- | --- |
| `merge_health_sources.py` | 合併「來源」：舊 iPhone 的紀錄併入目前的 iPhone，舊 Apple Watch 的紀錄併入目前的手錶 |
| `prune_empty_devices.py` | 清理「裝置」頁：刪除沒有資料的硬體紀錄；加上 `--collapse` 時，所有 iPhone / Apple Watch 資料的硬體紀錄都改成目前裝置 |

只用到 Python 3 標準函式庫，不需要安裝其他套件。

## 開始之前

- **這是非官方做法。** 資料庫結構沒有公開文件，Apple 隨時可能改變，改錯也可能讓「健康」資料庫損壞。請保留一份沒動過的原始備份，出問題時用它還原。目前只在 iOS 27 的資料庫上測試過。
- **一定要用加密備份。** 未加密的備份不會包含健康資料。
- **先關閉 iCloud 的「健康」同步，並刪除 iCloud 上的健康資料。** 如果其他裝置（例如 iPad）也開著健康同步，也一起關閉。否則改好的資料可能和雲端的舊資料互相同步。確認手機上的結果正確後，再重新開啟同步。
- **還原會讓手機回到備份當下的狀態。** 從做備份到還原完成之間，手機上新增的所有資料都會消失，不只是健康資料。整個流程請盡量一次做完。
- 所有腳本都以唯讀方式開啟原始檔，只修改 `--out` 資料夾裡的複本。`--out` 指定的資料夾必須不存在。

## 怎麼決定「目前的裝置」

| 項目 | 預設判斷方式 | 手動指定 |
| --- | --- | --- |
| 目前的 iPhone | 資料庫標記為本機（`sources.local_device`）的裝置 | `--iphone-target` |
| 目前的 Apple Watch | 最後配對的手錶（`nano_pairing` 的最後一筆） | `--watch-target` |
| 存放重疊資料的來源 | 同類型舊裝置中紀錄最多的那一台，會改名為「舊 iPhone」或「舊 Apple Watch」 | `--iphone-legacy`、`--watch-legacy` |

預覽會列出每台裝置的 `lid`、名稱、型號、紀錄數和最後一筆資料的日期，並註明目標是「自動判斷」還是「手動指定」。判斷錯的話，可以用列表上的 `lid` 指定，例如 `--watch-target 12`；也可以用 `com.apple.health.<UUID>` 形式的 bundle ID。不想合併的裝置，用 `--keep` 排除，可以重複指定。

### 為什麼會有「舊 iPhone」和「舊 Apple Watch」

如果兩台裝置在同一時段都有紀錄，把這些紀錄全部併進同一個來源，步數、距離這類累計值就會重複加總。例如換錶那幾天兩支錶都戴著，或是出門時同時帶著兩支手機，兩支都在記錄步數，再透過 iCloud 同步到同一個「健康」資料庫。所以和其他裝置時間重疊的紀錄不會併入目標，而是改放到「舊 iPhone」或「舊 Apple Watch」。不同來源之間的重疊，「健康」會依來源優先順序處理，不會重複計算。

**這兩個來源存的是真實紀錄，在「健康」App 裡刪除之前請謹慎考慮。** 在「來源」或「裝置」頁按「刪除所有資料」，會永久刪除該項目底下的所有紀錄。

## 操作步驟

### 1. 用 iMazing 取出資料庫

1. 在 iMazing 的裝置設定中開啟備份加密（Backup Encryption），然後替 iPhone 做一份新備份。
2. 在「Backups」選取這份備份，按「Create an Editable Copy of this Backup」，建立可編輯副本。原本的備份保持不動，出問題時可以用它還原。
3. 打開可編輯副本的「File System」，進入 `HealthDomain/Health/`。
4. 把 `healthdb.sqlite`、`healthdb_secure.sqlite` 複製到電腦的同一個資料夾，例如 `backup/`。如果有 `healthdb.sqlite-wal` 或 `healthdb_secure.sqlite-wal`，也一起複製。腳本會先把 `-wal` 裡尚未寫入的內容併入資料庫。

### 2. 執行腳本

每個指令都先執行一次預覽，確認內容後，再加上 `--apply` 產生修改後的複本。

```sh
# 合併來源
python3 -B merge_health_sources.py --src backup --out merged
python3 -B merge_health_sources.py --src backup --out merged --apply

# 清理裝置（不需要的話可以跳過，直接使用 merged/）
python3 -B prune_empty_devices.py --src merged --out final --collapse
python3 -B prune_empty_devices.py --src merged --out final --collapse --apply
```

- 每次 `--apply` 都會自動驗證，最後一行必須是「全部驗證通過。」。只要出現 `[FAIL]`，就不要使用這份輸出。
- 如果 `prune_empty_devices.py` 顯示「沒有需要清理的裝置紀錄，未產生輸出。」，表示不需要清理，下一步改用 `merged/`。
- 合併時如果出現「仍在 nano_pairing（手錶配對紀錄）中」的警告，請確認那些舊手錶已經在 Watch App 解除配對。解除配對後，舊的配對紀錄仍可能留在資料庫裡，這種情況可以繼續。
- 不加 `--collapse` 時，`prune_empty_devices.py` 只會刪除沒有資料的硬體紀錄。加上 `--collapse` 後，舊資料原本記錄的型號和系統版本會改成目前裝置，**無法復原**。

驗證項目包括：

- 各資料表的紀錄筆數不變。
- 各種數值資料的總和不變。
- 每個來源的紀錄筆數符合預期。
- 沒有紀錄指向不存在的來源或硬體紀錄。
- 合併後的目標來源內，不同裝置之間沒有時間重疊。
- SQLite 的 `quick_check` 和 `foreign_key_check` 都正常。

### 3. 放回備份並還原到手機

1. 在可編輯副本的 `HealthDomain/Health/` 中，用輸出資料夾（`final/` 或 `merged/`）裡的 `healthdb.sqlite`、`healthdb_secure.sqlite` 覆蓋原本的檔案。
2. 刪除同一個資料夾裡的 `healthdb.sqlite-wal`、`healthdb.sqlite-shm`、`healthdb_secure.sqlite-wal`、`healthdb_secure.sqlite-shm`。這些是修改前的資料庫留下的暫存檔，留著的話可能讓新的資料庫損壞。
3. 用 iMazing 把這份可編輯副本還原（Restore）到 iPhone。手機開著「尋找」時，iMazing 會要求先關閉；也需要輸入備份密碼。
4. 還原完成後，打開「健康」App 確認：
   - 「來源」只剩目前的裝置，以及「舊 iPhone」、「舊 Apple Watch」。
   - 「裝置」頁只剩目前的 iPhone、Apple Watch 和其他配件（例如 AirPods）。
5. 確認沒問題後，再開啟 iCloud 的「健康」同步。

如果結果不對，用第 1 步保留的原始備份還原，手機就會回到修改前的狀態。

## 建議做法

1. 依照上面的步驟整理一次。
2. 之後不需要記錄的裝置（例如備用手機），先關閉那台裝置的 iCloud「健康」同步，再刪除它本機的健康資料。順序不能反過來：同步還開著時刪除，iCloud 和其他裝置上的資料也會一起被刪掉。
3. 還在使用、想保留為獨立來源的裝置，執行腳本時用 `--keep` 排除。
4. 之後如果又出現多個裝置來源，再用這兩支腳本處理一次。

## 已知限制

- 只處理 Apple 的 iPhone 和 Apple Watch 裝置來源（`com.apple.health.<UUID>`）。App 寫入的來源和 AirPods 等配件不會改動。
- 同時配對多支手錶時，自動判斷會選最後配對的那一支。請在預覽時確認目標是否正確。
- 之後每次更新 iOS 或 watchOS，系統仍會新增一筆硬體紀錄。「裝置」頁的項目又變多時，可以再執行一次 `prune_empty_devices.py`。

## 使用風險

這個專案與 Apple 和 iMazing 都沒有關係。修改系統資料庫有一定風險，請自行評估，並確實保留原始備份。
