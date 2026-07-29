"""Convenience launcher for the web app.

Run it from anywhere; it changes into the project directory first (so relative
files like web_credentials.json / credentials.json / app.db resolve correctly)
and then starts the FastAPI server on http://127.0.0.1:8000.

    .venv\\Scripts\\python.exe run_server.py

Reload is intentionally OFF: a triage runs as a long background task, and
auto-reload would restart the server mid-run (interrupting/freezing it) the
moment any watched file changes. Restart manually when you change code.
"""

import os

import uvicorn

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
