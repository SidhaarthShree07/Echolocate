# Adding a Drive to the Windows Search Index — Programmatically & Silently

## TL;DR

- **`Search.SearchManager` isn't dead, the ProgID lookup is just fragile.** The underlying COM
  class (`CLSID_CSearchManager`) is almost always still there. Skip the ProgID and instantiate
  by raw CLSID — this alone fixes the `80040154` error in most cases (Section 0).
- **There is no WinRT replacement.** `Windows.Storage.Search` is for a UWP app's *own* private
  content, not the system-wide crawl scope. The classic CSM COM interfaces are still the only
  supported way to do this, even on the newest Windows 11 builds (Section 1).
- **Don't hand-edit the CrawlScopeManager registry keys.** They're not simple flat values — the
  real, supported mechanism is the same COM call, which updates the registry, the live
  in-memory scope, *and* notifies the running indexer in one atomic step (Section 2).
- **Microsoft ships an official CLI for exactly this** (`csmcmd`/`CrawlScopeCommandLine`) — you
  can compile it once and drop the ~30 KB `.exe` in your installer (Section 3).
- **If you just need instant name search and don't care about "the Windows index" specifically,
  bundle Everything (`es.exe`)** — it's free to redistribute commercially, doesn't require a
  heavyweight service, and is dramatically faster to stand up than fighting CSM (Section 4).

---

## Section 0 — Why you're actually seeing `80040154 REGDB_E_CLASSNOTREG`

Before writing any workaround, diagnose which of these it actually is — the fix is different for each:

| Cause | How to check | Fix |
|---|---|---|
| **Windows Search feature is fully uninstalled** (common on debloated/LTSC/Server images, some OEM images, "tiny11"-style trims) | `Get-WindowsOptionalFeature -Online -FeatureName *Search*` — if `State` isn't `Enabled`, or `srchadmin.dll` is missing from `System32`, this is it | `dism /Online /Enable-Feature /FeatureName:"SearchEngine-Client-Package" /All` (needs Windows Update access or an install.wim source) |
| **32-bit/64-bit COM mismatch** — extremely common cause of `REGDB_E_CLASSNOTREG` for *any* COM object, not just this one | Are you calling from 32-bit Python/PowerShell on a 64-bit box (or vice versa)? | Run the 64-bit interpreter (`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, 64-bit `python.exe`) |
| **`Search.SearchManager` ProgID key is missing/corrupt** even though the CLSID itself is fine (this is the most common one people hit and never diagnose) | `Get-Item "Registry::HKEY_CLASSES_ROOT\Search.SearchManager\CLSID"` — if this errors but `HKCR:\CLSID\{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}` exists, this is it | **Skip the ProgID, use the raw CLSID** (Section 3) |
| **The DLL exists but is unregistered** (rare, usually post-in-place-upgrade) | `regsvr32 C:\Windows\System32\srchadmin.dll` and see if it errors | Re-register, or fall back to the DISM enable/disable cycle above |

The DISM enable/disable cycle (`disable-feature` then `enable-feature`) is the standard Microsoft-documented repair and is what redeploys `srchadmin.dll` and re-registers its COM classes from the component store.

---

## Section 1 — Modern WinRT APIs: verdict

**No.** Microsoft did not replace the classic Crawl Scope Manager (CSM) with a WinRT equivalent.

`Windows.Storage.Search` (and its `ContentIndexer` class) exists in the WinRT surface, and it *is*
reachable from Python via the community `winrt`/`pywinrt` packages (`pip install winrt-Windows.Storage.Search`).
But it solves a different problem: `ContentIndexer` lets a **packaged (MSIX) UWP app** push a private
property bag into the system index, scoped to that app's own package identity — one app can't see or
manage another's data, and it has nothing to do with telling the indexer "go crawl this entire physical
drive." There's no WinRT call that touches the global crawl scope.

The official Microsoft documentation is explicit that the classic interfaces are still the entry point:

> "The Crawl Scope Manager (CSM) is a set of interfaces that provides methods to inform the Windows
> Search engine about containers to crawl... Developers can use the CSM to define a crawl scope
> programmatically for a new data store or protocol handler."

That page was last substantively updated in 2025 and still points at `ISearchCrawlScopeManager` —
this API surface simply hasn't been deprecated. So: `winsdk`/`pywinrt` is a dead end for this specific
task; keep it in your toolbox for things like per-app content indexing, not whole-drive indexing.

---

## Section 2 — The registry, and why you shouldn't write to it directly

### What's actually there

```
HKLM\SOFTWARE\Microsoft\Windows Search\CrawlScopeManager\Windows\SystemIndex\DefaultRules\<N>\URL
HKLM\SOFTWARE\Microsoft\Windows Search\CrawlScopeManager\Windows\SystemIndex\WorkingSetRules\<N>\URL
HKLM\SOFTWARE\Microsoft\Windows Search\Gather\Windows\SystemIndex\Sites\LocalHost\Paths\
HKLM\SOFTWARE\Microsoft\Windows Search\SetupCompletedSuccessfully   (REG_DWORD, reset flag)
```

Each numbered subkey under `DefaultRules`/`WorkingSetRules` holds one rule, e.g. a `URL` value of
`file:///D:\`. This is genuinely how the data is persisted on disk.

### Why `reg add` directly is a bad idea

1. **The key is SYSTEM-owned and ACL-locked.** Even as Administrator you must take ownership and
   grant yourself Full Control before `reg add` will succeed — that's already a second privileged
   step your installer has to script (people doing this for the dotfile-exclusion use case document
   exactly this friction: [couitchy/Windows-Search-Full-Index](https://github.com/couitchy/Windows-Search-Full-Index)).
2. **It's a read path, not confirmed as a write path.** CSM reads this on load; nothing in the
   public documentation says it re-scans the key live or that a bare `reg add` is safe/idempotent —
   people who've probed this report "if it doesn't exist, CSM just skips it," which cuts both ways.
3. **You bypass the "add root + notify indexer" transaction.** The supported COM call does three
   things atomically: updates the registry, updates the live in-memory working set inside the
   running `SearchIndexer.exe`, and (via `ReindexSearchRoot`) schedules an incremental crawl —
   all without a service restart. A raw registry write does none of the last two, so on a running
   machine your new rule may sit there inert until the next full service restart or reboot.

### If you ever need the sledgehammer (disaster recovery only)

This is the standard "reset & rebuild the whole index" script referenced across Microsoft forums —
**it discards the entire existing catalog**, not just adds a drive, and can take hours to rebuild
on a large disk. Don't use it just to add D:\; it's here for completeness/reference only:

```bat
@echo off
sc config wsearch start= disabled
net stop wsearch
reg add "HKLM\SOFTWARE\Microsoft\Windows Search" /v SetupCompletedSuccessfully /t REG_DWORD /d 0 /f
del "%ProgramData%\Microsoft\Search\Data\Applications\Windows\Windows.edb"
sc config wsearch start= delayed-auto
net start wsearch
```

**The right way to touch this data is the COM call in Section 3 — it writes the same registry keys
for you, correctly, and wakes the live service up in the process.**

---

## Section 3 — The actual fix: CSM over COM, minus the broken ProgID

### 3.1 Root cause of the "SearchManager is broken" folklore

`Search.SearchManager` is a **ProgID** — a human-readable alias that Windows resolves to a CLSID via
`HKEY_CLASSES_ROOT\Search.SearchManager\CLSID`. On a lot of real-world machines that alias key has
gone missing or gotten corrupted (partial updates, third-party "registry cleaners," WSUS/MDM image
customization) even though the actual COM class registration is intact. `New-Object -ComObject
Search.SearchManager` fails at the ProgID→CLSID lookup step, before it ever gets to the object itself.

**Fix: instantiate by the raw CLSID and skip ProgID resolution entirely.**

```
CLSID_CSearchManager = {7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}
```

(This is Microsoft's own published constant — see the [CsWin32 GUID reference](https://github.com/microsoft/CsWin32/issues/353) — and it's the exact class the official C++ sample below calls via `CoCreateInstance(CLSID_CSearchManager, ...)`.)

### 3.2 PowerShell — production-ready

```powershell
#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Add-DriveToWindowsSearchIndex {
    param(
        [Parameter(Mandatory)][string]$DriveLetter   # e.g. "D:"
    )

    $CLSID_CSearchManager = [Guid]'7D096C5F-AC08-4F1F-BEB7-5C22C517CE39'
    $url = "file:///$($DriveLetter.TrimEnd(':','\'))`:\"

    # Bypass the "Search.SearchManager" ProgID (frequently broken/missing) and
    # instantiate the COM class directly by CLSID.
    try {
        $type = [Type]::GetTypeFromCLSID($CLSID_CSearchManager)
        $searchManager = [Activator]::CreateInstance($type)
        if (-not $searchManager) { throw 'CreateInstance returned null' }
    }
    catch {
        throw "CLSID_CSearchManager isn't registered on this machine. " +
              "Windows Search is probably missing (see Section 0 diagnostics). Original error: $_"
    }

    $catalog = $searchManager.GetCatalog('SystemIndex')
    $csm     = $catalog.GetCrawlScopeManager()

    # A brand-new drive letter doesn't need a new "search root" -- the "file:" protocol
    # root already exists by default. We only need an inclusion *scope rule*.
    # AddUserScopeRule(url, include, overrideChildren, followFlags)
    [void]$csm.AddUserScopeRule($url, $true, $true, 0)
    [void]$csm.SaveAll()

    # Kick off an incremental crawl of just this root (no service restart, no full reindex).
    try   { $catalog.ReindexSearchRoot($url) }
    catch { $catalog.Reindex() }   # fallback if ReindexSearchRoot isn't supported on this build

    Write-Host "Added '$url' to the Windows Search crawl scope and started indexing it."
}

Add-DriveToWindowsSearchIndex -DriveLetter 'D:'
```

### 3.3 Python (pywin32) — same call, from your installer/app

```python
"""
pip install pywin32
Run elevated (admin). Tested logic mirrors Microsoft's own csmcmd.cpp sample.
"""
import win32com.client
import pywintypes

CLSID_CSearchManager = "{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}"


def add_drive_to_search_index(drive_letter: str = "D:") -> None:
    drive_letter = drive_letter.rstrip(":\\")
    url = f"file:///{drive_letter}:\\"

    try:
        # Passing a raw CLSID string makes pywin32/pythoncom resolve the class
        # directly (CLSIDFromString) instead of going through the ProgID
        # registry alias -- this is what sidesteps a broken "Search.SearchManager".
        search_manager = win32com.client.Dispatch(CLSID_CSearchManager)
    except pywintypes.com_error as exc:
        raise RuntimeError(
            "CLSID_CSearchManager is not registered on this machine. "
            "Check whether the 'SearchEngine-Client-Package' optional feature "
            "is installed (Get-WindowsOptionalFeature -Online -FeatureName *Search*)."
        ) from exc

    catalog = search_manager.GetCatalog("SystemIndex")
    csm = catalog.GetCrawlScopeManager()

    csm.AddUserScopeRule(url, True, True, 0)
    csm.SaveAll()

    try:
        catalog.ReindexSearchRoot(url)
    except Exception:
        catalog.Reindex()

    print(f"Added {url!r} to the Windows Search crawl scope.")


if __name__ == "__main__":
    add_drive_to_search_index("D:")
```

Both snippets do exactly what Explorer's *Indexing Options → Modify* dialog does under the hood when
you tick a drive checkbox, just without opening any UI.

### 3.4 The official Microsoft CLI you can just compile and ship

Microsoft publishes a ready-made command-line sample built on this exact API:
**[`microsoft/Windows-classic-samples` → `CrawlScopeCommandLine` (csmcmd)`](https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/Win7Samples/winui/WindowsSearch/CrawlScopeCommandLine/csmcmd.cpp)**

It already implements `AddRoots`, `AddRule`/`RemoveRule` (default or user), `Reindex`, `Reset`, `Revert`,
and enumeration — it's essentially a finished `csmcmd.exe /add_rule file:///D:\ /include /default`. If
your installer toolchain has MSVC available (or you build it once on a dev box and just ship the
resulting ~30–50 KB binary next to your Python app), this removes any COM/interop code from your app
entirely — just `subprocess.run(["csmcmd.exe", "/add_rule", "file:///D:\\", "/include", "/default"])`.

There's also a documented .NET path if you'd rather ship a small compiled helper than deal with
late-bound COM from Python: the community-maintained
**[`tlbimp-Microsoft.Search.Interop`](https://www.nuget.org/packages/tlbimp-Microsoft.Search.Interop)**
NuGet package gives you strongly-typed `CSearchManagerClass` from C#/PowerShell without hand-generating
the interop assembly yourself — see [this worked PowerShell example](https://powertoe.wordpress.com/2010/05/17/powershell-tackles-windows-desktop-search/) for the exact calling convention (note the `...Class` suffix quirk it documents for PowerShell-visible types).

---

## Section 4 — If Windows Search itself is the wrong tool: fast alternatives

### 4.1 Everything (voidtools) — the practical fallback

**Licensing/bundling — settled, not a gray area:** the binaries are MIT-equivalent licensed,
redistribution alongside your own app is explicitly permitted, and commercial use is explicitly
permitted per voidtools' own [License.txt](http://www.voidtools.com/License.txt) and their forum
confirmations.

**Admin requirements — there's a no-admin path:**

| Mode | Needs admin? | Speed | Notes |
|---|---|---|---|
| NTFS fast indexing (reads the MFT directly) | Yes, once, to install a small persistent **Everything Service** (~1 MB RAM) | Seconds for millions of files | After the one-time service install, the GUI/CLI runs unprivileged forever |
| **Folder indexing** | **No** | Slower (walks the filesystem like Windows Search does — can take a couple minutes for a big drive) | Works on FAT32, network shares, and non-NTFS volumes too |

For a customer-facing installer, the clean pattern is: silently install the Everything Service once
(`Everything.exe -install-service`), then your Python app talks to `es.exe`/the IPC SDK unprivileged
from then on — no per-run UAC prompt.

```bat
:: Silent install, service mode, no desktop clutter
Everything-1.4.1.1032.Setup.exe /S /D="C:\Program Files\Everything" ^
  -install-options "-app-data -install-service -install-start-menu-shortcuts=0"
```

```python
import subprocess

def find_files(pattern: str, path: str = "D:\\"):
    # es.exe talks to the always-on Everything service over IPC; near-instant even
    # across millions of indexed files.
    result = subprocess.run(
        ["es.exe", "-path", path, pattern],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()
```

Official references: [Command Line Options](https://www.voidtools.com/support/everything/command_line_options/),
[Folder Indexing](https://www.voidtools.com/support/everything/folder_indexing/),
[Everything Service](https://www.voidtools.com/support/everything/everything_service/),
official [Python SDK example](https://www.voidtools.com/support/everything/sdk/python/) (ctypes,
no external deps), and the community wrapper
[flipeador/Python-EveryThing-SDK](https://github.com/flipeador/Python-EveryThing-SDK) if you'd
rather not hand-roll the ctypes bindings. There's also an official CLI source repo,
[voidtools/ES](https://github.com/voidtools/ES), if you want to build `es.exe` yourself rather than
take the prebuilt binary.

### 4.2 Rust/C++ MFT readers — for a fully custom, no-third-party-binary index

If bundling someone else's `.exe` isn't acceptable, the same trick Everything uses (reading the NTFS
Master File Table directly via `FSCTL_ENUM_USN_DATA`/`DeviceIoControl` instead of walking the
filesystem tree) is available as open-source building blocks:

- **[`ntfs-reader`](https://lib.rs/crates/ntfs-reader)** (Rust crate) — direct MFT + USN journal
  reader; a few lines to enumerate every file/folder on a volume in seconds. Needs admin for the raw
  volume handle (`\\.\D:`), same requirement Everything has for NTFS fast mode.
- **[`omerbenamram/mft`](https://github.com/omerbenamram/mft)** — mature Rust MFT parser with
  official **Python bindings** (`pip install mft`) if you want MFT-speed enumeration without writing
  any Rust yourself.
- **[Dicklesworthstone/ultrasearch](https://github.com/Dicklesworthstone/ultrasearch)** — a full
  reference architecture (Rust, MFT + USN + Tantivy full-text index, background service + short-lived
  worker processes) if you want to see how a modern from-scratch implementation is structured end to end.

The practical DIY pattern: a small compiled helper (Rust or C++) does `DeviceIoControl` MFT enumeration
once, dumps `path,size,mtime` rows into a SQLite file (with FTS5 if you want fuzzy/substring search),
and your Python app just queries SQLite — sub-second lookups over millions of rows, and you own the
whole stack. Rescans/incremental updates come from tailing the USN journal (`ntfs-reader` exposes this
directly) rather than periodic full rescans.

`fd` (the popular Rust `find` replacement) is **not** an indexer — it re-walks the filesystem on every
invocation, just very efficiently. It's a great "search right now" tool but doesn't give you the
"instant, pre-built index" property you asked about; include it only if a live walk of the drive at
query time is actually fast enough for your use case (small-to-medium drives, SSD).

---

## Recommendation matrix

| Your situation | Do this |
|---|---|
| You want it to genuinely be "in Windows Search" (Start menu search, Explorer search box all see it) | **Section 3** — CLSID-direct COM call, PowerShell or Python, from your installer, elevated |
| You're hitting `80040154` right now and just need it gone | **Section 0** table — 90% of the time it's the ProgID issue (Section 3 fixes it) or the feature being fully absent (DISM re-enable) |
| You want a CLI binary with zero COM/interop code in your app | **Section 3.4** — compile Microsoft's own `csmcmd` sample once, ship the `.exe` |
| You want the fastest possible "search this whole drive by name" and don't need it inside Windows' own Search UI | **Section 4.1** — bundle Everything, install the service silently once, talk to `es.exe`/IPC from Python |
| You can't bundle any third-party binary at all | **Section 4.2** — MFT-reader crate compiled to a small helper + SQLite, or `pip install mft` for a pure-Python MFT parse |
