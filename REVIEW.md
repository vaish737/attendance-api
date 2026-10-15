# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

# REVIEW.md

## Defects Found in the Starter `app/main.py` and Fixes

The following defects were identified in the starter code and fixed. Examples were checked against the starter implementation.

| # | Where | What Is Wrong | How You Would Notice It | How I Fixed It |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Missing `tz_aware=True`, causing PyMongo to return naive datetimes. Mixing naive and timezone-aware datetimes causes errors. | `compute_overtime` with a naive `punch_out` raises `TypeError: can't subtract offset-naive and offset-aware datetimes`, resulting in HTTP 500. | Enabled `tz_aware=True`. All instants are timezone-aware in the application and converted to IST only for R1/R2/R3 calculations. |
| 2 | `compute_late_minutes` | The shift start is constructed using `.replace(hour, minute)` on the UTC clock instead of IST. | For a punch-in at 10:15:59 IST on a 09:30 shift, the shift start is 5.5 hours off, producing a negative difference and returning 0 instead of 45 minutes. | Constructed the shift start in IST using the attendance date and shift start time. |
| 3 | `compute_late_minutes` | The grace-period check compares whole minutes using `minutes > 10`, discarding seconds. | Punch-ins at 09:40:01 and 09:40:59 on a 09:30 shift return 0. R2 requires lateness when the delay is strictly greater than 10 minutes, so the result should be 10 minutes. | Compare the delay in seconds against 600, then floor-divide by 60. At 09:40:00, lateness is 0; at 09:40:01, it is 10 minutes. |
| 4 | `compute_late_minutes`, `compute_overtime`, date handling | No overnight-shift handling when `shift_end <= shift_start`. | For a 22:00–06:00 shift, a 02:00 punch-in is compared with 22:00 on the same day. A punch-out at 06:40 the next day produces 1,480 minutes of overtime instead of 40. | Implemented `attendance_date()` to apply R1: for overnight shifts, punches earlier than `shift_end` belong to the previous attendance date. `shift_end_dt()` adds one day for overnight shifts. |
| 5 | `compute_work_hours` | Uses `round(x, 2)`, which applies Python's floating-point/banker's rounding, and does not truncate seconds. This violates R4. | Working for 54 seconds equals 0.015 hours. `round(0.015, 2)` gives 0.01, whereas R4 requires 0.02. | Used `Decimal(seconds) / 3600`, quantized with `ROUND_HALF_UP`. Instants are truncated to whole seconds on entry. `half_day` is derived from the rounded value, as required by R5. |
| 6 | `compute_overtime` | Does not enforce the 30-minute minimum required by R3. | Punching out 10 minutes after the shift ends returns 10 minutes of overtime instead of 0. | Floored the duration to whole minutes and returned 0 when overtime was below 30 minutes. |
| 7 | `punch_in`, time handling | `datetime.fromtimestamp(ms / 1000)` uses the server's local timezone and retains milliseconds. `datetime.now()` also produces a naive local datetime. Consequently, the stored attendance date can differ from the IST date, violating R1. | When the server runs in UTC, a punch at 00:30 IST can be assigned to the previous day's date. | Implemented `ms_to_dt()` to convert timestamps through UTC and truncate them to whole seconds. The attendance date is derived using `attendance_date()` in IST. |
| 8 | `punch_in`, employee lookup | An unknown `emp_code` returns `None` from the employee lookup, and accessing `emp["shift_start"]` raises an exception. | Posting a punch-in for an unknown employee produces HTTP 500 instead of the required HTTP 404. | Implemented `get_employee()` to raise HTTP 404 when the employee does not exist. |
| 9 | `punch_in`, check-then-insert | The `find_one` followed by `insert_one` approach introduces a race condition, and no unique index prevents duplicate attendance records. | Ten parallel identical punch-ins can produce multiple HTTP 201 responses and duplicate documents. | Added a unique compound index on `(emp_code, date)`. Removed the check-then-insert sequence and used `insert_one`; `DuplicateKeyError` is converted to HTTP 409. The race test produces `[201, 409 x 9]`. |
| 10 | `PunchInIn` | Validation is too permissive: `punched_at` accepts seconds, floats, and numeric strings; `status` accepts arbitrary strings, including `ABSENT`; and `if body.punched_at` treats `0` as if no value was provided. | `punched_at=1783312500` (seconds) is accepted as a 1970 timestamp, and `status="ABSENT"` is accepted despite the contract requiring HTTP 422. | Implemented strict `EpochMs` validation with an integer type and a sensible millisecond range. Restricted `status` to valid presence statuses and used `is not None` for optional-value checks. |
| 11 | `punch_in` response | Returns the raw MongoDB document, exposing the ObjectId and datetime values instead of the required epoch-millisecond timestamps. | The response contains an `id` field and ISO-formatted datetime values, contrary to the API contract. | Implemented `record_out()` to construct the required response shape, exclude `_id`, return epoch milliseconds, and supply defaults for missing legacy fields. |
| 12 | `create_employee` | The check-then-insert approach is vulnerable to race conditions, and there is no unique index on `emp_code`. | Ten concurrent POST requests with the same `emp_code` can produce multiple HTTP 201 responses. | Added a unique index on `emp_code` and converted `DuplicateKeyError` into HTTP 409. |
| 13 | `create_employee` | `created_at` is generated using naive local time and returned as a datetime rather than epoch milliseconds. | The response contains a datetime string instead of the required epoch-millisecond timestamp. | Used a UTC-aware timestamp and returned milliseconds through `employee_out()`. |
| 14 | `EmployeeIn` | Input validation is missing for employee codes, names, email addresses, shift times, and joining dates. Equal shift start and end times are also accepted. Invalid values can cause helper functions to raise HTTP 500 later. | The model accepts values such as `emp_code="x"`, `name=""`, `email="nope"`, and `shift_start="25:99"`. | Added Pydantic patterns and length constraints, email validation, real-date validation, and a check requiring `shift_start != shift_end`. Used `[0-9]` instead of `\d` in the relevant patterns. |
| 15 | `list_employees` | `skip = page * page_size` skips the first page. `total` counts the entire collection rather than the filtered results. There is no deterministic sort or validation for pagination parameters. | Page 1 returns employees 21–40. Filtering by department still reports the total number of all employees. Invalid values such as `page=0` or `page_size=-1` can cause errors. | Changed the offset to `skip = (page - 1) * page_size`. Applied `count_documents` to the same filter, sorted by `emp_code`, and constrained `page >= 1` and `1 <= page_size <= 100`. |
| 16 | `list_attendance` | Loads every matching document into Python, then sorts and slices the results. It has no `emp_code` tie-breaker, and `total` is calculated using `len(docs)`. | With 100,000 records, each request can take seconds. Records with equal dates can appear in arbitrary order. | Moved sorting, skipping, and limiting into MongoDB. Added the `(date desc, emp_code)` and `(emp_code, date)` indexes and used `count_documents` for the total. |
| 17 | `list_attendance`, inputs and outputs | `date_from` and `date_to` are not validated, reversed date ranges are not rejected, `status` is not validated, IDs are exposed, and legacy documents may lack `history` or `half_day`. | `date_from=garbage` silently returns no results, while `date_from > date_to` returns HTTP 200 instead of HTTP 422. | Added real-date validation, date-range validation, and status validation. Used `record_out()` to hide IDs and supply defaults for legacy fields. |
| 18 | `/health` | Always returns `ok`, even when MongoDB is unavailable. | Stopping MongoDB and calling `/health` still returns HTTP 200, contrary to the required HTTP 503 behavior. | Added a MongoDB server ping and converted `PyMongoError` into HTTP 503. Set `serverSelectionTimeoutMS=5000` so the health check cannot hang indefinitely. |
| 19 | Entire file | No indexes are created, and there is no application startup hook to ensure that they exist. | MongoDB `explain` results show `COLLSCAN`, indicating collection scans. | Implemented `ensure_indexes()` and invoked it through the FastAPI application lifespan. Index creation is idempotent and occurs before the application serves requests. |

## Looked At and Decided It Is Fine

- **Synchronous `def` endpoints with PyMongo:** FastAPI runs synchronous endpoints in a thread pool. `MongoClient` is thread-safe, so I retained the synchronous endpoints rather than unnecessarily converting them to asynchronous endpoints.

- **`load_dotenv()` (if present in the starter):** It does not override variables already defined in the environment by default. Therefore, injected `MONGO_URI` and `MONGO_DB` values take precedence over values in the `.env` file.

- **`status_code=201` on create endpoints and `HTTPException(409)` for duplicates:** These match the API contract.

- **Initial values written at punch-in:** The values `history: []`, `punch_out: None`, `work_hours: None`, `overtime_minutes: 0`, and `half_day: False` match the requirements in `DATA_MODEL.md`.

## Verification

The analytics smoke test completed with **zero failures**. It verified monthly employee analytics, department summaries, leaderboards, department trends, validation errors, and MongoDB query plans.

The query-plan checks reported `IXSCAN` with no `COLLSCAN` for the tested queries.
