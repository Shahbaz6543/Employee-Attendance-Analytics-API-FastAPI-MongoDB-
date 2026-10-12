# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.
| Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|
| `health()` endpoint | Health check only verifies that the API process is running. It does not check MongoDB connectivity, so it can return `200 OK` even when the database is unavailable. | Stop MongoDB and call `/health`. The API still returns `{"status": "ok"}`. | Added MongoDB connectivity check using database ping so the endpoint reports actual service health. |
| `list_employees()` endpoint | Pagination logic is incorrect because `skip = page * page_size` skips the first page of records. | Call `/employees?page=1&page_size=20` and notice that the first 20 employees are missing. | Changed skip calculation to `(page - 1) * page_size`. |
| `create_employee()` endpoint | Duplicate `emp_code` protection only relies on application-level `find_one()` check. Concurrent requests can bypass this check and insert duplicates. | Send two employee creation requests with the same `emp_code` at the same time. | Added a unique MongoDB index on `emp_code` and handled duplicate key errors. |
| `punch_in()` endpoint | Employee existence is not validated before accessing employee fields. Invalid `emp_code` can cause a server error. | Send punch-in request with a non-existing employee code and observe a `500` error. | Added employee validation and return `404 Employee not found`. |
| `punch_in()` endpoint | Duplicate attendance prevention using `find_one()` before `insert_one()` is not safe for concurrent requests. Both requests can pass the check and create duplicate records. | Send two identical punch-in requests simultaneously. | Added a unique compound index on `(emp_code, date)` and handled `DuplicateKeyError`. |
| `punch_in()` endpoint | Timestamp handling uses system local timezone through `datetime.fromtimestamp()`, which can create inconsistent dates in different environments. | Run the API on a server with a different timezone and compare attendance dates. | Standardized timestamp handling using IST timezone-aware datetime values. |
| `list_attendance()` endpoint | The endpoint loads all matching attendance records into memory and performs sorting and pagination in Python. This becomes slow with large datasets. | Insert thousands of attendance records and observe slower response time and high memory usage. | Moved filtering, sorting, and pagination closer to MongoDB using query/aggregation operations. |
| `list_attendance()` endpoint | MongoDB documents contain `_id` fields that are not converted before returning the response, which can cause serialization issues. | Call the endpoint and observe MongoDB ObjectId appearing in the response. | Removed `_id` before returning API response and added response ID handling. |
| Missing analytics endpoints | Required endpoints from the API contract were not implemented in the starter code. | Run API contract tests and observe missing routes returning `404 Not Found`. | Implemented missing endpoints like monthly analytics, department summary, leaderboard, trend, and admin explain APIs. |
| Database indexing | Frequently used query fields did not have indexes, causing slower queries as data grows. | Test queries on large employee and attendance collections and observe increased response time. | Added indexes on `emp_code`, `(emp_code, date)`, department, date, and status fields. |
