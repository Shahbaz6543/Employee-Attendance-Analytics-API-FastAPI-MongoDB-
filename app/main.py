
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError


from pydantic import BaseModel, Field, ConfigDict
from typing import Literal, Optional


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None

    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Database indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employees_department_emp_code",
    )
    db.attendance_logs.create_index(
        [("date", -1)],
        name="ix_attendance_date",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1)],
        name="ix_attendance_status_date",
    )
    db["employees"].create_index(
    [("emp_code", 1)],
    unique=True,
    name="uq_employees_emp_code",
)

db["employees"].create_index(
    [("department", 1), ("joined_on", 1)],
    name="idx_employees_department_joined",
)

db["attendance"].create_index(
    [("emp_code", 1), ("date", 1)],
    name="idx_attendance_emp_date",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_shift_time(value: str) -> time:
    try:
        if len(value) != 5:
            raise ValueError
        parsed = time.fromisoformat(value)
        if parsed.strftime("%H:%M") != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Shift time must use HH:MM format",
        )


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format",
        )


def as_utc(value: datetime) -> datetime:
    # PyMongo can return naive datetimes representing UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_timestamp(value: Optional[int]) -> datetime:
    # API timestamps are epoch milliseconds, truncated to whole seconds.
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)

    try:
        return datetime.fromtimestamp(value // 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Timestamp is outside the supported range",
        )


def shift_bounds(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> tuple[datetime, datetime]:
    day = parse_date(attendance_date)
    start_time = parse_shift_time(shift_start)
    end_time = parse_shift_time(shift_end)

    start = datetime.combine(day, start_time, tzinfo=IST)
    end = datetime.combine(day, end_time, tzinfo=IST)

    # R1: Overnight shifts end on the next calendar day.
    if end_time <= start_time:
        end += timedelta(days=1)

    return start, end


def attendance_date_for(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    local = as_utc(punch_in).astimezone(IST)
    start_time = parse_shift_time(shift_start)
    end_time = parse_shift_time(shift_end)
    day = local.date()

    # Early-morning punch-in belongs to the previous overnight shift.
    if end_time <= start_time:
        if (local.hour, local.minute) < (
            end_time.hour, end_time.minute
        ):
            day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    start, _ = shift_bounds(
        attendance_date, shift_start, shift_end
    )
    local_punch = as_utc(punch_in).astimezone(IST)
    elapsed = (local_punch - start).total_seconds()

    # R2: Strictly more than 10 minutes; count from shift start.
    if elapsed <= 600:
        return 0

    return int(elapsed // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (
        as_utc(punch_out) - as_utc(punch_in)
    ).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    # R4: Two decimals, round half-up.
    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    _, scheduled_end = shift_bounds(
        attendance_date, shift_start, shift_end
    )
    local_punch = as_utc(punch_out).astimezone(IST)
    elapsed = (local_punch - scheduled_end).total_seconds()

    # R3: Overtime starts only after at least 30 minutes.
    if elapsed < 1800:
        return 0

    return int(elapsed // 60)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code", "name", "email", "department")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field must not be blank")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        parse_shift_time(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_date(cls, value: str) -> str:
        parse_date(value)
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=0)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("emp_code must not be blank")
        return value


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
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

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    punch_time = parse_timestamp(body.punched_at)
    day = attendance_date_for(
        punch_time, emp["shift_start"], emp["shift_end"]
    )

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punch_time,
            emp["shift_start"],
            emp["shift_end"],
            day,
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
            detail="already punched in for this date",
        )

    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from.isoformat()
        if date_to:
            date_query["$lte"] = date_to.isoformat()
        query["date"] = date_query

    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort("date", -1)
        .skip(skip)
        .limit(page_size)
    )

    # Support older seeded records.
    for item in items:
        item.setdefault("history", [])
        item.setdefault("half_day", False)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# The remaining required contract endpoints still need implementation:
# POST  /attendance/punch-out

@app.post("/attendance/punch-out")
def punch_out(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    punch_time = parse_timestamp(body.punched_at)

    # Find today's open attendance record.
    day = attendance_date_for(
        punch_time,
        emp["shift_start"],
        emp["shift_end"],
    )

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": day,
        "punch_out": None,
        "punch_in": {"$ne": None},
        "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
    })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No open punch-in record found",
        )

    punch_in_time = record["punch_in"]

    if punch_time <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be before punch-in",
        )

    work_hours = compute_work_hours(
        punch_in_time,
        punch_time,
    )

    overtime_minutes = compute_overtime(
        punch_time,
        emp["shift_end"],
        day,
    )

    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_time,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance has already been punched out",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    return updated

# PATCH /attendance/{emp_code}/{date}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeRequest,
):
    from datetime import datetime, timezone
    from decimal import Decimal, ROUND_HALF_UP

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    day = date.isoformat()
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": day,
    })

    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # Use the current record as the starting point.
    old = dict(record)
    old.setdefault("history", [])
    old.setdefault("half_day", False)

    new_status = (
        body.status if body.status is not None else old["status"]
    )
    new_in = old.get("punch_in")
    new_out = old.get("punch_out")

    # Only replace times explicitly supplied in the request.
    if "punch_in" in body.model_fields_set:
        new_in = parse_timestamp(body.punch_in)

    if "punch_out" in body.model_fields_set:
        new_out = parse_timestamp(body.punch_out)

    if new_status in ("ABSENT", "LEAVE"):
        if (
            "punch_in" in body.model_fields_set
            or "punch_out" in body.model_fields_set
        ):
            raise HTTPException(
                422,
                "ABSENT/LEAVE cannot include punch times",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(
            422,
            "Presence status requires punch_in",
        )

    if new_in is not None:
        # Attendance date is based on the punch-in instant.
        expected_day = attendance_date_for(
            new_in, emp["shift_start"], emp["shift_end"]
        )
        if expected_day != day:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date",
            )

    if new_out is not None:
        if new_in is None or new_out <= new_in:
            raise HTTPException(
                422,
                "punch_out must be after punch_in",
            )
        if (new_out - new_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                422,
                "punch_out must be within 24 hours of punch_in",
            )

    # Recompute all derived values from the final times.
    if new_in is None:
        late = 0
    else:
        late = compute_late_minutes(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
            day,
        )

    if new_in is not None and new_out is not None:
        hours = compute_work_hours(new_in, new_out)
        overtime = compute_overtime(
            new_out, emp["shift_end"], day
        )
        half_day = hours < 4.50
    else:
        hours = None
        overtime = 0
        half_day = False

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    # Record only fields whose values actually changed.
    changes = {}
    for key, new_value in new_values.items():
        old_value = old.get(key)
        if key in ("punch_in", "punch_out"):
            old_ms = (
                int(old_value.timestamp() * 1000)
                if old_value is not None else None
            )
            new_ms = (
                int(new_value.timestamp() * 1000)
                if new_value is not None else None
            )
            if old_ms != new_ms:
                changes[key] = {"from": old_ms, "to": new_ms}
        elif old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "Request does not change anything")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update: concurrent corrections cannot overwrite each
    # other's history silently. A stale concurrent edit gets 409.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": old["history"],
            "status": old["status"],
            "punch_in": old.get("punch_in"),
            "punch_out": old.get("punch_out"),
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    # API instants must be returned as epoch milliseconds.
    for field in ("punch_in", "punch_out"):
        if updated.get(field) is not None:
            updated[field] = int(updated[field].timestamp() * 1000)

    for entry in updated.get("history", []):
        if entry.get("at") is not None:
            entry["at"] = int(entry["at"].timestamp() * 1000)
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                for side in ("from", "to"):
                    value = entry["changes"][field].get(side)
                    if value is not None and isinstance(value, datetime):
                        entry["changes"][field][side] = int(
                            value.timestamp() * 1000
                        )

    return updated

# GET   /analytics/employees/{emp_code}/monthly

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
):
    """
    Monthly attendance summary for one employee.

    Follows the EmployeeMonthly schema in openapi.yaml.
    Analytics are calculated using a MongoDB aggregation pipeline.
    """

    # ---------------------------------------------------------
    # 1. Validate month and calculate the month's date boundaries
    # ---------------------------------------------------------
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid month. Expected YYYY-MM."
        )

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_start_str = month_start.isoformat()
    next_month_str = next_month.isoformat()

    # ---------------------------------------------------------
    # 2. Check whether the employee exists
    # ---------------------------------------------------------
    employee = employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1}
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found."
        )

    # ---------------------------------------------------------
    # 3. MongoDB aggregation
    #
    # Start from employees so an employee with zero attendance
    # records still receives the correct working_days value.
    # ---------------------------------------------------------
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code
            }
        },

        # Convert calendar strings into IST-aware date boundaries.
        {
            "$set": {
                "_month_start": {
                    "$dateFromString": {
                        "dateString": {"$literal": month_start_str},
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata"
                    }
                },
                "_month_end": {
                    "$dateFromString": {
                        "dateString": {"$literal": next_month_str},
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata"
                    }
                },
                "_joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "format": "%Y-%m-%d",
                        "timezone": "Asia/Kolkata"
                    }
                }
            }
        },

        # Working days start on the later of joined_on or month start.
        {
            "$set": {
                "_effective_start": {
                    "$max": [
                        "$_joined_date",
                        "$_month_start"
                    ]
                }
            }
        },

        # Generate calendar-day offsets inside MongoDB.
        # A zero-length range handles employees who joined later.
        {
            "$set": {
                "_day_offsets": {
                    "$range": [
                        0,
                        {
                            "$max": [
                                0,
                                {
                                    "$dateDiff": {
                                        "startDate": "$_effective_start",
                                        "endDate": "$_month_end",
                                        "unit": "day"
                                    }
                                }
                            ]
                        }
                    ]
                }
            }
        },

        # Turn offsets into calendar dates.
        {
            "$set": {
                "_calendar_days": {
                    "$map": {
                        "input": "$_day_offsets",
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_effective_start",
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },

        # Count Monday-Friday only (MongoDB dayOfWeek:
        # Sunday=1, Monday=2, ..., Saturday=7).
        {
            "$set": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_calendar_days",
                            "as": "day",
                            "cond": {
                                "$and": [
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "date": "$$day",
                                                    "timezone": "Asia/Kolkata"
                                                }
                                            },
                                            2
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "date": "$$day",
                                                    "timezone": "Asia/Kolkata"
                                                }
                                            },
                                            6
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        },

        # Retrieve only this employee's records for the requested month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "start_date": {
                        "$literal": month_start_str
                    },
                    "end_date": {
                        "$literal": next_month_str
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            "$$start_date"
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            "$date",
                                            "$$end_date"
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1
                        }
                    }
                ],
                "as": "_month_logs"
            }
        },

        # Parse each attendance date and derive its weekday in IST.
        {
            "$set": {
                "_month_logs": {
                    "$map": {
                        "input": "$_month_logs",
                        "as": "log",
                        "in": {
                            "$mergeObjects": [
                                "$$log",
                                {
                                    "_attendance_date": {
                                        "$dateFromString": {
                                            "dateString": "$$log.date",
                                            "format": "%Y-%m-%d",
                                            "timezone": "Asia/Kolkata"
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "_month_logs": {
                    "$map": {
                        "input": "$_month_logs",
                        "as": "log",
                        "in": {
                            "$mergeObjects": [
                                "$$log",
                                {
                                    "_weekday": {
                                        "$dayOfWeek": {
                                            "date": "$$log._attendance_date",
                                            "timezone": "Asia/Kolkata"
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        },

        # Calculate all monthly metrics in MongoDB.
        {
            "$set": {
                "_present_logs": {
                    "$filter": {
                        "input": "$_month_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"]
                                    ]
                                },
                                {"$gte": ["$$log._weekday", 2]},
                                {"$lte": ["$$log._weekday", 6]}
                            ]
                        }
                    }
                },

                "_leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_month_logs",
                            "as": "log",
                            "cond": {
                                "$eq": ["$$log.status", "LEAVE"]
                            }
                        }
                    }
                },

                "_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_month_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                },

                "_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_month_logs",
                            "as": "log",
                            "in": {
                                "$ifNull": ["$$log.late_minutes", 0]
                            }
                        }
                    }
                },

                "_total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$_month_logs",
                            "as": "log",
                            "in": {
                                "$ifNull": ["$$log.overtime_minutes", 0]
                            }
                        }
                    }
                }
            }
        },

        # A half-day contributes 0.5; a full presence day contributes 1.
        {
            "$set": {
                "_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$_present_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$$log.half_day",
                                                    False
                                                ]
                                            },
                                            True
                                        ]
                                    },
                                    0.5,
                                    1
                                ]
                            }
                        }
                    }
                }
            }
        },

        # Return only the fields defined in EmployeeMonthly.
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": "$_working_days",
                "present_days": "$_present_days",
                "leave_days": "$_leave_days",
                "late_count": "$_late_count",
                "total_late_minutes": "$_total_late_minutes",
                "total_overtime_minutes": "$_total_overtime_minutes"
            }
        }
    ]

    # ---------------------------------------------------------
    # 4. Execute aggregation
    # ---------------------------------------------------------
    result = next(employees.aggregate(pipeline), None)

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found."
        )

    # ---------------------------------------------------------
    # 5. Calculate attendance_pct using Decimal half-up rounding
    # ---------------------------------------------------------
    working_days = int(result["working_days"])
    present_days = Decimal(str(result["present_days"]))

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (
                present_days * Decimal("100")
                / Decimal(working_days)
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP
            )
        )

    # ---------------------------------------------------------
    # 6. Return the exact response contract
    # ---------------------------------------------------------
    return {
        "emp_code": result["emp_code"],
        "month": result["month"],
        "working_days": working_days,
        "present_days": float(
            present_days.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        ),
        "leave_days": int(result["leave_days"]),
        "late_count": int(result["late_count"]),
        "total_late_minutes": int(result["total_late_minutes"]),
        "total_overtime_minutes": int(result["total_overtime_minutes"]),
        "attendance_pct": attendance_pct
    }


# GET   /analytics/departments/summary
import calendar
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException, Query


def round_half_up(value, places=2):
    """Round a number using the assignment's half-up rule."""
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = Query(default=None),
):
    # 1. Calculate the month's date range.
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]

    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # 2. Start from employees so employees with zero attendance
    #    records are still included in headcount.
    employee_match = {
        "joined_on": {"$lte": month_end}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {
            "$match": employee_match
        },
        {
            "$lookup": {
                "from": "attendance",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            month_start,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            month_end,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "monthly_attendance",
            }
        },
        {
            "$unwind": {
                "path": "$monthly_attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$set": {
                "attendance_date": {
                    "$cond": [
                        {
                            "$ne": [
                                "$monthly_attendance.date",
                                None,
                            ]
                        },
                        {
                            "$dateFromString": {
                                "dateString": (
                                    "$monthly_attendance.date"
                                ),
                                "format": "%Y-%m-%d",
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$set": {
                "is_present": {
                    "$in": [
                        "$monthly_attendance.status",
                        ["PRESENT", "WFH", "ON_DUTY"],
                    ]
                },
                "is_weekday": {
                    "$and": [
                        {
                            "$ne": [
                                "$attendance_date",
                                None,
                            ]
                        },
                        {
                            "$gte": [
                                {
                                    "$dayOfWeek": "$attendance_date"
                                },
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$dayOfWeek": "$attendance_date"
                                },
                                6,
                            ]
                        },
                    ]
                },
                "late_minutes_value": {
                    "$ifNull": [
                        "$monthly_attendance.late_minutes",
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",

                # Every qualifying employee counts once,
                # even when attendance is missing.
                "headcount": {"$sum": 1},

                # Present days count only Monday-Friday.
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present",
                                    "$is_weekday",
                                ]
                            },
                            {
                                "$cond": [
                                    "$monthly_attendance.half_day",
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                # Average is across qualifying attendance records,
                # not averages of employee averages.
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present",
                                    {
                                        "$ne": [
                                            "$monthly_attendance.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$monthly_attendance.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_present",
                                    {
                                        "$ne": [
                                            "$monthly_attendance.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$late_minutes_value",
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$late_minutes_value",
                                    0,
                                ]
                            },
                            "$late_minutes_value",
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$monthly_attendance.status",
                                    "LEAVE",
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$monthly_attendance.status",
                                    "ON_DUTY",
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$sort": {"_id": 1}
        },
    ]

    # 3. Run the aggregation in MongoDB.
    results = list(db["employees"].aggregate(pipeline))

    # 4. Build the exact response contract.
    items = []

    for row in results:
        hours_count = row["work_hours_count"]

        avg_work_hours = (
            round_half_up(
                row["work_hours_sum"] / hours_count
            )
            if hours_count
            else None
        )

        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": round_half_up(
                    row["present_days"]
                ),
                "avg_work_hours": avg_work_hours,
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )

    return {
        "month": month,
        "items": items,
    }






# GET   /analytics/leaderboard/late
from datetime import date
from typing import Optional

from fastapi import Query


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    # 1. Calculate month boundaries
    year, month_number = map(int, month.split("-"))

    import calendar
    last_day = calendar.monthrange(year, month_number)[1]

    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # 2. Match attendance records with late minutes > 0
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        # 3. Aggregate lateness per employee
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "late_count": {"$sum": 1},
            }
        },
        # 4. Ignore records with no matching employee
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

    # 5. Filter by department when provided
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    # 6. Sort by total late minutes, then emp_code
    pipeline.extend([
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    # 7. Execute aggregation in MongoDB
    results = list(db["attendance"].aggregate(pipeline))

    # 8. Assign standard competition ranks
    items = []
    previous_minutes = None
    current_rank = 0

    for position, row in enumerate(results, start=1):
        minutes = row["total_late_minutes"]

        if minutes != previous_minutes:
            current_rank = position

        row["rank"] = current_rank
        previous_minutes = minutes
        items.append(row)

    # 9. Apply limit AFTER ranking.
    # Include all employees tied at the last allowed rank.
    items = [
        item for item in items
        if item["rank"] <= limit
    ]

    return {
        "month": month,
        "items": items,
    }




# GET   /analytics/departments/{department}/trend
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException, Query


def round_half_up(value, places=2):
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)

    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    # 1. Validate date range
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    total_days = (to_date - from_date).days + 1

    if total_days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range must not exceed 92 days",
        )

    start = from_date.isoformat()
    end = to_date.isoformat()

    # 2. Check whether department exists
    if db["employees"].count_documents(
        {"department": department}
    ) == 0:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    # 3. Generate every calendar date inside MongoDB.
    # Requires MongoDB 6.0+ for the $documents approach.
    pipeline = [
        {
            "$documents": [
                {
                    "start": {
                        "$dateFromString": {
                            "dateString": start
                        }
                    },
                    "end": {
                        "$dateFromString": {
                            "dateString": end
                        }
                    },
                    "department": department,
                }
            ]
        },
        {
            "$project": {
                "department": 1,
                "day": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$start",
                                                "endDate": "$end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": "$start",
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$day"},
        {
            "$set": {
                "date_obj": {
                    "$dateFromString": {
                        "dateString": "$day"
                    }
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$day"
                                }
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },

        # 4. Headcount: employees who joined on or before each day
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "dept": "$department",
                    "current_day": "$day",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$department",
                                            "$$dept",
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$current_day",
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_data",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$headcount_data.count",
                                0,
                            ]
                        },
                        0,
                    ]
                }
            }
        },

        # 5. Daily attendance metrics for this department
        {
            "$lookup": {
                "from": "attendance",
                "let": {
                    "current_day": "$day",
                    "dept": "$department",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": [
                                    "$date",
                                    "$$current_day",
                                ]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "$expr": {
                                "$eq": [
                                    "$employee.department",
                                    "$$dept",
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                [
                                                    "PRESENT",
                                                    "WFH",
                                                    "ON_DUTY",
                                                ],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                "$half_day",
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes",
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_stats",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_stats.present_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_stats.late_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
            }
        },

        # 6. Attendance rate: weekdays only, with positive headcount
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

        # 7. Moving average over current and previous 6 rows
        {
            "$setWindowFields": {
                "sortBy": {"date_obj": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },

        # 8. Return the exact required fields
        {
            "$project": {
                "_id": 0,
                "date": "$day",
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

    # Run aggregation on the database because $documents
    # must be the first stage.
    results = list(db.aggregate(pipeline))

    # 9. Apply half-up rounding required by the contract
    items = []

    for row in results:
        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": round_half_up(
                row["present_count"]
            ),
            "late_count": row["late_count"],
            "attendance_rate": round_half_up(
                row["attendance_rate"], 4
            ),
            "moving_avg_7d": round_half_up(
                row["moving_avg_7d"], 4
            ),
        })

    return {
        "department": department,
        "items": items,
    }




# GET   /admin/explain/{endpoint}
from fastapi import HTTPException
from pymongo.errors import PyMongoError


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    queries = {
        "employees": {
            "collection": "employees",
            "filter": {},
        },
        "attendance": {
            "collection": "attendance",
            "filter": {},
        },
    }

    if endpoint not in queries:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Unknown endpoint",
                "available_endpoints": list(queries.keys()),
            },
        )

    config = queries[endpoint]
    collection = db[config["collection"]]

    try:
        result = collection.find(config["filter"]).explain(
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": config["collection"],
            "executionStats": result.get("executionStats", {}),
            "queryPlanner": result.get("queryPlanner", {}),
        }

    except PyMongoError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MongoDB explain failed: {str(exc)}",
        )
