import sys
import os
import threading
import time
import ctypes
import webbrowser
import urllib.request
import uvicorn

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

FROZEN = getattr(sys, "frozen", False)

SERVICE_NAME = "RentAsstMiddlewareService"
DISPLAY_NAME = "RentAsst Standalone Middleware Service"
SERVICE_DESC = "High-performance integration gateway for RentAsst, Tally Prime, and external ERPs."
DASHBOARD_URL = "http://127.0.0.1:8088/login"
UNINSTALL_SHORTCUT_NAME = "Uninstall RentAsst Middleware.lnk"

if getattr(sys, "frozen", False):
    app_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))

if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from app.main import app


if WIN32_AVAILABLE:
    class RentAsstMiddlewareService(win32serviceutil.ServiceFramework):
        _svc_name_ = "RentAsstMiddlewareService"
        _svc_display_name_ = "RentAsst Standalone Middleware Service"
        _svc_description_ = "High-performance integration gateway for RentAsst, Tally Prime, and external ERPs."

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.server:
                # Ask uvicorn's own event loop to shut down gracefully instead of
                # relying on the process being force-killed by SCM after a timeout.
                self.server.should_exit = True
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            # Localhost only — see the same note in run.py.
            # log_config=None: Windows Services run with no attached console, so
            # sys.stdout is None. Uvicorn's default logging setup calls
            # sys.stdout.isatty() while configuring its colorized formatter, which
            # raises and aborts startup entirely (SCM reports a service-specific
            # error and the process exits before ever binding the port).
            config = uvicorn.Config(app, host="127.0.0.1", port=8088, log_level="info", log_config=None)
            self.server = uvicorn.Server(config)

            thread = threading.Thread(target=self.server.run, daemon=True)
            thread.start()

            # Report RUNNING only once uvicorn has actually started, so SCM doesn't
            # time out waiting for a status update that never came.
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            thread.join(timeout=15)


def _run_standalone():
    print("Starting RentAsst Middleware in standalone mode...")
    # Localhost only — see the same note in run.py.
    uvicorn.run(app, host="127.0.0.1", port=8088, reload=False)


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_elevated(arg: str) -> None:
    """
    Re-launches this same exe with `arg`, triggering the UAC elevation prompt
    (the "runas" verb), and returns without waiting: the elevated child does
    the actual install/uninstall work and prints/pauses on its own console.
    """
    print("Elevating privileges to Administrator...")
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, arg, None, 1)
    if ret <= 32:
        print("Administrator privileges are required. Cancelled.")
        input("Press Enter to exit...")


def _service_exists() -> bool:
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        hsvc = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        win32service.CloseServiceHandle(hsvc)
        return True
    except Exception:
        return False
    finally:
        win32service.CloseServiceHandle(scm)


def _create_uninstall_shortcut() -> None:
    # Best-effort: a Start Menu shortcut is how a non-technical client user
    # uninstalls the service without opening a terminal. Never let a failure
    # here block the actual service install.
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        folder = shell.SpecialFolders("AllUsersPrograms")
        shortcut = shell.CreateShortCut(os.path.join(folder, UNINSTALL_SHORTCUT_NAME))
        shortcut.TargetPath = sys.executable
        shortcut.Arguments = "uninstall"
        shortcut.WorkingDirectory = os.path.dirname(sys.executable)
        shortcut.IconLocation = sys.executable
        shortcut.Description = "Remove the RentAsst <-> Tally Middleware Windows service"
        shortcut.Save()
    except Exception as ex:
        print(f"(Could not create the Start Menu uninstall shortcut: {ex})")


def _remove_uninstall_shortcut() -> None:
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        folder = shell.SpecialFolders("AllUsersPrograms")
        path = os.path.join(folder, UNINSTALL_SHORTCUT_NAME)
        if os.path.exists(path):
            os.remove(path)
    except Exception as ex:
        print(f"(Could not remove the Start Menu uninstall shortcut: {ex})")


def _ensure_started_and_open_dashboard() -> None:
    try:
        win32serviceutil.StartService(SERVICE_NAME)
        print(f"Service {SERVICE_NAME} started.")
    except Exception as ex:
        print(f"(start: {ex})")

    # Start-Service-equivalent returns as soon as SCM marks the service RUNNING,
    # which happens before uvicorn's own async startup finishes binding the port
    # (see SvcDoRun above) — poll the liveness probe instead of opening the
    # browser immediately against a port that isn't listening yet.
    print("Waiting for the middleware to come online...")
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8088/health/live", timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(1)

    if ready:
        print("Opening the middleware dashboard in your browser...")
        webbrowser.open(DASHBOARD_URL)
    else:
        print(f"The service didn't respond within 30 seconds. Open {DASHBOARD_URL} manually once it's ready.")


def _cmd_install() -> None:
    if not _is_admin():
        _relaunch_elevated("install")
        return

    if _service_exists():
        print(f"Service {SERVICE_NAME} is already installed.")
        _ensure_started_and_open_dashboard()
        input("Press Enter to exit...")
        return

    print("=======================================================")
    print("  RentAsst Middleware Windows Service Installer")
    print("=======================================================")

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
    try:
        # The compiled exe correctly responds to the Service Control Manager's
        # dispatch protocol (SvcDoRun above hands off to servicemanager when
        # launched with no arguments, exactly how SCM launches a registered
        # binary), so it can be registered directly as the service binary —
        # unlike pywin32's own HandleCommandLine('install'), which assumes a
        # python.exe-hosted service and would register the wrong binary path here.
        hsvc = win32service.CreateService(
            scm, SERVICE_NAME, DISPLAY_NAME,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            f'"{sys.executable}"',
            None, 0, None, None, None,
        )
        try:
            win32service.ChangeServiceConfig2(hsvc, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESC)
            win32service.ChangeServiceConfig2(hsvc, win32service.SERVICE_CONFIG_FAILURE_ACTIONS, {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": [(win32service.SC_ACTION_RESTART, 10000)] * 3,
            })
        finally:
            win32service.CloseServiceHandle(hsvc)
    finally:
        win32service.CloseServiceHandle(scm)

    print(f"Service {SERVICE_NAME} installed successfully.")
    _create_uninstall_shortcut()
    _ensure_started_and_open_dashboard()
    input("Press Enter to exit...")


def _cmd_uninstall() -> None:
    if not _is_admin():
        _relaunch_elevated("uninstall")
        return

    if not _service_exists():
        print(f"Service {SERVICE_NAME} is not installed.")
    else:
        print(f"Stopping service {SERVICE_NAME}...")
        try:
            win32serviceutil.StopService(SERVICE_NAME)
        except Exception:
            pass
        print(f"Removing service {SERVICE_NAME}...")
        win32serviceutil.RemoveService(SERVICE_NAME)
        print(f"Service {SERVICE_NAME} removed successfully.")

    _remove_uninstall_shortcut()
    input("Press Enter to exit...")


if __name__ == "__main__":
    if FROZEN and len(sys.argv) > 1 and sys.argv[1].lower() in ("install", "uninstall"):
        # The client-facing entry points: `RentalMiddleware.exe install` (also what
        # a bare double-click falls through to below) and `... uninstall` (also
        # what the Start Menu shortcut runs). Both self-elevate as needed.
        if sys.argv[1].lower() == "install":
            _cmd_install()
        else:
            _cmd_uninstall()
    elif WIN32_AVAILABLE:
        if len(sys.argv) == 1:
            # No arguments is exactly how the Service Control Manager launches a
            # registered service binary — hand off to pywin32's dispatcher so it can
            # correctly report status back to SCM. If that fails, this process
            # wasn't actually started by SCM (double-clicked directly instead): for
            # the compiled exe that means "install (or launch) the service"; from
            # source, keep the old dev convenience of just running in the foreground.
            try:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(RentAsstMiddlewareService)
                servicemanager.StartServiceCtrlDispatcher()
            except Exception:
                if FROZEN:
                    _cmd_install()
                else:
                    _run_standalone()
        else:
            # e.g. `service.py install|start|stop|remove` from source — pywin32's
            # own CLI handler, which correctly registers a python.exe-hosted service.
            win32serviceutil.HandleCommandLine(RentAsstMiddlewareService)
    else:
        _run_standalone()

