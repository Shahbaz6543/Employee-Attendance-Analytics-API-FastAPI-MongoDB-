# Employee Attendance & Analytics API

A backend system built with **FastAPI** and **MongoDB** for managing employee records, tracking daily attendance (punch-in / punch-out), and generating attendance analytics.

---

## 1. Project Overview

The Employee Attendance & Analytics API lets organizations maintain employee records, capture daily attendance, and derive insights from that data. Employees punch in and out through the API, and the system enforces one attendance record per employee per day at the database level.

On top of the core attendance workflow, the service exposes analytics endpoints for monthly employee reports, department-wise summaries, late-arrival rankings, and department trends. These are designed to stay efficient as the dataset grows.

---

## 2. Features

- **Employee management**: create and list employee records
- **Attendance punch-in and punch-out**: record daily check-in and check-out times
- **Duplicate attendance prevention**: enforced with a unique database constraint, not just application logic
- **Attendance filtering and pagination**: filter by employee, department, and date range
- **Monthly employee attendance analytics**: days present, late arrivals, and working hours per employee
- **Department-wise attendance summary**: aggregated attendance metrics per department
- **Late arrival leaderboard**: ranks employees by number of late arrivals
- **Department trend analytics**: attendance patterns for each department over time
- **Health check API**: lightweight endpoint for uptime and monitoring

---

## 3. Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| Web Framework | FastAPI |
| Database | MongoDB |
| Database Driver | PyMongo |
| Data Validation | Pydantic |
| ASGI Server | Uvicorn |

---

## 4. Project Architecture

```
Client  →  FastAPI API  →  MongoDB  →  Analytics APIs
```

1. **Client**: a frontend, script, or API tool sends HTTP requests.
2. **FastAPI API**: validates input with Pydantic models, applies business rules (such as duplicate prevention and punch-out checks), and handles routing.
3. **MongoDB**: stores employees and attendance logs. Indexes enforce data integrity and speed up queries.
4. **Analytics APIs**: run MongoDB aggregation pipelines over attendance logs to produce monthly, departmental, and leaderboard insights.

```
.
├── app/
│   ├── main.py          # Application entry point
│   ├── database.py      # MongoDB connection and index setup
│   ├── models/          # Pydantic schemas
│   ├── routers/         # Employees, attendance, analytics routes
│   └── services/        # Business logic and aggregation pipelines
├── requirements.txt
├── .env.example
└── README.md
```

> Adjust the folder layout above to match your repository structure.

---

## 5. API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check for the API and database connection |
| `POST` | `/employees` | Create a new employee |
| `GET` | `/employees` | List employees |
| `POST` | `/attendance/punch-in` | Record an employee's punch-in for the day |
| `POST` | `/attendance/punch-out` | Record an employee's punch-out for the day |
| `GET` | `/attendance` | List attendance logs with filters and pagination |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance report for one employee |
| `GET` | `/analytics/departments/summary` | Attendance summary grouped by department |
| `GET` | `/analytics/leaderboard/late` | Employees ranked by late arrivals |

Interactive documentation is available at `/docs` (Swagger UI) once the server is running.

---

## 6. Database Design

### `employees`

Stores the master record for each employee.

| Field | Type | Description |
|-------|------|-------------|
| `_id` | ObjectId | MongoDB document ID |
| `emp_code` | string | Unique employee identifier |
| `name` | string | Full name |
| `email` | string | Contact email |
| `department` | string | Department name |
| `created_at` | datetime | Record creation timestamp |

### `attendance_logs`

Stores one document per employee per day.

| Field | Type | Description |
|-------|------|-------------|
| `_id` | ObjectId | MongoDB document ID |
| `emp_code` | string | References `employees.emp_code` |
| `date` | string / date | Attendance date (`YYYY-MM-DD`) |
| `punch_in` | datetime | Check-in time |
| `punch_out` | datetime \| null | Check-out time (null until punch-out) |
| `is_late` | boolean | Whether the punch-in was after the cutoff time |
| `department` | string | Denormalized department, used for analytics |

---

## 7. Indexing Strategy

| Collection | Index | Purpose |
|------------|-------|---------|
| `employees` | `emp_code` (unique) | Prevents duplicate employees and speeds up lookups |
| `attendance_logs` | `(emp_code, date)` (unique, compound) | Guarantees one record per employee per day and supports per-employee queries |
| `attendance_logs` | `(department, date)` | Speeds up department summaries and trend analytics |
| `attendance_logs` | `(date, is_late)` | Supports the late arrival leaderboard and date-range scans |

```python
db.employees.create_index("emp_code", unique=True)
db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
db.attendance_logs.create_index([("department", 1), ("date", 1)])
db.attendance_logs.create_index([("date", 1), ("is_late", 1)])
```

---

## 8. Running Locally

**Prerequisites:** Python 3.10+ and a running MongoDB instance (local or MongoDB Atlas).

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/employee-attendance-analytics-api.git
cd employee-attendance-analytics-api

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env            # then edit .env with your values

# 5. Start the FastAPI server
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

---

## 9. Environment Variables

Create a `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

| Variable | Description |
|----------|-------------|
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB` | Name of the database to use |

---

## 10. Testing

The API can be tested interactively with **Swagger UI**:

1. Start the server with `uvicorn app.main:app --reload`.
2. Open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).
3. Expand an endpoint, click **Try it out**, enter the request data, and click **Execute**.

Suggested test flow:

1. `GET /health` to confirm the service and database are reachable.
2. `POST /employees` to create an employee.
3. `POST /attendance/punch-in` to record a check-in.
4. Repeat the punch-in to confirm the duplicate is rejected.
5. `POST /attendance/punch-out` to complete the day's record.
6. `GET /attendance` with filters and pagination parameters.
7. Call the analytics endpoints to review the results.

ReDoc is also available at `/redoc`.

---

## 11. Design Decisions

### Handling concurrent punch-in requests
Checking for an existing record and then inserting leaves a race condition when two requests arrive at the same moment. Instead, the unique compound index on `(emp_code, date)` acts as the source of truth. The API attempts the insert directly and catches `DuplicateKeyError`, returning a clear `409 Conflict` response. Punch-out uses an atomic `update_one` filtered on `punch_out: null`, so it can only be applied once.

### Keeping employees with zero attendance logs in the department summary
A plain aggregation over `attendance_logs` would silently drop employees who never punched in, making departments look healthier than they are. The department summary starts from the `employees` collection and uses `$lookup` to join attendance logs, so employees with zero logs are still counted in headcount and absence figures.

### Scaling approach for large datasets
- Compound indexes cover the main query and aggregation patterns.
- List endpoints use pagination (`skip`/`limit`, or range-based pagination for very deep pages) to bound response size.
- Analytics queries are narrowed by date range with `$match` early in each pipeline.
- Department is denormalized into attendance logs to avoid joins on hot analytics paths.
- For very large volumes, attendance logs can be sharded by `emp_code` or date, and heavy aggregations can be pre-computed into summary collections.

---

## 12. Author

**Your Name**
Backend Developer


- Email: shahbazminto23@gmail.com

---

*Built with FastAPI and MongoDB.*
