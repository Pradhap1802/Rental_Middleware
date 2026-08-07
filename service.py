import sys
import os
import time
import uvicorn

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False


if WIN32_AVAILABLE:
    class RentAsstMiddlewareService(win32serviceutil.ServiceFramework):
        _svc_name_ = "RentAsstMiddlewareService"
        _svc_display_name_ = "RentAsst Standalone Middleware Service"
        _svc_description_ = "High-performance integration gateway for RentAsst, Tally Prime, and external ERPs."

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            app_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, app_dir)
            uvicorn.run("app.main:app", host="0.0.0.0", port=8088, log_level="info")

if __name__ == "__main__":
    if WIN32_AVAILABLE and len(sys.argv) > 1:
        win32serviceutil.HandleCommandLine(RentAsstMiddlewareService)
    else:
        print("Starting RentAsst Middleware in standalone mode...")
        uvicorn.run("app.main:app", host="0.0.0.0", port=8088, reload=True)
