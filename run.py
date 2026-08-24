import uvicorn

if __name__ == "__main__":
    # reload=True is a development-only feature (file-watcher + auto-restart) and must
    # never run in a deployed/unattended service — this is the entry point NSSM and
    # start.bat/start.ps1 wrap directly. For local development, run
    # `uvicorn app.main:app --reload` from the command line instead.
    # Bound to localhost only — this deployment runs on the same machine as Tally and
    # the dashboard's browser, with no other machine on the network needing access. If
    # a future deployment needs LAN-wide reachability, change this back to "0.0.0.0"
    # and scope access with a firewall rule instead (the API key alone isn't a
    # substitute for network-level restriction).
    uvicorn.run("app.main:app", host="127.0.0.1", port=8088, reload=False)
