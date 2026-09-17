<#
.SYNOPSIS
    Install Mistery into the Start Menu (and optionally the Desktop).

.DESCRIPTION
    ASCII only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI unless the
    file has a BOM, so any smart punctuation here becomes a parse error.

    Creates a shortcut that launches Mistery with pythonw.exe, so there is no
    console window, and stamps it with the same AppUserModelID the app sets on
    itself. Matching IDs keep a pinned shortcut and the running window as one
    taskbar button instead of two.

    Windows 11 has no supported way to pin to Start programmatically, so after
    running this, right-click Mistery in the Start Menu and choose "Pin to Start".

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1
    powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1 -Desktop
    powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1 -Uninstall
#>
param(
    [switch]$Desktop,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$AppId     = 'Mistery.Player.1'
$Root      = Split-Path -Parent $PSScriptRoot
$MainPy    = Join-Path $Root 'main.py'
$IconFile  = Join-Path $Root 'assets\icon.ico'
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$LinkPath  = Join-Path $StartMenu 'Mistery.lnk'
$DeskPath  = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Mistery.lnk'

if ($Uninstall) {
    foreach ($p in @($LinkPath, $DeskPath)) {
        if (Test-Path $p) { Remove-Item $p -Force; "removed $p" }
    }
    return
}

if (-not (Test-Path $MainPy))   { throw "main.py not found at $MainPy" }
if (-not (Test-Path $IconFile)) { throw "icon not found - run: python tools\make_icon.py" }

# pythonw.exe runs the GUI without a console window.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if ($python) { $pythonw = Join-Path (Split-Path $python) 'pythonw.exe' }
}
if (-not $pythonw -or -not (Test-Path $pythonw)) { throw 'pythonw.exe not found on PATH' }

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($LinkPath)
$link.TargetPath       = $pythonw
$link.Arguments        = '"{0}"' -f $MainPy
$link.WorkingDirectory = $Root
$link.IconLocation     = '{0},0' -f $IconFile
$link.Description      = 'Mistery - local movie and TV library'
$link.WindowStyle      = 1
$link.Save()
"created $LinkPath"

# --- stamp the shortcut with the app's AppUserModelID -----------------------
$interop = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace MisteryShell {

  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey {
    public Guid fmtid; public uint pid;
    public PropertyKey(Guid g, uint p) { fmtid = g; pid = p; }
  }

  // PROPVARIANT is 24 bytes on x64 (16 on x86). The union must be reserved in
  // full or GetValue writes past the end of the struct.
  [StructLayout(LayoutKind.Explicit)]
  public struct PropVariant {
    [FieldOffset(0)]  public ushort vt;
    [FieldOffset(2)]  public ushort wReserved1;
    [FieldOffset(4)]  public ushort wReserved2;
    [FieldOffset(6)]  public ushort wReserved3;
    [FieldOffset(8)]  public IntPtr pointerValue;
    [FieldOffset(16)] public IntPtr unionTail;
  }

  [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    void GetCount(out uint c);
    void GetAt(uint i, out PropertyKey key);
    void GetValue(ref PropertyKey key, out PropVariant pv);
    void SetValue(ref PropertyKey key, ref PropVariant pv);
    void Commit();
  }

  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  public class CShellLink { }

  [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPersistFile {
    void GetClassID(out Guid id);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string file, uint mode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string file,
              [MarshalAs(UnmanagedType.Bool)] bool remember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string file);
    void GetCurFile([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file);
  }

  public static class AppId {
    static readonly Guid Fmt = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");

    const ushort VT_LPWSTR = 31;

    [DllImport("ole32.dll")]
    static extern int PropVariantClear(ref PropVariant pv);

    // A VT_LPWSTR PROPVARIANT is just a tag plus a CoTaskMem string, so we
    // build it by hand rather than depend on propsys!InitPropVariantFromString.
    public static void Set(string linkPath, string appId) {
      object link = new CShellLink();
      ((IPersistFile)link).Load(linkPath, 2 /* STGM_READWRITE */);
      PropertyKey key = new PropertyKey(Fmt, 5);
      PropVariant pv = new PropVariant();
      pv.vt = VT_LPWSTR;
      pv.pointerValue = Marshal.StringToCoTaskMemUni(appId);
      IPropertyStore store = (IPropertyStore)link;
      store.SetValue(ref key, ref pv);
      store.Commit();
      ((IPersistFile)link).Save(linkPath, true);
      PropVariantClear(ref pv);          // frees the string we allocated
      Marshal.ReleaseComObject(link);
    }

    public static string Get(string linkPath) {
      object link = new CShellLink();
      ((IPersistFile)link).Load(linkPath, 0 /* STGM_READ */);
      PropertyKey key = new PropertyKey(Fmt, 5);
      PropVariant pv;
      ((IPropertyStore)link).GetValue(ref key, out pv);
      string result = pv.vt == VT_LPWSTR ? Marshal.PtrToStringUni(pv.pointerValue) : null;
      PropVariantClear(ref pv);
      Marshal.ReleaseComObject(link);
      return result;
    }
  }
}
'@

try {
    if (-not ('MisteryShell.AppId' -as [type])) {
        Add-Type -TypeDefinition $interop -Language CSharp | Out-Null
    }
    [MisteryShell.AppId]::Set($LinkPath, $AppId)
    $readBack = [MisteryShell.AppId]::Get($LinkPath)
    if ($readBack -eq $AppId) { "AppUserModelID set to $readBack" }
    else { "warning: AppUserModelID read back as '$readBack'" }
} catch {
    "note: could not set AppUserModelID ($($_.Exception.Message.Trim()))"
    "      the shortcut still works; taskbar pinning may show a second button"
}

if ($Desktop) {
    Copy-Item $LinkPath $DeskPath -Force
    "created $DeskPath"
}

""
"Mistery is now in the Start Menu."
"To pin it: press Start, type 'Mistery', right-click the result, choose 'Pin to Start'."
"(Windows 11 has no supported way to do that pin from a script.)"
