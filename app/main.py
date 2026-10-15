"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import time as time_module
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone ,time
from typing import Optional,Literal,Annotated
from decimal import Decimal,ROUND_HALF_UP
from calendar import monthrange
import json
from enum import Enum
from bson import json_util

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query,Path
from pydantic import BaseModel,Field,ConfigDict,field_validator,model_validator
from pymongo import MongoClient,ASCENDING,DESCENDING,ReturnDocument
from pymongo.errors import DuplicateKeyError,PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),tz_aware=True,serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    db.employees.create_index([("emp_code",ASCENDING)],unique=True,name="emp_code_unique")
    db.employees.create_index([("department",ASCENDING),("emp_code",ASCENDING)],name="dept_emp_code")
    db.employees.create_index([("joined_on",ASCENDING)],name="joined_on")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    
@asynccontextmanager
async def lifespan(app):
    ensure_indexes()
    yield
      
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0",lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
GRACE_SECONDS=10*60

def parse_hm(value:str)->time:
    h,m=value.split(":")
    return time(int(h),int(m))

def is_overnight(shift_start:str,shift_end:str)->bool:
    return parse_hm(shift_end)<=parse_hm(shift_start)

def attendance_date(punch_in:datetime,shift_start:str,shift_end:str)->date:
    ist=punch_in.astimezone(IST)
    day=ist.date()
    if is_overnight(shift_start,shift_end) and ist.time() < parse_hm(shift_end):
        day-=timedelta(days=1)
    return day

def shift_start_dt(day:date,shift_start:str)->datetime:
    return datetime.combine(day,parse_hm(shift_start),tzinfo=IST)

def shift_end_dt(day:date,shift_start:str,shift_end:str)->datetime:
    end=datetime.combine(day,parse_hm(shift_end),tzinfo=IST)
    if is_overnight(shift_start,shift_end):
        end +=timedelta(days=1)
    return end
        

def compute_late_minutes(punch_in: datetime, shift_start: str ,day:date) -> int:
    punch_in=punch_in.astimezone(IST).replace(microsecond=0)
    seconds=int((punch_in-shift_start_dt(day,shift_start)).total_seconds())
    return seconds//60 if seconds>GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in=punch_in.replace(microsecond=0)
    punch_out=punch_out.replace(microsecond=0)
    
    seconds=int((punch_out-punch_in).total_seconds())
    
    hours=(Decimal(seconds)/Decimal(3600)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start:str, shift_end: str, day: date) -> int:
    
    punch_out=punch_out.astimezone(IST).replace(microsecond=0)
    seconds=int((punch_out-shift_end_dt(day,shift_start,shift_end)).total_seconds())
    minutes=seconds//60
    return minutes if minutes>=30 else 0

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def dt_to_ms(dt):
    if dt is None:
        return None
    return (dt - EPOCH) // timedelta(milliseconds=1)     # exact integer maths, no float drift

def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d.get("name"), "email": d.get("email"),
        "department": d.get("department"), "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"), "joined_on": d.get("joined_on"),
        "created_at": dt_to_ms(d.get("created_at")),
    }

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

def ms_to_dt(ms: int) -> datetime:
    """epoch ms -> UTC datetime, truncated to whole seconds (R1)"""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)

def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp

def record_out(d: dict) -> dict:
    """Mongo document -> the API's attendance record. Never exposes _id; fills defaults for legacy documents."""
    history = []
    for h in d.get("history") or []:
        changes = {}
        for field, c in (h.get("changes") or {}).items():
            conv = dt_to_ms if field in ("punch_in", "punch_out") else (lambda v: v)
            changes[field] = {"from": conv(c.get("from")), "to": conv(c.get("to"))}
        history.append({"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": history,
    }
    
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_STATUSES = {"PRESENT", "ABSENT", "WFH", "ON_DUTY", "LEAVE"}  
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}

def parse_filter_date(name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)          # rejects 2026-02-31
    except ValueError:
        raise HTTPException(422, f"{name} is not a real date")
    return value


def _history_value(field, value):
    return dt_to_ms(value) if field in ("punch_in", "punch_out") else value


def record_out(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                f: {"from": _history_value(f, c.get("from")),
                    "to": _history_value(f, c.get("to"))}
                for f, c in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": int(d.get("late_minutes") or 0),
        "overtime_minutes": int(d.get("overtime_minutes") or 0),
        "half_day": bool(d.get("half_day") or False),
        "history": history,
    }
    
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "year must be >= 0001")
    return date(year, mon, 1), date(year, mon, monthrange(year, mon)[1])


def half_up_div(num: int, den: int) -> int:
    """round-half-up(num / den) for non-negative ints, exact."""
    return (2 * num + den) // (2 * den)


# --- pieces of the aggregation ---
IS_PRESENT = {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
IS_WEEKDAY = {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {
    "dateString": "$date", "format": "%Y-%m-%d"}}}, 6]}               # Mon=1 ... Fri=5
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MIN, 0]}
HALVES_IF_PRESENT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}
HALVES_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, HALVES_IF_PRESENT, 0]}


def pipeline_employee_monthly(emp_code: str, first: date, last: date):
    return [
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "halves": {"$sum": HALVES_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [IS_LATE, LATE_MIN, 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

   
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
HOURS_CENTS = {"$cond": [HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}


def pipeline_department_summary(first: date, last: date, department: Optional[str]):
    emp_match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "halves": {"$sum": HALVES_WEEKDAY},
                    "hours_cents": {"$sum": HOURS_CENTS},
                    "hours_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
                    "late_minutes": {"$sum": {"$cond": [IS_LATE, LATE_MIN, 0]}},
                    "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "halves": {"$sum": {"$ifNull": ["$s.halves", 0]}},
            "hours_cents": {"$sum": {"$ifNull": ["$s.hours_cents", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$s.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_minutes": {"$sum": {"$ifNull": ["$s.late_minutes", 0]}},
            "leave": {"$sum": {"$ifNull": ["$s.leave", 0]}},
            "on_duty": {"$sum": {"$ifNull": ["$s.on_duty", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]

def pipeline_late_leaderboard(first: date, last: date, limit: int, department: Optional[str]):
    emp_filter = [{"$match": {"department": department}}] if department is not None else []
    return [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": emp_filter + [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "e",
        }},
        {"$unwind": "$e"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id",
                      "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]

HALVES_ANY_DAY = {"$cond": [IS_PRESENT, HALVES_IF_PRESENT, 0]}   # weekend records count too


def pipeline_department_trend(department: str, d_from: date, d_to: date):
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)

    # one calendar row per day: look up that day's stats (if any), default to zeros
    day_row = {"$let": {
        "vars": {"dt": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}},
        "in": {"$let": {
            "vars": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}},
            "in": {"$let": {
                "vars": {"st": {"$arrayElemAt": [
                    {"$filter": {"input": "$stats", "as": "s",
                                 "cond": {"$eq": ["$$s._id", "$$ds"]}}}, 0]}},
                "in": {
                    "date": "$$ds",
                    "is_working_day": {"$lt": [{"$isoDayOfWeek": "$$dt"}, 6]},
                    "present_halves": {"$ifNull": ["$$st.present_halves", 0]},
                    "late_count": {"$ifNull": ["$$st.late_count", 0]},
                },
            }},
        }},
    }}
    window = {"documents": [-6, 0]}   # current row + up to 6 before, never before the first row

    return [
        # 1. this department's records in range, grouped per day
        {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
        {"$lookup": {
            "from": "employees", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"department": department}}, {"$project": {"_id": 1}}],
            "as": "emp",
        }},
        {"$match": {"emp.0": {"$exists": True}}},
        {"$group": {"_id": "$date",
                    "present_halves": {"$sum": HALVES_ANY_DAY},
                    "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        # 2. $facet always emits one document, so the calendar is built even with zero records
        {"$facet": {"stats": [{"$sort": {"_id": 1}}]}},
        {"$project": {"_id": 0, "rows": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i", "in": day_row}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        # 3. headcount per day (R9)
        {"$lookup": {
            "from": "employees", "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        # 4. rate in 1e-4 units, half-up on integers; null off working days or with no headcount
        {"$addFields": {"scaled": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [10000, "$present_halves"]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$isNumber": "$scaled"}, 1, 0]}}},
        # 5. moving average over the rounded rates ($sum skips nulls; has_rate counts non-nulls)
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "win_sum": {"$sum": "$scaled", "window": window},
            "win_cnt": {"$sum": "$has_rate", "window": window}}}},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present_halves", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$has_rate", 1]},
                                          {"$divide": ["$scaled", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$win_cnt", 0]},
                {"$divide": [{"$floor": {"$divide": [
                    {"$add": [{"$multiply": [2, "$win_sum"]}, "$win_cnt"]},
                    {"$multiply": [2, "$win_cnt"]}]}}, 10000]},
                None]},
        }},
    ]


def validate_trend_range(d_from: str, d_to: str):
    a = parse_filter_date("from", d_from)      # 422 if bad format or fake date
    b = parse_filter_date("to", d_to)
    a, b = date.fromisoformat(a), date.fromisoformat(b)
    if b < a:
        raise HTTPException(422, "to must not be before from")
    if (b - a).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return a, b

def attendance_find_args(emp_code, date_from, date_to, status, page, page_size) -> dict:
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status:
        flt["status"] = status
    return {"filter": flt,
            "sort": [("date", -1), ("emp_code", 1)],
            "skip": (page - 1) * page_size,
            "limit": page_size}


def validate_attendance_filters(date_from, date_to, status):
    date_from = parse_filter_date("date_from", date_from)
    date_to = parse_filter_date("date_to", date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(VALID_STATUSES)}")
    
def jsonable(doc):
    """BSON types (dates, Int64, ObjectId...) -> plain JSON."""
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))

def _require(name: str, value):
    if value is None:
        raise HTTPException(422, f"{name} is required for this endpoint")
    return value

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

class EmployeeIn(BaseModel):
    emp_code: str=Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str=Field(min_length=1, max_length=100)
    email: str= Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str= Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str=Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


    @field_validator("joined_on")
    @classmethod
    def real_date(cls, v):
        date.fromisoformat(v)         
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self
    
class PunchInIn(BaseModel):
    emp_code: str=Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"]="PRESENT"  # ABSENT/LEAVE are not allowed here

class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None
    
class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v):
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        return v

class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}



@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump()
    now = datetime.now(timezone.utc)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)   # Mongo keeps milliseconds
    try:
        db.employees.insert_one(doc)          # the unique index decides, not a find() beforehand
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)

    


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1,ge=1), page_size: int = Query( 20,ge=1,le=100)):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q, {"_id": 0})
           .sort("emp_code", ASCENDING)                  # stable order, so pages don't overlap
           .skip((page - 1) * page_size)                 # page 1 skips nothing
           .limit(page_size))
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)                                  # 404 if unknown
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None \
         else datetime.now(timezone.utc).replace(microsecond=0)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])    # R1, handles overnight
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)          # <-- THE RACE GUARD: no find() first
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    validate_attendance_filters(date_from, date_to, status)
    a = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(a["filter"])
    cur = (db.attendance_logs.find(a["filter"]).sort(a["sort"])
           .skip(a["skip"]).limit(a["limit"]))
    return {"items": [record_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None \
         else datetime.now(timezone.utc).replace(microsecond=0)

    for _ in range(5):
        # most recent record whose punch_in is at or before the punch-out (works across midnight)
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
            sort=[("punch_in", DESCENDING)],
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")

        p_in = rec["punch_in"]
        if ts <= p_in:
            raise HTTPException(422, "punched_at must be after the punch-in")
        if ts - p_in > timedelta(hours=24):
            raise HTTPException(422, "punched_at is more than 24 hours after the punch-in")

        day = date.fromisoformat(rec["date"])
        work_hours = compute_work_hours(p_in, ts)
        overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], day)
        half_day = work_hours < 4.5                              # R5: on the ROUNDED value

        updated = db.attendance_logs.find_one_and_update(
            {"_id": rec["_id"], "punch_out": None, "punch_in": p_in},   # <-- THE LOCK: only matches if still open
            {"$set": {"punch_out": ts, "work_hours": work_hours,
                      "overtime_minutes": overtime, "half_day": half_day}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
        # nothing matched: someone closed it first. The loop re-reads, now sees punch_out set -> 409.
    raise HTTPException(409, "record changed concurrently, retry")

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_filter_date("date", date)
    emp = get_employee(emp_code)
    day = datetime.strptime(date, "%Y-%m-%d").date()

    for _ in range(8):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        old_status = rec["status"]
        old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
        status = body.status or old_status

        if status in NON_PRESENT_STATUSES:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, f"{status} clears punch times; do not send punch_in/punch_out")
            new_in = new_out = None
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
            if new_in is None:
                raise HTTPException(422, "a presence status requires a punch_in")
            if body.punch_in is not None and \
               attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        late = compute_late_minutes(new_in, emp["shift_start"], day) if new_in else 0
        if new_in and new_out:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], day)
            half_day = work_hours < 4.5
        else:
            work_hours, overtime, half_day = None, 0, False

        new_values = {"status": status, "punch_in": new_in, "punch_out": new_out,
                      "work_hours": work_hours, "late_minutes": late,
                      "overtime_minutes": overtime, "half_day": half_day}
        old_values = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                      "work_hours": rec.get("work_hours"),
                      "late_minutes": int(rec.get("late_minutes") or 0),
                      "overtime_minutes": int(rec.get("overtime_minutes") or 0),
                      "half_day": bool(rec.get("half_day") or False)}

        if all(old_values[k] == new_values[k] for k in ("status", "punch_in", "punch_out")):
            raise HTTPException(422, "the request changes nothing")

        changes = {k: {"from": old_values[k], "to": new_values[k]}
                   for k in new_values if old_values[k] != new_values[k]}
        entry = {"at": datetime.now(timezone.utc), "by": body.regularized_by,
                 "reason": body.reason, "changes": changes}

        n_hist = len(rec.get("history") or [])
        guard = {"_id": rec["_id"], "status": old_status,
                 "punch_in": old_in, "punch_out": old_out}
        if n_hist:
            guard["history"] = {"$size": n_hist}
        else:
            guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]

        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new_values, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
        # guard didn't match: someone else changed the record, loop re-reads and retries

    raise HTTPException(409, "record was modified concurrently, please retry")

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_RE)]):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)                       # 404 if unknown

    # R7: Mon-Fri, counted from joined_on if they joined mid-month
    joined = date.fromisoformat(emp["joined_on"])
    start = max(first, joined)
    working_days = sum(
        1 for i in range((last - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5   # Mon=0 ... Fri=4
    )                                                  # empty range if joined after the month ends

    row = next(iter(db.attendance_logs.aggregate(pipeline_employee_monthly(emp_code, first, last))), {})
    halves = int(row.get("halves", 0))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up_div(5000 * halves, working_days) / 100 if working_days else None,
    }

@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_RE)],
                       department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(pipeline_department_summary(first, last, department)):
        n = int(r["hours_n"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["halves"]) / 2,
            "avg_work_hours": half_up_div(int(round(r["hours_cents"])), n) / 100 if n else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_minutes"]),
            "leave_count": int(r["leave"]),
            "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[str, Query(pattern=MONTH_RE)],
                     limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(
        pipeline_late_leaderboard(first, last, limit, department)))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     d_from: Annotated[str, Query(alias="from")],
                     d_to: Annotated[str, Query(alias="to")]):
    a, b = validate_trend_range(d_from, d_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.attendance_logs.aggregate(pipeline_department_trend(department, a, b)))
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_RE)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    d_from: Annotated[Optional[str], Query(alias="from")] = None,
    d_to: Annotated[Optional[str], Query(alias="to")] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint is ExplainEndpoint.attendance_list:
        validate_attendance_filters(date_from, date_to, status)
        a = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        command = {"find": coll, "filter": a["filter"], "sort": dict(a["sort"]),
                   "skip": a["skip"], "limit": a["limit"]}
    else:
        if endpoint is ExplainEndpoint.employee_monthly:
            first, last = month_bounds(_require("month", month))
            coll = "attendance_logs"
            pipeline = pipeline_employee_monthly(_require("emp_code", emp_code), first, last)
        elif endpoint is ExplainEndpoint.department_summary:
            first, last = month_bounds(_require("month", month))
            coll = "employees"
            pipeline = pipeline_department_summary(first, last, department)
        elif endpoint is ExplainEndpoint.late_leaderboard:
            first, last = month_bounds(_require("month", month))
            coll = "attendance_logs"
            pipeline = pipeline_late_leaderboard(first, last, limit, department)
        else:  # department_trend
            a_, b_ = validate_trend_range(_require("from", d_from), _require("to", d_to))
            coll = "attendance_logs"
            pipeline = pipeline_department_trend(_require("department", department), a_, b_)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint.value, "collection": coll, "explain": jsonable(result)}