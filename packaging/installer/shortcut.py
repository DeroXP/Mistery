"""Writing a .lnk, stamped with Mistery's AppUserModelID.

The stamp is the whole reason this is not four lines of WScript.Shell. Mistery
calls SetCurrentProcessExplicitAppUserModelID("Mistery.Player.1") on itself at
startup (main.py:37). A shortcut carrying the same string is, as far as the
taskbar is concerned, the same application as the running window: one button,
and a tile the person pinned keeps working after an update replaces Mistery.exe.
A shortcut without it gets its own second button and the pin goes dead.

    That string must never change. Not the capitalisation, not the trailing
    ".1". Everything pinned to anybody's taskbar is keyed on it.

This is the same thing tools/install_shortcut.ps1 does with Add-Type and inline
C#, done with ctypes instead, because MisterySetup.exe cannot count on
PowerShell being allowed to run a script on somebody else's PC.
"""

from __future__ import annotations

import ctypes
from ctypes import (HRESULT, POINTER, byref, c_int, c_uint, c_void_p,
                    c_wchar_p)
from ctypes import wintypes
from pathlib import Path

from .setup_common import APP_ID, InstallError

ole32 = ctypes.WinDLL("ole32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str) -> None:
        super().__init__()
        if ole32.CLSIDFromString(c_wchar_p(text), byref(self)) != 0:
            raise InstallError(f"bad GUID {text}")


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    """24 bytes on x64. The union has to be reserved in full or SetValue writes
    past the end of the struct and corrupts whatever ctypes put after it."""
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT),
                ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                ("pointer", c_void_p), ("tail", c_void_p)]


CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"

# PKEY_AppUserModel_ID, straight out of propkey.h.
PKEY_AppUserModel_ID_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PKEY_AppUserModel_ID_PID = 5
VT_LPWSTR = 31

CLSCTX_INPROC_SERVER = 1
STGM_READWRITE = 2

# The slot numbers are the interface's vtable order; they are fixed by the ABI
# and are the one thing here that cannot be looked up at runtime.
_RELEASE = 2
_QUERY_INTERFACE = 0
_SHELLLINK_SET_DESCRIPTION = 7
_SHELLLINK_SET_WORKING_DIRECTORY = 9
_SHELLLINK_SET_ARGUMENTS = 11
_SHELLLINK_SET_SHOW_CMD = 15
_SHELLLINK_SET_ICON_LOCATION = 17
_SHELLLINK_SET_PATH = 20
_PERSISTFILE_SAVE = 6
_PROPERTYSTORE_SET_VALUE = 6
_PROPERTYSTORE_COMMIT = 7


def _call(interface: c_void_p, slot: int, restype, *argtypes):
    """A bound method out of a COM vtable: table[slot], typed and callable."""
    vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return lambda *args: prototype(vtable[slot])(interface, *args)


def _ok(result: int, what: str) -> None:
    if result != 0:
        raise InstallError(f"{what} failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")


def write_shortcut(link_path: Path, target: Path, *, arguments: str = "",
                   working_dir: Path | None = None, icon: Path | None = None,
                   description: str = "", app_id: str = APP_ID) -> Path:
    """Create or replace one .lnk. Returns the path it wrote."""
    link_path.parent.mkdir(parents=True, exist_ok=True)

    # COINIT_APARTMENTTHREADED. The shell's link object wants an STA, and the
    # installer's worker thread has not initialised COM for itself.
    hresult = ole32.CoInitializeEx(None, 2)
    needs_uninit = hresult in (0, 1)         # S_OK, S_FALSE (already in one)
    try:
        link = c_void_p()
        _ok(ole32.CoCreateInstance(byref(GUID(CLSID_ShellLink)), None,
                                   CLSCTX_INPROC_SERVER,
                                   byref(GUID(IID_IShellLinkW)), byref(link)),
            "CoCreateInstance(ShellLink)")
        try:
            _ok(_call(link, _SHELLLINK_SET_PATH, HRESULT, c_wchar_p)(
                str(target)), "IShellLink::SetPath")
            if arguments:
                _ok(_call(link, _SHELLLINK_SET_ARGUMENTS, HRESULT,
                          c_wchar_p)(arguments), "IShellLink::SetArguments")
            if working_dir is not None:
                _ok(_call(link, _SHELLLINK_SET_WORKING_DIRECTORY,
                          HRESULT, c_wchar_p)(str(working_dir)),
                    "IShellLink::SetWorkingDirectory")
            if icon is not None:
                _ok(_call(link, _SHELLLINK_SET_ICON_LOCATION, HRESULT,
                          c_wchar_p, c_int)(str(icon), 0),
                    "IShellLink::SetIconLocation")
            if description:
                _ok(_call(link, _SHELLLINK_SET_DESCRIPTION, HRESULT,
                          c_wchar_p)(description), "IShellLink::SetDescription")
            _ok(_call(link, _SHELLLINK_SET_SHOW_CMD, HRESULT, c_int)(1),
                "IShellLink::SetShowCmd")       # SW_SHOWNORMAL

            if app_id:
                _stamp_app_id(link, app_id)

            persist = c_void_p()
            _ok(_call(link, _QUERY_INTERFACE, HRESULT,
                      POINTER(GUID), POINTER(c_void_p))(
                          byref(GUID(IID_IPersistFile)), byref(persist)),
                "QueryInterface(IPersistFile)")
            try:
                _ok(_call(persist, _PERSISTFILE_SAVE, HRESULT,
                          c_wchar_p, wintypes.BOOL)(str(link_path), True),
                    f"IPersistFile::Save({link_path})")
            finally:
                _call(persist, _RELEASE, c_uint)()
        finally:
            _call(link, _RELEASE, c_uint)()
    finally:
        if needs_uninit:
            ole32.CoUninitialize()
    return link_path


def _stamp_app_id(link: c_void_p, app_id: str) -> None:
    """Set PKEY_AppUserModel_ID on the link's property store and commit it."""
    store = c_void_p()
    _ok(_call(link, _QUERY_INTERFACE, HRESULT,
              POINTER(GUID), POINTER(c_void_p))(
                  byref(GUID(IID_IPropertyStore)), byref(store)),
        "QueryInterface(IPropertyStore)")
    try:
        key = PROPERTYKEY()
        key.fmtid = GUID(PKEY_AppUserModel_ID_FMTID)
        key.pid = PKEY_AppUserModel_ID_PID

        # A VT_LPWSTR PROPVARIANT is a tag and a CoTaskMem string, so it is
        # built by hand rather than pulling in propsys!InitPropVariantFromString.
        text = ctypes.create_unicode_buffer(app_id)
        size = ctypes.sizeof(text)
        ole32.CoTaskMemAlloc.restype = c_void_p
        buffer = ole32.CoTaskMemAlloc(size)
        if not buffer:
            raise InstallError("out of memory setting the shortcut's app id")
        ctypes.memmove(buffer, text, size)

        value = PROPVARIANT()
        value.vt = VT_LPWSTR
        value.pointer = buffer
        try:
            _ok(_call(store, _PROPERTYSTORE_SET_VALUE, HRESULT,
                      POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
                          byref(key), byref(value)),
                "IPropertyStore::SetValue(AppUserModelID)")
            _ok(_call(store, _PROPERTYSTORE_COMMIT, HRESULT)(),
                "IPropertyStore::Commit")
        finally:
            ole32.PropVariantClear(byref(value))     # frees the string
    finally:
        _call(store, _RELEASE, c_uint)()


# --- reading one back, which is how the tests check the install -------------


def read_shortcut(link_path: Path) -> dict[str, str]:
    """Target, arguments, working directory and app id out of an existing .lnk.

    Only the installer's tests call this, but they need it: "the shortcut file
    exists" proves nothing, and a shortcut pointing at the wrong exe or missing
    its app id is exactly the kind of thing that looks fine until somebody tries
    to pin it.
    """
    hresult = ole32.CoInitializeEx(None, 2)
    needs_uninit = hresult in (0, 1)
    try:
        link = c_void_p()
        _ok(ole32.CoCreateInstance(byref(GUID(CLSID_ShellLink)), None,
                                   CLSCTX_INPROC_SERVER,
                                   byref(GUID(IID_IShellLinkW)), byref(link)),
            "CoCreateInstance(ShellLink)")
        try:
            persist = c_void_p()
            _ok(_call(link, _QUERY_INTERFACE, HRESULT,
                      POINTER(GUID), POINTER(c_void_p))(
                          byref(GUID(IID_IPersistFile)), byref(persist)),
                "QueryInterface(IPersistFile)")
            try:
                _ok(_call(persist, 5, HRESULT, c_wchar_p, wintypes.DWORD)(
                    str(link_path), STGM_READWRITE), f"IPersistFile::Load({link_path})")
            finally:
                _call(persist, _RELEASE, c_uint)()

            out: dict[str, str] = {}
            buffer = ctypes.create_unicode_buffer(1024)
            # GetPath(pszFile, cch, WIN32_FIND_DATA*, flags) — SLGP_RAWPATH(4)
            # so an unexpanded %VAR% comes back as written rather than resolved.
            _call(link, 3, HRESULT, c_wchar_p, c_int, c_void_p, c_uint)(
                buffer, 1024, None, 4)
            out["target"] = buffer.value
            _call(link, 10, HRESULT, c_wchar_p, c_int)(buffer, 1024)
            out["arguments"] = buffer.value
            _call(link, 8, HRESULT, c_wchar_p, c_int)(buffer, 1024)
            out["working_dir"] = buffer.value
            icon_index = c_int()
            _call(link, 16, HRESULT, c_wchar_p, c_int, POINTER(c_int))(
                buffer, 1024, byref(icon_index))
            out["icon"] = buffer.value

            store = c_void_p()
            if _call(link, _QUERY_INTERFACE, HRESULT,
                     POINTER(GUID), POINTER(c_void_p))(
                         byref(GUID(IID_IPropertyStore)), byref(store)) == 0:
                try:
                    key = PROPERTYKEY()
                    key.fmtid = GUID(PKEY_AppUserModel_ID_FMTID)
                    key.pid = PKEY_AppUserModel_ID_PID
                    value = PROPVARIANT()
                    if _call(store, 5, HRESULT, POINTER(PROPERTYKEY),
                             POINTER(PROPVARIANT))(byref(key), byref(value)) == 0:
                        if value.vt == VT_LPWSTR and value.pointer:
                            out["app_id"] = ctypes.wstring_at(value.pointer)
                        ole32.PropVariantClear(byref(value))
                finally:
                    _call(store, _RELEASE, c_uint)()
            return out
        finally:
            _call(link, _RELEASE, c_uint)()
    finally:
        if needs_uninit:
            ole32.CoUninitialize()
