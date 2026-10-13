# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
**Answer**
 I created indexes to improve query performance and maintain data integrity. A unique index on `employees.emp_code` prevents duplicate employees, while a compound index on `attendance_logs(emp_code, date)` prevents duplicate daily attendance records. Other indexes support department, date, and status-based queries. I rejected a separate `emp_code` attendance index because the compound index already covers the required queries.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
**Answer**
If two identical punch-in requests arrive at the same time, both pass the employee existence check and create the attendance document. The unique compound index on `(emp_code, date)` allows only one insert. The first request succeeds with `201 Created`, while the second fails with `DuplicateKeyError` and returns `409 Conflict`. This prevents duplicate attendance records even during concurrent requests.




3. **Ties.**Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
**Answer** 
If two employees are tied on total late minutes at the cutoff of the leaderboard limit, my API returns both employees.

I first sort the employees by `total_late_minutes` in descending order and use `emp_code` in ascending order as a secondary sort to make the ordering deterministic.

Then I assign standard competition ranks. Employees with the same total late minutes receive the same rank. For example, if two employees are tied at rank 10, both receive rank 10.

The important part is that I apply the `limit` **after assigning ranks**, using `item["rank"] <= limit`. This ensures that all employees tied at the cutoff rank are included, even if the response contains more employees than the requested limit.

I chose this approach so that employees with equal lateness are treated fairly and are not arbitrarily excluded because of the limit.



4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
**Answer**
The department summary starts from the `employees` collection instead of `attendance` so employees with no attendance logs are not missed. Using `$lookup` and `preserveNullAndEmptyArrays=True` keeps employees without records. The `headcount` is calculated using `$sum:1`, ensuring every active employee is counted correctly.


5. **One thing you would change** if this had to serve 100x the data.
**Answer**
For 100x more data, I would move heavy analytics from real-time MongoDB aggregation to pre-computed reporting collections. Background jobs would generate summaries and rankings periodically, allowing APIs to read faster. I would also optimize indexes, add pagination, and introduce caching to reduce database load and maintain performance.