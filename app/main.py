"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
import re
from datetime import date
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
def utc_from_epoch_ms(punched_at: int) -> datetime:
    """Convert epoch milliseconds to a whole-second UTC datetime."""
    return datetime.fromtimestamp(
        punched_at // 1000, tz=timezone.utc
    ).replace(tzinfo=None)


def serialize_timestamps(value):
    """Convert UTC datetime values to epoch milliseconds for API responses."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return int(value.timestamp() * 1000)

    if isinstance(value, dict):
        return {
            key: serialize_timestamps(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [serialize_timestamps(item) for item in value]

    return value



def attendance_date_for(punch_in_utc: datetime, emp: dict) -> str:
    """Return the attendance date in IST, including overnight shifts."""
    punch_in_ist = punch_in_utc.replace(tzinfo=timezone.utc).astimezone(IST)

    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute) < (end_h, end_m):
        punch_in_ist -= timedelta(days=1)

    return punch_in_ist.date().isoformat()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



@app.on_event("startup")
def create_indexes():
    # Prevent duplicate employee codes, including concurrent requests.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_emp_code",
    )

    # Support department and joining-date queries.
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )

    # Allow only one attendance record per employee per date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )

    # Support attendance date filtering and sorting.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp",
    )

    # Support queries filtering employees by joining date alone.
    db.employees.create_index(
        [("joined_on", 1)],
        name="ix_employee_joined_on",
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: str = "18:30"
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start = punch_in.replace(
        hour=start_h, minute=start_m, second=0, microsecond=0
    )

    # A shift ending at or before its start time is overnight.
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    # Early-morning punches belong to the previous day's
    # shift only when that shift is overnight.
    if is_overnight and punch_in.time() < datetime.strptime(
        shift_end, "%H:%M"
    ).time():
        start -= timedelta(days=1)


    elapsed_seconds = (punch_in - start).total_seconds()

    if elapsed_seconds > 10 * 60:
        return int(elapsed_seconds // 60)

    return 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str = "09:30",
) -> int:
    end_h, end_m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))

    # Shift end is defined in IST on the attendance date.
    shift_end_ist = datetime.fromisoformat(date_str).replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST,
    )

    # If the shift crosses midnight, its end is on the next day.
    if (end_h, end_m) <= (start_h, start_m):
        shift_end_ist += timedelta(days=1)

    # MongoDB stores our timestamps as naive UTC datetimes.
    punch_out_ist = punch_out.replace(
        tzinfo=timezone.utc
    ).astimezone(IST)

    minutes = int(
        (punch_out_ist - shift_end_ist).total_seconds() // 60
    )

    # Overtime counts only when it reaches 30 complete minutes.
    return minutes if minutes >= 30 else 0
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError(
                "emp_code must be EMP followed by 4 to 6 digits"
            )
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(
            r"[^@\s]+@[^@\s]+\.[^@\s]+", value
        ):
            raise ValueError("Invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Shift time must use HH:MM format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value

    @model_validator(mode="after")
    def validate_shift_range(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendanceCorrectionIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1)
    regularized_by: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )



from pymongo.errors import DuplicateKeyError


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    # Convert created_at to epoch milliseconds.
    doc["created_at"] = int(
        doc["created_at"].timestamp() * 1000
    )

    # Never expose MongoDB's internal _id in the API response.
    doc.pop("_id", None)

    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for employee in items:
        created_at = employee.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            employee["created_at"] = int(
                created_at.timestamp() * 1000
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    if body.punched_at is not None:
        ts = utc_from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(
           microsecond=0, tzinfo=None
        )
    attendance_date = attendance_date_for(ts, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts.replace(tzinfo=timezone.utc).astimezone(IST),
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc.pop("_id", None)
    return serialize_timestamps(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    # Count only records matching the filters.
    total = db.attendance_logs.count_documents(q)

    # Let MongoDB sort and paginate instead of loading all records.
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = serialize_timestamps(list(cursor))
    return {
       "items": items,
       "total": total,
       "page": page,
       "page_size": page_size,
    }



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1)

    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)

    month_end = next_month - timedelta(days=1)
    joined_on = date.fromisoformat(employee["joined_on"])
    effective_start = max(month_start, joined_on)

    working_days = 0
    if effective_start <= month_end:
        current_day = effective_start
        while current_day <= month_end:
            if current_day.weekday() < 5:
                working_days += 1
            current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                   "$gte": max(month_start, joined_on).isoformat(),
                   "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lt": [
                                            {
                                                "$dateFromString": {
                                                    "dateString": "$date"
                                                }
                                            },
                                            {
                                                "$dateFromString": {
                                                    "dateString": next_month.isoformat()
                                                }
                                            },
                                        ]
                                    },

{
    "$and": [
        {
            "$ne": [
                {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                },
                1,
            ]
        },
        {
            "$ne": [
                {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                },
                7,
            ]
        },
    ]
}

                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
    }

    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(stats["present_days"])) / Decimal(working_days) * 100)
            .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": stats["present_days"],
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))
    month_start = date(year, month_number, 1)

    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)

    month_end = (next_month - timedelta(days=1)).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes":{"$addToSet":"$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$logs.date", None]},
                                            None,
                                        ]
                                    },
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$logs.date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7],
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid_hours"}, 0]},
                                {"$avg": "$$valid_hours"},
                                None,
                            ]
                        },
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    for row in rows:
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = float(
                Decimal(str(row["avg_work_hours"])).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )
    return {"month": month, "departments": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, month_number = map(int, month.split("-"))

    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)

    match_stage = {
        "date": {
            "$gte": f"{year:04d}-{month_number:02d}-01",
            "$lt": next_month.isoformat(),
        },
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {
                        "total_late_minutes": -1,
                    },
                    "output": {
                        "rank": {
                            "$rank": {},
                        }
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
        ]
    )

    results = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": f"{year:04d}-{month_number:02d}",
        "limit": limit,
        "leaderboard": results,
    }



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_date.isoformat() != from_date or end_date.isoformat() != to_date:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
    )

    pipeline = [
        # Seed the pipeline so it also works when there are no attendance logs.
        {"$documents": [{"date": start_dt}]},

        # Generate every date in the requested range inside MongoDB.
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "day_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                }
            }
        },

        # Find employees who have joined this department by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$day_string"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$current_day"]
                            },
                        }
                    }
                ],
                "as": "employees",
            }
        },

        # Retrieve attendance logs for those employees on that date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "current_day": "$day_string",
                    "employee_codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$current_day"]},
                                    {
                                        "$in": [
                                            "$emp_code",
                                            "$$employee_codes",
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$employees"},
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {"$dayOfWeek": "$date"},
                                [1, 7],
                            ]
                        }
                    ]
                },
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$$this.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        "$$this.half_day",
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                                                {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        # Average the current day and up to six preceding dates.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    trend_rows = list(db.aggregate(pipeline))
    for row in trend_rows:
        for field in ("attendance_rate", "moving_avg_7d"):
            value = row[field]
            if value is not None:
                row[field] = float(
                    Decimal(str(value)).quantize(
                        Decimal("0.0001"),
                        rounding=ROUND_HALF_UP,
                    )
                )

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "trend": trend_rows,
    }

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of {sorted(allowed)}",
        )

    def explain_aggregate(collection_name, pipeline):
        command = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }
        return db.command(
            "explain",
            command,
            verbosity="executionStats",
        )

    if endpoint == "attendance_list":
        match = {}
        if emp_code:
            match["emp_code"] = emp_code
        if status:
            match["status"] = status
        if date_from or date_to:
            match["date"] = {}
            if date_from:
                match["date"]["$gte"] = date_from
            if date_to:
                match["date"]["$lte"] = date_to
        pipeline = [{"$match": match}]
        pipeline.extend([
            {"$sort": {"date": -1, "emp_code": 1}},
            {"$skip": (page - 1) * page_size},
            {"$limit": page_size},
        ])
        result = explain_aggregate("attendance_logs", pipeline)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
            or not 1 <= int(month[:4]) <= 9999
            or not 1 <= int(month[5:]) <= 12
        ):
            raise HTTPException(
                status_code=422,
                detail="Invalid month; expected YYYY-MM",
            )

        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1)

        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        result = explain_aggregate("attendance_logs", pipeline)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        try:
            year, month_number = map(int, month.split("-"))
            month_start = date(year, month_number, 1)
            if month_number == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month_number + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid month")

        employee_match = {
            "joined_on": {"$lt": next_month.isoformat()}
        }
        if department:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {
                                            "$gte": [
                                                "$date",
                                                month_start.isoformat(),
                                            ]
                                        },
                                        {
                                            "$lt": [
                                                "$date",
                                                next_month.isoformat(),
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "emp_code": 1,
                    "logs": 1,
                }
            },
        ]
        result = explain_aggregate("employees", pipeline)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        try:
            year, month_number = map(int, month.split("-"))
            month_start = date(year, month_number, 1)
            if month_number == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month_number + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="Invalid month")

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )
        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ])
        result = explain_aggregate("attendance_logs", pipeline)

    else:  # department_trend
        # Accept both documented query names and Swagger field names.
        from_date = from_date or date_from
        to_date = to_date or date_to

        if not department:
            raise HTTPException(
                status_code=422,
                detail="department is required",
            )

        if not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="from and to are required",
            )

        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date")

        if (
            start_date.isoformat() != from_date
            or end_date.isoformat() != to_date
        ):
            raise HTTPException(status_code=422, detail="Invalid date format")

        if end_date < start_date or (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Invalid date range; maximum is 92 days",
            )

        if not db.employees.find_one({"department": department}):
            raise HTTPException(status_code=404, detail="Department not found")

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_exclusive = datetime.combine(
            end_date + timedelta(days=1),
            datetime.min.time(),
        )

        pipeline = [
            {"$documents": [{"date": start_dt}]},
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_exclusive],
                    },
                }
            },
            {
                "$set": {
                    "day_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                            "timezone": "UTC",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"current_day": "$day_string"},
                    "pipeline": [
                        {
                            "$match": {
                                "department": department,
                                "$expr": {
                                    "$lte": ["$joined_on", "$$current_day"]
                                },
                            }
                        }
                    ],
                    "as": "employees",
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "current_day": "$day_string",
                        "employee_codes": {
                            "$map": {
                                "input": "$employees",
                                "as": "employee",
                                "in": "$$employee.emp_code",
                            }
                        },
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$current_day"]},
                                        {
                                            "$in": [
                                                "$emp_code",
                                                "$$employee_codes",
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$set": {
                    "headcount": {"$size": "$employees"},
                    "is_working_day": {
                        "$not": [
                            {
                                "$in": [
                                    {"$dayOfWeek": "$date"},
                                    [1, 7],
                                ]
                            }
                        ]
                    },
                    "present_count": {
                        "$reduce": {
                            "input": "$logs",
                            "initialValue": 0,
                            "in": {
                                "$add": [
                                    "$$value",
                                    {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$$this.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {
                                                        "$eq": [
                                                            "$$this.half_day",
                                                            True,
                                                        ]
                                                    },
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    },
                                ]
                            },
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {
                                            "$ifNull": [
                                                "$$log.late_minutes",
                                                0,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                                                        {
                                "$divide": [
                                    "$present_count",
                                    "$headcount",
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$day_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        # $documents is a database-level aggregation stage.
        result = explain_aggregate(1, pipeline)
    return result

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Check that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Convert the supplied timestamp to UTC.
    if body.punched_at is not None:
        ts = utc_from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(
            microsecond=0,
            tzinfo=None,
        )

    # 3. Find the employee's most recent open attendance record
    # whose punch-in is not later than the punch-out timestamp.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out",
        )

    # 4. Validate the punch-out time.
    elapsed_seconds = (
        ts - record["punch_in"]
    ).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    # 5. Calculate attendance values.
    work_hours = compute_work_hours(
        record["punch_in"],
        ts,
    )

    overtime_minutes = compute_overtime(
        ts,
        emp["shift_end"],
        record["date"],
        emp["shift_start"],
    )

    half_day = work_hours < 4.50

    # 6. Atomically close the record.
    # Only one concurrent request can update an open record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out",
        )

    # 7. Return the updated record without MongoDB's internal ID.
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    return serialize_timestamps(updated)


@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: AttendanceCorrectionIn,
):
    # 1. Find the employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if not record:
        raise HTTPException(
            status_code=404, detail="Attendance record not found"
        )

    # 2. Build the proposed values without changing the database yet.
    supplied = body.model_fields_set
    old_status = record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_status = body.status if body.status is not None else old_status


    if new_status in {"PRESENT", "WFH", "ON_DUTY"}:
        if "punch_in" in supplied and body.punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

    new_in = (
        utc_from_epoch_ms(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else old_in
    )

    if "punch_out" in supplied:
        new_out = (
            utc_from_epoch_ms(body.punch_out)
            if body.punch_out is not None
            else None
        )
    else:
        new_out = old_out



    if new_status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch timestamps",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    # 3. Validate corrected timestamps.
    if new_in is not None:
        if attendance_date_for(new_in, emp) != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    if new_out is not None:
        if new_in is None:
            raise HTTPException(
                status_code=422, detail="punch_out requires punch_in"
            )
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # 4. Recalculate derived fields.
    if new_in is None:
        late_minutes = 0
    else:
        late_minutes = compute_late_minutes(
            new_in.replace(tzinfo=timezone.utc).astimezone(IST),
            emp["shift_start"],
            emp["shift_end"],
        )

    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        overtime_minutes = compute_overtime(
            new_out, emp["shift_end"], date, emp["shift_start"]
        )
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # 5. Record only fields whose values actually changed.
    changes = {}
    for field, new_value in proposed.items():
        old_value = record.get(field)
        if old_value != new_value:
            changes[field] = {"old": old_value, "new": new_value}

    if not changes:
        raise HTTPException(
            status_code=422, detail="Correction does not change the record"
        )

    # 6. Atomically update the record and append its audit entry.
    now = datetime.now(timezone.utc).replace(microsecond=0).replace(tzinfo=None)
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
        },
        {
            "$set": proposed,
            "$push": {
                "history": {
                    "at": now,
                    "by": body.regularized_by,
                    "reason": body.reason,
                    "changes": changes,
                }
            },
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}, {"_id": 0}
    )
    return serialize_timestamps(updated)
