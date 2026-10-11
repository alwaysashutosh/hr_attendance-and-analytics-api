"""
Employee Attendance & Analytics API - Production Implementation
Full implementation adhering to openapi.yaml contract and DATA_MODEL.md specification.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MILLIS = 100_000_000_000
MAX_EPOCH_MILLIS = 4_102_444_800_000


def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees collection indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

        # attendance_logs collection indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in_desc"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_att_date_late"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("status", ASCENDING)], name="idx_att_date_status"
        )
    except Exception as e:
        # In testing or offline environments, index creation will log or retry on connect
        print(f"Index creation warning: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_test_ui():
    ui_path = os.path.join(os.path.dirname(__file__), "..", "test_ui.html")
    if os.path.exists(ui_path):
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Test UI not found. Please create test_ui.html in the root folder.</h1>"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | int | Decimal], decimals: int) -> Optional[float]:
    """Round a number half-up to the given number of decimal places."""
    if val is None:
        return None
    d = Decimal(str(val))
    exp = Decimal("10") ** (-decimals)
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def truncate_ms_to_seconds(ms: int) -> int:
    """Instants are truncated to whole seconds (R1)."""
    return (ms // 1000) * 1000


def ms_to_datetime_utc(ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime."""
    truncated = truncate_ms_to_seconds(ms)
    return datetime.fromtimestamp(truncated / 1000, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def current_epoch_ms() -> int:
    """Return current server time in epoch milliseconds truncated to whole seconds."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return truncate_ms_to_seconds(now_ms)


def determine_attendance_date(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of punch_in.
    Exception: overnight shifts (shift_end <= shift_start): punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_ist = punch_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (end_h, end_m, 0):
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_utc: datetime, attendance_date_str: str, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. Late only if punch_in is strictly more than 10:00
    after shift_start. When late, late_minutes = whole minutes (floor) elapsed since
    shift_start - not since the grace period ended. Otherwise 0.
    """
    punch_ist = punch_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST)

    diff_sec = (punch_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str
) -> int:
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    For overnight shift, shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    end_h, end_m = map(int, shift_end.split(":"))

    if shift_end <= shift_start:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = diff_sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """
    R7: Count Mon-Fri days between start_date_str and end_date_str inclusive.
    """
    d_start = date.fromisoformat(start_date_str)
    d_end = date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    count = 0
    while cur <= d_end:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee MongoDB document to Employee API response schema."""
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = datetime_to_ms(created_at)
    else:
        created_at_ms = created_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance MongoDB document to AttendanceRecord API response schema."""
    history_api = []
    for h in doc.get("history") or []:
        h_at = h.get("at")
        h_at_ms = datetime_to_ms(h_at) if isinstance(h_at, datetime) else h_at
        changes_api = {}
        for k, v in (h.get("changes") or {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_val = datetime_to_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = datetime_to_ms(to_val) if isinstance(to_val, datetime) else to_val
            changes_api[k] = {"from": from_val, "to": to_val}
        history_api.append(
            {
                "at": h_at_ms,
                "by": h.get("by", ""),
                "reason": h.get("reason", ""),
                "changes": changes_api,
            }
        )

    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status", "PRESENT"),
        "punch_in": datetime_to_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": datetime_to_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_api,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start and shift_start == v:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    punch_out: Optional[int] = Field(None, ge=MIN_EPOCH_MILLIS, le=MAX_EPOCH_MILLIS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MongoDB unavailable: {str(e)}",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    ts_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    ts_dt = ms_to_datetime_utc(ts_ms)

    att_date = determine_attendance_date(ts_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_dt, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    ts_ms = body.punched_at if body.punched_at is not None else current_epoch_ms()
    out_dt = ms_to_datetime_utc(ts_ms)

    # Find the most recent record whose punch_in <= punched_at
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that record is already punched out",
        )

    in_dt = rec["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = (out_dt - in_dt).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    work_hrs = compute_work_hours(in_dt, out_dt)
    overtime_min = compute_overtime(out_dt, rec["date"], emp["shift_start"], emp["shift_end"])
    is_half_day = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that record is already punched out",
        )

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to",
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="attendance record not found for that date",
        )

    old_status = rec.get("status")
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    new_status = body.status if body.status is not None else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="supplying punch_in or punch_out with ABSENT or LEAVE is not allowed",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = ms_to_datetime_utc(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="presence status requires a punch_in",
            )

        # punch_in must stay on the record's attendance date (R1)
        calc_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must belong to the record's attendance date per R1",
            )

        if body.punch_out is not None:
            new_punch_out = ms_to_datetime_utc(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, date_str, emp["shift_start"], emp["shift_end"]
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, date_str, emp["shift_start"])

    # Determine what actually changed
    changes_db = {}
    if new_status != old_status:
        changes_db["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes_db["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes_db["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_db:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request changes nothing",
        )

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Atomic update appending history to prevent race condition losing entries
    current_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="concurrent modification detected",
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def get_month_date_range(month_str: str) -> tuple[str, str, int, int]:
    """Return (month_start, month_end, year, month)."""
    parts = month_str.split("-")
    y, m = int(parts[0]), int(parts[1])
    num_days = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{num_days:02d}", y, m


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="employee not found")

    m_start, m_end, _, _ = get_month_date_range(month)

    # R7: working days counted from joined_on for mid-month joiners
    eff_start = max(emp["joined_on"], m_start)
    working_days = count_working_days(eff_start, m_end)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": m_start, "$lte": m_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon-Fri
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    m_start, m_end, _, _ = get_month_date_range(month)

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                    {"$gte": ["$date", m_start]},
                                    {"$lte": ["$date", m_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                            "day_of_week": {
                                "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ],
                            },
                        }
                    }
                },
                "present_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes",
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$avg": "$present_work_hours.work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": ASCENDING}},
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        avg_wh = d.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append(
            {
                "department": d["department"],
                "headcount": d["headcount"],
                "present_days": float(d["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": d["late_count"],
                "total_late_minutes": int(d["total_late_minutes"] or 0),
                "leave_count": d["leave_count"],
                "on_duty_count": d["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    m_start, m_end, _, _ = get_month_date_range(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": m_start, "$lte": m_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    out_items = []
    for it in items:
        out_items.append(
            {
                "rank": int(it["rank"]),
                "emp_code": it["emp_code"],
                "name": it["name"],
                "department": it["department"],
                "total_late_minutes": int(it["total_late_minutes"]),
                "late_count": int(it["late_count"]),
            }
        )

    return {"month": month, "items": out_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' must be >= 'from'",
        )

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date range must not exceed 92 days",
        )

    dept_employees = list(
        db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0})
    )
    if not dept_employees:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown department '{department}'",
        )

    dept_emp_codes = [e["emp_code"] for e in dept_employees]

    # Generate daily trend using MongoDB aggregation
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "date_str": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$dateFromString": {"dateString": from_date}},
                            "date_str": from_date,
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(d_from, datetime.min.time()),
                        datetime.combine(d_to + timedelta(days=1), datetime.min.time()),
                    ],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                }
            }
        },
        {
            "$group": {
                "_id": "$date_str",
                "present_count": {"$max": {"$ifNull": ["$present_count", 0.0]}},
                "late_count": {"$max": {"$ifNull": ["$late_count", 0]}},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    db_rows = list(db.attendance_logs.aggregate(pipeline))
    row_map = {r["_id"]: r for r in db_rows}

    # Format exactly one row per calendar day
    cur = d_from
    items = []
    rates: List[Optional[float]] = []

    while cur <= d_to:
        c_str = cur.isoformat()
        is_working = cur.weekday() < 5
        hc = sum(1 for e in dept_employees if e["joined_on"] <= c_str)

        row = row_map.get(c_str, {})
        pres = float(row.get("present_count", 0.0))
        late_c = int(row.get("late_count", 0))

        if is_working and hc > 0:
            att_rate = round_half_up(pres / hc, 4)
        else:
            att_rate = None

        rates.append(att_rate)

        # 7-day moving average among non-null rates in window [max(0, i-6) .. i]
        window = [r for r in rates[-7:] if r is not None]
        if window:
            m_avg = round_half_up(sum(window) / len(window), 4)
        else:
            m_avg = None

        items.append(
            {
                "date": c_str,
                "is_working_day": is_working,
                "headcount": hc,
                "present_count": pres,
                "late_count": late_c,
                "attendance_rate": att_rate,
                "moving_avg_7d": m_avg,
            }
        )
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        coll_name = "attendance_logs"
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter
        skip = (page - 1) * page_size
        cmd: Dict[str, Any] = {
            "find": coll_name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        coll_name = "attendance_logs"
        m_start, m_end, _, _ = get_month_date_range(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": 1},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        coll_name = "employees"
        m_start, m_end, _, _ = get_month_date_range(month)
        match_emp: Dict[str, Any] = {"joined_on": {"$lte": m_end}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        coll_name = "attendance_logs"
        m_start, m_end, _, _ = get_month_date_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": m_start, "$lte": m_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        coll_name = "attendance_logs"
        dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1}))
        dept_emp_codes = [e["emp_code"] for e in dept_employees]
        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": coll_name, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    # Convert BSON extended json types to standard JSON primitives
    explain_json = json.loads(json_util.dumps(explain_res))
    return JSONResponse(
        content={"endpoint": endpoint, "collection": coll_name, "explain": explain_json}
    )
