# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance corrections, and employee and department analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Dependencies listed in `requirements.txt`

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` through your local environment. Do not commit credentials or secrets.
The API expects the `employees` and `attendance_logs` collections to be available in the configured database. The sample seed script replaces existing collection contents, so do not run it against a database containing data you want to preserve.

## Run the API

```bash
python -m uvicorn app.main:app --port 8000
```

Open the interactive API documentation at:

`http://127.0.0.1:8000/docs`

## Implementation

All application code is in `app/main.py`. MongoDB indexes enforce uniqueness and support common queries. See `REVIEW.md` for the starter-code review and `DECISIONS.md` for implementation decisions.