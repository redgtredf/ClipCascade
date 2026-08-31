# Windows clipboard history: data, upgrade and uninstall

Applies to the Windows desktop client (the build produced from
`ClipCascade_Desktop/src/ClipCascade_win.spec`). The clipboard history feature
stores everything locally on the machine; nothing is synced, replayed to the
server or included in backups made by the app.

## Where the data lives

ClipCascade for Windows runs as a single portable executable. All persistent
data lives **next to the executable**, in the install folder:

| Path | Contents |
| --- | --- |
| `<install folder>\ClipCascade.exe` | The application itself. |
| `<install folder>\DATA` | Login/session config (JSON; the saved password field is empty unless you disabled encryption, and the hashed password is obfuscated, not encrypted). |
| `<install folder>\clipcascade_log.log` | Application log. |
| `<install folder>\history\history.db` | Clipboard history database. Text, link and image payloads and file-batch metadata are **encrypted** (AES-GCM) before they touch the disk. |
| `<install folder>\history\master-key.dpapi` | The history master key, wrapped with **Windows DPAPI** for the Windows user that created it. |
| `<install folder>\history\blobs\` | Encrypted pending file-transfer bytes (pending downloads and image payloads awaiting retention expiry). |
| `<install folder>\history\thumbnails\` | Locally generated image thumbnails. |
| `<install folder>\history\quarantine\` | Damaged/incompatible history data moved aside by the app instead of being deleted. |

Downloaded files are different: **Save/Download all** writes to a folder you
choose (by default your Downloads folder). Those copies are plain files you
own; the app never deletes them.

## Upgrading

Upgrading means replacing `ClipCascade.exe` with a newer build (no installer
touches any other file):

- The `DATA` file and the whole `history\` folder are **not** part of the
  executable, so encrypted history, pinned entries and pending transfer bytes
  survive the upgrade untouched.
- A newer build reading an older history database migrates the schema
  in place; if it encounters data it cannot safely use, it quarantines the
  files under `history\quarantine\` instead of deleting them, and starts a
  fresh history.
- History stays decryptable only on the same Windows user profile: the master
  key is DPAPI-wrapped, so copying the `history\` folder to another machine or
  another Windows user yields encrypted bytes that cannot be opened. That is
  by design.

## Uninstalling

There is no installer-side uninstaller; removing ClipCascade means deleting
files, so you choose what happens to your data:

| You delete | Effect |
| --- | --- |
| Only `ClipCascade.exe` | App is gone. All history data, config and logs remain on disk. |
| `ClipCascade.exe` + `DATA` | App and login/config gone. History remains, still encrypted, and would be re-adopted by a future reinstall under the same Windows user. |
| `ClipCascade.exe` + `history\` | Everything history-related is gone: database, wrapped key and pending transfer bytes. Without `master-key.dpapi` any leftover `history.db`/`blobs\` content is unrecoverable ciphertext. |
| The whole install folder | Complete removal. |
| The whole install folder + downloaded files | Complete removal including the copies you downloaded into your chosen folders. |

Pending file-transfer bytes (batches shown as "Ready to download") live only
inside `history\blobs\`, so deleting the `history\` folder also discards any
transfers you had not downloaded yet. Deleting a single history entry, or
letting retention expire a transfer, never touches files you already
downloaded elsewhere.
