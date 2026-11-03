# 合併「健康」App 重複的裝置

[English](README.md) | 繁體中文

換過幾支 iPhone 或 Apple Watch 之後，「健康」App 裡會留下一堆舊裝置：

- 「來源」列表裡有好幾個 iPhone 和 Apple Watch。
- 個人資料頁的「裝置」裡也有好幾個 iPhone 和 Apple Watch。

App 只提供「刪除所有資料」，沒辦法「保留資料、只合併裝置」。這個專案的腳本可以做到這件事：**把舊裝置的紀錄搬到目前的裝置上，不刪除任何健康紀錄。**

做法是：從 iPhone 的加密備份取出「健康」的資料庫 → 在電腦上用腳本修改 → 放回備份 → 還原到手機。下面用 [iMazing](https://imazing.com/) 示範。

## 目錄

1. [三支腳本各做什麼](#三支腳本各做什麼)
2. [先認識幾個名詞](#先認識幾個名詞)
3. [開始之前一定要做的事](#開始之前一定要做的事)
4. [整個流程一覽](#整個流程一覽)
5. [詳細步驟](#詳細步驟)
6. [所有參數說明](#所有參數說明)
7. [常見情況與範例](#常見情況與範例)
8. [畫面上的訊息是什麼意思](#畫面上的訊息是什麼意思)
9. [為什麼會多出「Old iPhone」和「Old Apple Watch」](#為什麼會多出old-iphone和old-apple-watch)
10. [已知限制](#已知限制)
11. [使用風險](#使用風險)

## 三支腳本各做什麼

| 腳本 | 做什麼 | 會不會修改資料 |
| --- | --- | --- |
| `list_health_sources.py` | **查看**：列出資料庫裡有哪些來源和裝置、各有幾筆紀錄 | 不會，只會讀取 |
| `merge_health_sources.py` | **合併來源**：把舊 iPhone 的紀錄搬到目前的 iPhone，把舊 Apple Watch 的紀錄搬到目前的手錶 | 只修改 `--out` 資料夾裡的複本 |
| `prune_empty_devices.py` | **清理「裝置」頁**：刪除沒有資料的硬體紀錄（選用） | 只修改 `--out` 資料夾裡的複本 |

使用順序：**先查看 → 再合併 → 需要的話再清理**。

這些腳本只用到 Python 3 內建的功能，不需要安裝其他套件。在終端機輸入 `python3 --version`，有顯示版本號碼就可以使用。

所有指令都要在**這個專案的資料夾裡**執行，因為三支腳本會互相引用。指令裡的 `-B` 是讓 Python 不要產生快取檔案，照著打就好。

## 先認識幾個名詞

| 名詞 | 意思 |
| --- | --- |
| **來源** | 寫入健康資料的裝置或 App。打開任何一種資料（例如「步數」），在最下方的「資料來源與取用權」可以看到。每台 iPhone、每支 Apple Watch、每個 App 都是一個來源。 |
| **lid** | 每個來源在資料庫裡的編號，例如 `1`、`39`。指定裝置時最常用它。**每個人的編號都不一樣**，請用 `list_health_sources.py` 查自己的。 |
| **bundle ID** | 來源的另一種名稱，例如 `com.apple.health.1A2B3C4D-...`。可以代替 lid 使用。 |
| **硬體紀錄** | 個人資料頁「裝置」裡看到的項目。上面記著型號和系統版本。iPhone 或手錶每更新一次系統，就可能多一筆。 |
| **目標裝置** | 你**現在使用**的 iPhone 或 Apple Watch。舊裝置的紀錄會搬到這裡。 |
| **舊裝置** | 要被合併掉的 iPhone 或 Apple Watch。合併後，它會從「來源」列表消失。 |
| **重疊紀錄** | 兩台裝置在**同一段時間**都有的紀錄，例如換錶那幾天兩支都戴著。詳見[這一節](#為什麼會多出old-iphone和old-apple-watch)。 |
| **預覽** | 指令**沒有**加 `--apply`。只顯示「會怎麼做」，不產生任何檔案。 |
| **套用** | 指令**有**加 `--apply`。真的產生修改後的資料庫複本。 |

「來源」和「裝置」是兩回事：

- `merge_health_sources.py` 處理的是**來源**。
- `prune_empty_devices.py` 處理的是**硬體紀錄**（「裝置」頁）。

## 開始之前一定要做的事

請一項一項確認：

- [ ] **保留一份沒動過的原始備份。** 這是非官方做法，資料庫的格式 Apple 沒有公開，隨時可能改變。改壞了，就靠這份備份救回來。目前只在 iOS 27 測試過。
- [ ] **使用加密備份。** 沒有加密的備份**不包含**健康資料。
- [ ] **關閉 iCloud 的「健康」同步，並刪除 iCloud 上的健康資料。** iPad 等其他裝置如果也開著健康同步，也要關閉。不然改好的資料可能被雲端的舊資料蓋掉，或互相混在一起。等手機上的結果確認沒問題，再重新打開。
- [ ] **整個流程盡量一次做完。** 還原會讓手機回到「做備份那一刻」的狀態。做備份之後在手機上新增的所有東西都會消失，不只是健康資料。
- [ ] **舊手錶已經在 Watch App 解除配對。**

## 整個流程一覽

| 步驟 | 做什麼 | 在哪裡做 |
| --- | --- | --- |
| 1 | 做加密備份，取出兩個資料庫檔案到 `backup/` | iMazing |
| 2 | 查看有哪些裝置和它們的 lid | `list_health_sources.py` |
| 3 | 預覽合併 → 確認 → 套用，產生 `merged/` | `merge_health_sources.py` |
| 4 | 檢查合併結果 | `list_health_sources.py` |
| 5 | （選用）預覽清理 → 確認 → 套用，產生 `final/` | `prune_empty_devices.py` |
| 6 | 把修改後的檔案放回備份，刪掉舊的暫存檔 | iMazing |
| 7 | 把備份還原到手機 | iMazing |
| 8 | 在手機上檢查結果，再打開 iCloud 同步 | iPhone |

## 詳細步驟

### 步驟 1：用 iMazing 取出資料庫

1. 在 iMazing 的裝置設定中打開備份加密（Backup Encryption），然後替 iPhone 做一份新備份。
2. 在「Backups」選這份備份，按「Create an Editable Copy of this Backup」，建立一份**可編輯副本**。原本的備份不要動，它就是你的保險。
3. 打開可編輯副本的「File System」，進入 `HealthDomain/Health/`。
4. 把下面的檔案複製到電腦上這個專案資料夾裡的 `backup/` 資料夾：
   - `healthdb.sqlite`（一定要）
   - `healthdb_secure.sqlite`（一定要）

完成後，資料夾看起來像這樣：

```text
iCloud-Health-Device-Cleanup/
├── backup/
│   ├── healthdb.sqlite
│   └── healthdb_secure.sqlite
├── list_health_sources.py
├── merge_health_sources.py
└── prune_empty_devices.py
```

### 步驟 2：查看有哪些裝置

```sh
python3 -B list_health_sources.py --src backup --devices
```

畫面會像這樣（以下是範例，你的名稱、編號和數字會不同）：

```text
 lid  Name                           Model           Records  Bundle ID
   1  My iPhone                      iPhone19,4        1,200  com.apple.health.1111...
  12  Previous iPhone                iPhone18,1      400,000  com.apple.health.2222...
  15  Previous Watch                 Watch8,5      5,000,000  com.apple.health.3333...
  39  Main Watch                     Watch8,5          3,000  com.apple.health.4444...
  40  Spare Watch                    Watch6,7            700  com.apple.health.5555...
```

請記下：

- 你**現在使用**的 iPhone 和 Apple Watch 是哪個 lid。
- 有沒有哪台裝置**還在用、不想合併**。

### 步驟 3：合併來源

先**預覽**（不會產生任何檔案）：

```sh
python3 -B merge_health_sources.py --src backup --out merged
```

仔細看畫面上的這幾件事：

- `Target:` 後面是不是你**現在使用**的裝置？如果不是，請用 `--iphone-target` 或 `--watch-target` 指定。
- 下面表格列出的，是**會被合併掉**的舊裝置。裡面有不想合併的裝置嗎？有的話，用 `--keep` 排除。
- `To legacy` 這一欄是重疊紀錄的數量，這些紀錄會放到 `Old iPhone` 或 `Old Apple Watch`。

不對就加上參數再預覽一次，直到結果正確為止。參數請看[所有參數說明](#所有參數說明)和[常見情況與範例](#常見情況與範例)。

確認沒問題後，用**完全相同的參數**再加上 `--apply` 執行：

```sh
python3 -B merge_health_sources.py --src backup --out merged --apply
```

等它跑完，**最後一行必須是 `All checks passed.`**。只要畫面上有任何 `[FAIL]`，就不要使用這份輸出。

### 步驟 4：檢查合併結果

```sh
python3 -B list_health_sources.py --src merged --devices
```

確認舊裝置已經不見，紀錄數加到了目標裝置上。

### 步驟 5：清理「裝置」頁（選用）

這一步可以跳過。跳過的話，步驟 6 就用 `merged/` 裡的檔案。

先看看有哪些硬體紀錄：

```sh
python3 -B list_health_sources.py --src merged --hardware
```

清理有兩種程度，選一種：

**A. 只刪除沒有資料的硬體紀錄（安全）**

```sh
python3 -B prune_empty_devices.py --src merged --out final
python3 -B prune_empty_devices.py --src merged --out final --apply
```

**B. 把所有 iPhone 和 Apple Watch 的硬體紀錄都併成目前的裝置（`--collapse`）**

```sh
python3 -B prune_empty_devices.py --src merged --out final --collapse
python3 -B prune_empty_devices.py --src merged --out final --collapse --apply
```

使用 `--collapse` 前請注意：

- 舊資料原本記錄的型號和系統版本，會全部改成目前裝置的。**這無法復原。**
- **`--collapse` 不理會合併時的 `--keep`。** 如果你在步驟 3 用 `--keep` 保留了某台 iPhone 或 Apple Watch，它的硬體紀錄也會被改成目標裝置的型號。這種情況建議不要用 `--collapse`。
- 如果步驟 3 用了 `--iphone-target` 或 `--watch-target`，這裡也要加上**一樣的參數**。不然它會自動判斷，可能選到另一台。

同樣地，`--apply` 的最後一行必須是 `All checks passed.`。如果顯示 `No device records to clean up; no output created.`，代表沒有東西需要清理，步驟 6 改用 `merged/`。

### 步驟 6：把檔案放回備份

1. 在 iMazing 打開可編輯副本的 `HealthDomain/Health/`。
2. 用輸出資料夾裡的 `healthdb.sqlite` 和 `healthdb_secure.sqlite`，**覆蓋**原本的同名檔案：
   - 有做步驟 5，就用 `final/` 裡的檔案。
   - 沒做步驟 5，或步驟 5 沒有產生輸出，就用 `merged/` 裡的檔案。

### 步驟 7：還原到手機

用 iMazing 把這份可編輯副本還原（Restore）到 iPhone。

- 如果手機開著「尋找」，iMazing 會要求你先關閉。
- 需要輸入備份密碼。

### 步驟 8：在手機上檢查

打開「健康」App，確認：

- 「來源」裡只剩目前的裝置、你用 `--keep` 保留的裝置，以及 `Old iPhone`、`Old Apple Watch`（只有在有重疊紀錄時才會出現）。
- 有做步驟 5 的 `--collapse`：「裝置」頁只剩目前的 iPhone、Apple Watch 和其他配件（例如 AirPods）。沒有用 `--collapse` 的話，舊型號的硬體紀錄只要還有資料就會保留，這是正常的。

都沒問題後，再打開 iCloud 的「健康」同步。

**結果不對的話**：用步驟 1 保留的原始備份還原，手機就會回到修改前的狀態。

## 所有參數說明

參數裡的「裝置」可以填 **lid**（例如 `39`），也可以填 **bundle ID**（例如 `com.apple.health.1A2B...`）。lid 用 `list_health_sources.py` 查。

### `list_health_sources.py`（查看，不會修改任何檔案）

| 參數 | 必填？ | 說明 |
| --- | --- | --- |
| `--src 資料夾` | 必填 | 放 `healthdb.sqlite` 和 `healthdb_secure.sqlite` 的資料夾。可以是 `backup`、`merged`、`final`。 |
| `--devices` | 選填 | 只列出 iPhone 和 Apple Watch。不加 `--hardware` 時排除 App；加上 `--hardware` 時排除 AirPods 等配件，最後一行的 `unused` 數量也只計算列出的項目。想知道 `prune_empty_devices.py` 實際會刪掉哪些，請不要加 `--devices`。 |
| `--hardware` | 選填 | 改列「裝置」頁的硬體紀錄。標示 `unused` 的是沒有資料、會被 `prune_empty_devices.py` 刪掉的項目。最後一欄 `Source` 是哪個 iPhone 或 Apple Watch 來源（名稱和 lid）寫入了使用這筆硬體紀錄的資料，紀錄多的排前面；AirPods 會顯示和它搭配使用的 iPhone，只有 App 寫入時顯示 `-`。`Created` 是這筆硬體紀錄建立的日期，`Last data` 是最新一筆紀錄的日期。如果有資料指向已經不存在的硬體紀錄，最後會用 `[WARNING]` 另外列出它們的 ID、筆數，以及第一筆和最新一筆紀錄的日期。`__NONE__` 是系統內部用的項目，只會彙總成一行，腳本不會動它。 |

常用指令：

```sh
python3 -B list_health_sources.py --src backup                       # 所有來源（含 App）
python3 -B list_health_sources.py --src backup --devices             # 只看 iPhone 和 Apple Watch
python3 -B list_health_sources.py --src merged --hardware            # 「裝置」頁的硬體紀錄
python3 -B list_health_sources.py --src merged --hardware --devices  # 只看 iPhone 和 Apple Watch 的硬體紀錄（不含 AirPods）
```

### `merge_health_sources.py`（合併來源）

| 參數 | 必填？ | 說明 | 沒填時 |
| --- | --- | --- | --- |
| `--src 資料夾` | 必填 | 放原始資料庫的資料夾，通常是 `backup`。這裡的檔案只會讀取，不會被修改。 | – |
| `--out 資料夾` | 必填 | 輸出資料夾，例如 `merged`。**必須是還不存在的資料夾。** 預覽時也要填，但不會建立。 | – |
| `--apply` | 選填 | 真的產生修改後的複本。 | 只預覽，不產生檔案 |
| `--keep 裝置` | 選填，可重複 | 這台裝置**不要合併**，保持原樣。要保留多台就寫多次，例如 `--keep 39 --keep 40`。 | 所有舊裝置都合併 |
| `--iphone-target 裝置` | 選填 | 指定**目前的 iPhone**。舊 iPhone 的紀錄會搬到這台。 | 資料庫標記為「本機」的 iPhone |
| `--watch-target 裝置` | 選填 | 指定**目前的 Apple Watch**。舊手錶的紀錄會搬到這支。 | 最後配對的手錶 |
| `--iphone-legacy 裝置` | 選填 | 指定用哪一台舊 iPhone 的來源存放重疊紀錄。必須是會被合併的舊 iPhone。 | 紀錄最多的舊 iPhone |
| `--watch-legacy 裝置` | 選填 | 指定用哪一支舊手錶的來源存放重疊紀錄。必須是會被合併的舊手錶。 | 紀錄最多的舊手錶 |
| `--iphone-legacy-name 名稱` | 選填 | 存放 iPhone 重疊紀錄的來源要改成什麼名字。 | `Old iPhone` |
| `--watch-legacy-name 名稱` | 選填 | 存放手錶重疊紀錄的來源要改成什麼名字。 | `Old Apple Watch` |

### `prune_empty_devices.py`（清理「裝置」頁，選用）

| 參數 | 必填？ | 說明 | 沒填時 |
| --- | --- | --- | --- |
| `--src 資料夾` | 必填 | 要清理的資料庫資料夾，通常是 `merged`。這裡的檔案只會讀取，不會被修改。 | – |
| `--out 資料夾` | 必填 | 輸出資料夾，例如 `final`。**必須是還不存在的資料夾。** 預覽時也要填，但不會建立。 | – |
| `--apply` | 選填 | 真的產生修改後的複本。 | 只預覽，不產生檔案 |
| `--collapse` | 選填 | 把所有 iPhone 和 Apple Watch 的硬體紀錄併成目前的裝置，再刪除變空的紀錄。**無法復原，而且不理會 `--keep`。** | 只刪除沒有資料的硬體紀錄 |
| `--drop-empty-provenance` | 選填 | 有些硬體紀錄（例如部分 AirPods）底下沒有任何健康紀錄，只被空的 provenance 引用，所以顯示 0 筆卻沒有標 `unused`。加上這個參數會先刪掉這些空的 provenance，再把這類硬體紀錄一起刪除。不會刪除任何健康紀錄。 | 這類硬體紀錄會保留 |
| `--relink 舊=新` | 選填，可重複 | 把「舊」硬體紀錄的資料改指向現有的「新」紀錄，例如 `--relink 165=184`。「舊」可以是已經不存在、但仍有資料指向它的紀錄（`list_health_sources.py --hardware` 最後的 `[WARNING]` 會列出來），也可以是現有的紀錄，用來把同一個裝置的多筆紀錄併成一筆（例如同一副 AirPods）。現有的「舊」紀錄改完後沒有資料，會被刪除，**它的型號和韌體資訊會消失，無法復原**。預覽會列出每組兩邊的名稱、型號、韌體、建立日期，以及筆數、第一筆和最新一筆紀錄的日期，請確認是同一個裝置再套用。每個「舊」都要直接指向最後要保留的紀錄。 | 不修改這類資料 |
| `--iphone-target 裝置` | 選填 | 指定**目前的 iPhone**，只在加上 `--collapse` 時有作用。請和合併時用的一樣。 | 資料庫標記為「本機」的 iPhone |
| `--watch-target 裝置` | 選填 | 指定**目前的 Apple Watch**，只在加上 `--collapse` 時有作用。請和合併時用的一樣。 | 最後配對的手錶 |

## 常見情況與範例

下面範例裡的 lid 只是舉例，**請換成你自己用 `list_health_sources.py` 查到的編號**。每個範例都是先預覽，確認後再在**同一行最後**加上 `--apply`。

### 情況 1：最簡單，全部用預設

自動判斷的目標正確，所有舊裝置都要合併：

```sh
python3 -B merge_health_sources.py --src backup --out merged
python3 -B merge_health_sources.py --src backup --out merged --apply
```

### 情況 2：有一台裝置還在用，不想合併

例如 lid 40 是你還在用的備用手錶：

```sh
python3 -B merge_health_sources.py --src backup --out merged --keep 40
python3 -B merge_health_sources.py --src backup --out merged --keep 40 --apply
```

### 情況 3：自動判斷選錯了目標

例如預覽的 `Target:` 顯示的不是你現在戴的手錶，而你現在戴的是 lid 39：

```sh
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --apply
```

沒有加 `--keep` 的話，其他所有手錶都會併入 lid 39，包括最後配對的那一支。

### 情況 4：把舊手錶併到另一支手錶，最後配對的那支保持不動

例如預覽自動選了最後配對的手錶（lid 40）當目標，但你想把舊手錶的紀錄併入 lid 39，lid 40 則保持原樣：

```sh
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --keep 40
python3 -B merge_health_sources.py --src backup --out merged --watch-target 39 --keep 40 --apply
```

結果：

- 其他舊手錶的紀錄搬到 lid 39。
- 和 lid 39 時間重疊的紀錄放到 `Old Apple Watch`。
- lid 40 的名稱和紀錄完全不變。
- iPhone 的合併不受影響。

這種情況下，步驟 5 建議**不要**用 `--collapse`，因為它會把 lid 40 的硬體紀錄也改成 lid 39 的型號。

### 情況 5：重疊紀錄的來源想用中文名稱

```sh
python3 -B merge_health_sources.py --src backup --out merged --iphone-legacy-name "舊 iPhone" --watch-legacy-name "舊 Apple Watch"
```

名稱有空格時，要用英文雙引號 `"` 包起來。

### 情況 6：想重新跑一次

`--out` 指定的資料夾必須還不存在。想重跑時，二選一：

- 先刪掉舊的輸出資料夾（例如 `merged`）。
- 換一個新名字，例如 `--out merged2`。

原始的 `backup/` 不會被修改，可以一直重複使用。

## 畫面上的訊息是什麼意思

| 訊息 | 意思 | 該怎麼做 |
| --- | --- | --- |
| `This is a preview. Check the plan, then add --apply ...` | 這只是預覽，沒有產生任何檔案。 | 確認內容沒問題後，加上 `--apply` 再執行一次。 |
| `All checks passed.` | 全部驗證通過。 | 可以使用這份輸出。 |
| `[FAIL] ...` 或 `Validation failed. Do not use this output ...` | 有驗證沒通過。 | **不要使用這份輸出。** 刪掉輸出資料夾，檢查參數後重來。 |
| `[WARNING] ... is still in nano_pairing ...` | 某支要被合併的手錶還留有配對紀錄。 | 確認它已經在 Watch App 解除配對。已經解除的話可以繼續；解除配對後，舊紀錄常常還會留著。如果那支手錶還在使用，請用 `--keep` 保留它。 |
| `iPhone: no old devices to merge` 或 `Apple Watch: no old devices to merge` | 這類裝置沒有需要合併的舊裝置。 | 不用做任何事。 |
| `No devices to merge; no output created.` | 完全沒有需要合併的裝置，沒有產生輸出。 | 不用做任何事。 |
| `No device records to clean up; no output created.` | `prune_empty_devices.py` 沒有找到需要清理的硬體紀錄。 | 步驟 6 改用 `merged/`。 |
| `Cannot detect the current iPhone; specify --iphone-target ...`（或 Apple Watch） | 腳本判斷不出你目前使用的裝置。 | 用 `list_health_sources.py --devices` 查出 lid，再用 `--iphone-target` 或 `--watch-target` 指定。 |
| `Invalid target or legacy source for ...` | 指定的目標或存放重疊紀錄的來源不正確。例如型號類型不符、目標和存放來源是同一台，或存放來源被 `--keep` 排除了。 | 檢查 `--*-target`、`--*-legacy`、`--keep` 填的 lid。 |
| `No source found for lid ...` | 填的 lid 不存在。 | 用 `list_health_sources.py` 重新查。 |
| `Missing healthdb.sqlite ...` | `--src` 指定的資料夾裡找不到資料庫檔案。 | 確認資料夾名稱，以及兩個檔案都有放進去。 |
| `FileExistsError: ...` | `--out` 指定的資料夾已經存在。 | 刪掉它，或換一個名字。 |
| `Stopped: ... conflicting derived_flags ...` | 清理時發現無法安全合併的資料，已經停止，沒有產生輸出。 | 改用不加 `--collapse` 的清理，或跳過步驟 5。 |
| `[WARNING] Data references ... missing hardware records ...` | 有資料指向已經不存在的硬體紀錄。 | 依列出的日期找出對應的現有硬體紀錄，用 `prune_empty_devices.py --relink 舊=新` 修復。 |

每次 `--apply` 都會自動檢查這些項目：

- 各資料表的紀錄筆數沒有變。
- 各種數值資料的總和沒有變。
- 每個來源的紀錄筆數符合預期。
- 沒有紀錄指向不存在的來源或硬體紀錄。
- 合併後的目標來源裡，不同裝置之間沒有時間重疊。
- SQLite 的 `quick_check` 和 `foreign_key_check` 都正常。

## 為什麼會多出「Old iPhone」和「Old Apple Watch」

如果兩台裝置在**同一段時間**都有紀錄，把它們全部放進同一個來源，步數、距離這類會累加的數值就會**算兩次**。例如：

- 換錶那幾天，兩支手錶都戴著。
- 出門時同時帶著兩支手機，兩支都在記錄步數。

所以腳本會把這些「時間重疊」的紀錄留在舊裝置原本的來源，並把它改名為 `Old iPhone` 或 `Old Apple Watch`。不同來源之間的重疊，「健康」App 會依照來源的優先順序處理，不會重複計算。

- 沒有重疊紀錄時，就不會出現 `Old iPhone` 或 `Old Apple Watch`。
- 想換名字，請用 `--iphone-legacy-name` 和 `--watch-legacy-name`。

**這兩個來源裡是真實的健康紀錄，請不要隨便刪除。** 在「來源」或「裝置」頁按「刪除所有資料」，會永久刪除那個項目底下的所有紀錄。

## 之後的建議

1. 不再需要記錄的裝置（例如備用手機）：**先**關閉那台裝置的 iCloud「健康」同步，**再**刪除它本機的健康資料。順序不能反過來，不然 iCloud 和其他裝置上的資料也會一起被刪掉。
2. 還在使用、想保留成獨立來源的裝置，執行合併時用 `--keep` 排除。
3. 之後又出現很多舊裝置時，照這份說明再做一次。

## 已知限制

- 只處理 Apple 的 iPhone 和 Apple Watch。App 寫入的來源不會改動。
- **AirPods 等配件不會合併。** AirPods 沒有自己的來源，它的資料是由 iPhone 寫入的，只會出現在「裝置」頁的硬體紀錄裡。`prune_empty_devices.py` 只會在 AirPods 的硬體紀錄完全沒有資料時才刪除它，`--collapse` 也不會合併 AirPods。要把同一副 AirPods 的多筆硬體紀錄併成一筆，請用 `--relink` 逐筆指定。之後 iPhone 記錄耳機音量、耳機心率，或耳機更新韌體時，系統仍可能再新增硬體紀錄。
- 同時配對多支手錶時，自動判斷會選最後配對的那一支。請在預覽時確認 `Target:` 是否正確。
- 之後每次更新 iOS 或 watchOS，系統仍可能新增一筆硬體紀錄。「裝置」頁又變多時，可以再執行一次 `prune_empty_devices.py`。

## 使用風險

這個專案和 Apple、iMazing 都沒有關係。修改系統資料庫有一定的風險，請自行評估，並確實保留原始備份。
