import uvicorn

if __name__ == "__main__":
    # reload=True is a development-only feature (file-watcher + auto-restart) and must
    # never run in a deployed/unattended service — this is the entry point NSSM and
    # start.bat/start.ps1 wrap directly. For local development, run
    # `uvicorn app.main:app --reload` from the command line instead.
    uvicorn.run("app.main:app", host="0.0.0.0", port=8088, reload=False)
