"""The hourly scheduled task that runs MisteryUpdate.exe.

The one setting that is not a preference:

    <LogonType>InteractiveToken</LogonType>

Not "run whether the user is logged on or not". Mistery decides whether a second
copy of itself is already running by opening a mutex named
"Local\\Mistery-<user>" (main.py _claim_instance), and "Local\\" means *this
logon session*. A task registered to run whether or not anyone is logged on runs
in session 0, where that mutex does not exist and never will. Such an updater
would look at a machine with Mistery open on screen, see nothing, and start
replacing the exe underneath it.

InteractiveToken also means no password is stored anywhere, and the task simply
does not run while nobody is logged in — which is exactly right, because there
is no one there to be interrupted and StartWhenAvailable catches the missed run
at the next logon.

The rest is unremarkable and chosen for laptops: it runs on battery, it runs at
below-normal priority, it gives up after half an hour, and a second copy never
starts while the first is still going.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from xml.sax.saxutils import escape

from .setup_common import CREATE_NO_WINDOW, InstallError, run_quietly

SCHTASKS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "schtasks.exe"

# Runs an hour after the install and every hour after that, plus up to fifteen
# minutes of jitter. The jitter is for the server: every Mistery on earth asking
# the same small Railway instance at exactly :00 is a self-inflicted outage.
INTERVAL = "PT1H"
RANDOM_DELAY = "PT15M"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>Mistery</Author>
    <Description>Checks once an hour whether a newer Mistery has been released, and installs it while Mistery is closed. Remove this task and Mistery stops updating itself.</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>{interval}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <RandomDelay>{random_delay}</RandomDelay>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    """The account the task runs as, as a SID string.

    A SID and not DOMAIN\\name because the name is not stable: a Microsoft
    account shows up locally as a truncated five-character login, and a renamed
    account keeps its SID and loses its name. Task Scheduler resolves the SID
    back to a display name itself.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    # Every argtype spelled out: the pseudo-handle GetCurrentProcess returns is
    # -1 as a 64-bit value, and ctypes' default int conversion refuses it.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = ()
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(token)):
        return _user_from_environment()
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buffer,
                                            size, ctypes.byref(size)):
            return _user_from_environment()
        # TOKEN_USER is { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; } }
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        text = ctypes.c_wchar_p()
        advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_wchar_p))
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return _user_from_environment()
        try:
            return text.value or _user_from_environment()
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _user_from_environment() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    name = os.environ.get("USERNAME") or "user"
    return f"{domain}\\{name}" if domain else name


def build_xml(task_name: str, command: Path, start: str) -> str:
    """The task definition. `start` is a local ISO time, e.g. 2026-09-17T20:00:00.

    Everything substituted in goes through escape() first, because the install
    path ends up inside <Command> and the default install path contains the
    account name. Windows allows & in a folder name and in an account name, so
    "Tom & Jerry" is a path somebody really has: measured, that path produced
    XML that Expat rejects at line 47 column 29, schtasks refused the file, and
    the install finished with a note saying Mistery would never update itself —
    the one failure in this chain that cannot be fixed later from the server.
    """
    return TASK_XML.format(
        task_name=escape(task_name), start=escape(start), interval=INTERVAL,
        random_delay=RANDOM_DELAY, user=escape(current_user()),
        command=escape(str(command)), working_dir=escape(str(command.parent)))


def register(task_name: str, command: Path, start: str, xml_path: Path) -> None:
    """Create (or replace) the task. Raises InstallError with schtasks' own words.

    The XML file has to be UTF-16 with a byte order mark: schtasks /XML reads it
    as ANSI otherwise and rejects the file with an error that does not say so.
    """
    if not command.is_file():
        raise InstallError(
            f"{command.name} is not in the install folder, so there is nothing "
            "for the update task to run.")
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(build_xml(task_name, command, start), encoding="utf-16")
    try:
        result = run_quietly([str(SCHTASKS), "/Create", "/TN", task_name,
                              "/XML", str(xml_path), "/F"], timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError(f"could not run schtasks: {error}") from error
    finally:
        xml_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise InstallError(
            "Windows would not register the hourly update task:\n"
            + (result.stderr or result.stdout or "").strip()[:400]
            + "\n\nMistery is installed and works; it just will not update "
              "itself. You can install updates by downloading them again.")


def exists(task_name: str) -> bool:
    try:
        result = run_quietly([str(SCHTASKS), "/Query", "/TN", task_name], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def describe(task_name: str) -> str:
    """The task as Task Scheduler sees it, for the tests to read back."""
    try:
        result = subprocess.run([str(SCHTASKS), "/Query", "/TN", task_name, "/XML"],
                                capture_output=True, timeout=30,
                                creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        return f"(schtasks failed: {error})"
    if result.returncode != 0:
        return f"(no task named {task_name})"
    return result.stdout.decode("utf-16", errors="replace") \
        if result.stdout[:2] == b"\xff\xfe" else result.stdout.decode(errors="replace")


def unregister(task_name: str) -> bool:
    """Delete the task. True if it is gone afterwards, either way."""
    try:
        run_quietly([str(SCHTASKS), "/Delete", "/TN", task_name, "/F"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return not exists(task_name)
