"""Windows GUI automation mediated by policy.

Enforces:
1. Session check: AT10 check for desktop locked / unavailable (never fakes success).
2. Concurrency: exactly one active GUI job at any time via file lock.
3. Policy: only registered, non-elevated applications; no admin/elevated execution.
4. Screenshots: captures 'before' and 'after' state images locally.
5. Benign observable interactions only.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple


class GuiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GuiSessionState:
    is_interactive: bool
    details: str


ALLOWED_APPS = {
    "notepad": {
        "executable": "notepad.exe",
        "description": "Standard Windows Notepad",
        "allowed_actions": ["open_and_inspect", "write_test_note", "close"],
    },
    "calc": {
        "executable": "calc.exe",
        "description": "Standard Windows Calculator",
        "allowed_actions": ["open_and_inspect", "close"],
    }
}

BANNED_APPS = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "wsl.exe",
    "regedit.exe", "mmc.exe", "taskmgr.exe", "net.exe", "sc.exe"
}


class GuiAutomationManager:
    def __init__(
        self,
        screenshots_dir: Path | None = None,
        lock_path: Path | None = None,
        job_store: Any | None = None,
    ):
        self.job_store = job_store
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        base_dir = state_home / "jarvis-hermes"
        self.screenshots_dir = screenshots_dir or (base_dir / "screenshots")
        self.lock_path = lock_path or (base_dir / "gui_job.lock")
        self.screenshots_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)

    def _get_powershell_cmd(self) -> list[str] | None:
        """Find powershell on Windows or WSL."""
        if os.name == "nt":
            if shutil.which("powershell.exe"):
                return ["powershell.exe", "-NoProfile", "-NonInteractive"]
        else:
            wsl_ps = shutil.which("powershell.exe") or "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"
            if Path(wsl_ps).exists():
                return [str(wsl_ps), "-NoProfile", "-NonInteractive"]
        return None

    def check_desktop_interactive(self) -> Tuple[bool, str]:
        """Check whether Windows GUI desktop session is interactive (OpenInputDesktop)."""
        ps = self._get_powershell_cmd()
        if not ps:
            return False, "NO_WINDOWS_SESSION"

        script = """
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class WinCheck {
    [DllImport("user32.dll")]
    public static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll")]
    public static extern bool CloseDesktop(IntPtr hDesktop);
}
'@
$desk = [WinCheck]::OpenInputDesktop(0, $false, 0x0100)
if ($desk -eq [IntPtr]::Zero) {
    Write-Output 'DESKTOP_LOCKED'
} else {
    [WinCheck]::CloseDesktop($desk)
    Write-Output 'DESKTOP_INTERACTIVE'
}
"""
        try:
            res = subprocess.run(
                [*ps, "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = res.stdout.strip()
            if "DESKTOP_INTERACTIVE" in out:
                return True, "DESKTOP_INTERACTIVE"
            return False, "DESKTOP_LOCKED"
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT_CHECKING_DESKTOP"
        except Exception as e:
            return False, f"ERROR_CHECKING_DESKTOP: {e}"

    def _try_acquire_lock(self) -> bool:
        """Acquire single concurrency lock for GUI operations."""
        try:
            # Atomic file creation
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
            return True
        except FileExistsError:
            # Check if stale lock (older than 5 minutes)
            try:
                mtime = self.lock_path.stat().st_mtime
                if time.time() - mtime > 300:
                    self.lock_path.unlink(missing_ok=True)
                    return self._try_acquire_lock()
            except OSError:
                pass
            return False

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _take_screenshot(self, prefix: str = "shot") -> str:
        """Take screenshot of primary screen and save to screenshots_dir."""
        ps = self._get_powershell_cmd()
        if not ps:
            raise GuiError("DESKTOP_UNAVAILABLE", "Cannot take screenshot: Windows desktop not reachable.")

        ts = int(time.time() * 1000)
        filename = f"{prefix}_{ts}.png"
        filepath = self.screenshots_dir / filename

        # If running from WSL, translate path to Windows path
        win_path = str(filepath)
        if os.name != "nt":
            # Translate /home/... to wsl path for Windows or wslpath
            try:
                res = subprocess.run(["wslpath", "-w", str(filepath)], capture_output=True, text=True, check=True)
                win_path = res.stdout.strip()
            except Exception:
                pass

        script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('{win_path}', [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
"""
        res = subprocess.run([*ps, "-Command", script], capture_output=True, text=True, timeout=15)
        if res.returncode != 0 or not filepath.exists():
            raise GuiError("SCREENSHOT_FAILED", f"Screenshot failed: {res.stderr.strip()}")
        return str(filepath)

    def _get_active_window_info(self) -> dict[str, Any]:
        ps = self._get_powershell_cmd()
        if not ps:
            return {"title": "unknown", "process": "unknown"}

        script = """
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class WinUtil {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
}
'@
$hwnd = [WinUtil]::GetForegroundWindow()
$sb = New-Object System.Text.StringBuilder 256
[void][WinUtil]::GetWindowText($hwnd, $sb, 256)
$pid = 0
[void][WinUtil]::GetWindowThreadProcessId($hwnd, [ref]$pid)
$proc = (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName
Write-Output "$($sb.ToString())|||$pid|||$proc"
"""
        try:
            res = subprocess.run([*ps, "-Command", script], capture_output=True, text=True, timeout=5)
            parts = res.stdout.strip().split("|||")
            return {
                "title": parts[0] if len(parts) > 0 else "unknown",
                "pid": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
                "process": parts[2] if len(parts) > 2 else "unknown",
            }
        except Exception:
            return {"title": "unknown", "pid": 0, "process": "unknown"}

    def _launch_and_interact(self, app_cfg: dict, action: str) -> dict[str, Any]:
        """Launch app and perform benign observable action."""
        ps = self._get_powershell_cmd()
        exe = app_cfg["executable"]

        # Run non-elevated app
        launch_script = f"""
$p = Start-Process -FilePath "{exe}" -PassThru
Start-Sleep -Seconds 1
Write-Output "$($p.Id)|||$($p.MainWindowTitle)"
"""
        res = subprocess.run([*ps, "-Command", launch_script], capture_output=True, text=True, timeout=10)
        parts = res.stdout.strip().split("|||")
        pid = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
        title = parts[1] if len(parts) > 1 else ""

        # Perform action
        observed = "window_opened_ready"
        if action == "close" and pid:
            subprocess.run([*ps, "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"])
            observed = "window_closed"

        return {
            "window_title": title,
            "process_id": pid,
            "action_performed": action,
            "observed_state": observed,
        }

    def get_gui_status(self) -> dict[str, Any]:
        """Inspect desktop session and active window."""
        interactive, detail = self.check_desktop_interactive()
        status: dict[str, Any] = {
            "session_state": "interactive" if interactive else "unavailable",
            "detail": detail,
        }
        if interactive:
            status["active_window"] = self._get_active_window_info()
        return status

    def execute_gui_action(
        self,
        app_name: str,
        action: str = "open_and_inspect",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a benign GUI action with before/after screenshots and concurrency lock."""
        if job_id and self.job_store:
            self.job_store.check_not_cancelled(job_id)

        # 1. Reject banned or non-whitelisted apps
        norm_app = app_name.lower().strip()
        if norm_app in BANNED_APPS or any(norm_app.endswith(b) for b in BANNED_APPS):
            raise GuiError("PERMISSION_DENIED", f"Execution of system/shell tool '{app_name}' is banned via GUI.")

        app_cfg = ALLOWED_APPS.get(norm_app)
        if not app_cfg:
            raise GuiError("PERMISSION_DENIED", f"Application '{app_name}' is not in allowed GUI applications list.")

        if action not in app_cfg["allowed_actions"]:
            raise GuiError("ACTION_NOT_PERMITTED", f"Action '{action}' is not permitted for '{app_name}'.")

        # 2. Check desktop interactive session (AT10)
        interactive, detail = self.check_desktop_interactive()
        if not interactive:
            raise GuiError("DESKTOP_UNAVAILABLE", f"Windows desktop session is not interactive: {detail}")

        # 3. Enforce single concurrent GUI job
        if not self._try_acquire_lock():
            raise GuiError("CONCURRENT_GUI_JOB", "Another GUI job is currently executing. Only one concurrent GUI job allowed.")

        try:
            # 4. Take before screenshot
            before_shot = self._take_screenshot("before")

            # 5. Launch & interact
            interaction = self._launch_and_interact(app_cfg, action)
            pid = interaction.get("process_id")
            if pid and job_id and self.job_store:
                try:
                    self.job_store.register_process(job_id=job_id, pid=pid, pgid=pid)
                except Exception:
                    # Cancelled in the meantime: close process immediately
                    ps = self._get_powershell_cmd()
                    if ps:
                        subprocess.run([*ps, "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"])
                    raise

            # Short wait for UI stabilization
            time.sleep(0.5)

            # 6. Take after screenshot
            after_shot = self._take_screenshot("after")

            return {
                "ok": True,
                "app_name": norm_app,
                "action": action,
                "screenshots": {
                    "before": before_shot,
                    "after": after_shot,
                },
                "interaction": interaction,
            }
        finally:
            self._release_lock()
