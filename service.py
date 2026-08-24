import sys
import os
import threading
import uvicorn

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

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
            config = uvicorn.Config(app, host="127.0.0.1", port=8088, log_level="info")
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


if __name__ == "__main__":
    if WIN32_AVAILABLE:
        if len(sys.argv) == 1:
            # No arguments is exactly how the Service Control Manager launches a
            # registered service binary — hand off to pywin32's dispatcher so it can
            # correctly report status back to SCM. If that fails (e.g. this process
            # wasn't actually started by SCM — double-clicked the exe directly, or
            # invoked as a plain script), fall back to running the app directly.
            try:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(RentAsstMiddlewareService)
                servicemanager.StartServiceCtrlDispatcher()
            except Exception:
                _run_standalone()
        else:
            # e.g. `service.py install|start|stop|remove` — pywin32's own CLI handler.
            win32serviceutil.HandleCommandLine(RentAsstMiddlewareService)
    else:
        _run_standalone()

