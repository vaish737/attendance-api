# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.



| # | Decision | Why I chose it | How it works | Future consideration |
|---|---|---|---|---|
| 1 | **Indexes** | To improve query performance and prevent duplicate records. | Added a unique `emp_code` index, `(department, emp_code)` and `joined_on` indexes for employees, plus unique `(emp_code, date)`, `(date desc, emp_code)` and `(emp_code, punch_in desc)` indexes for attendance. Skipped the `status` index because it has few distinct values. | Review index usage as data grows. |
| 2 | **Simultaneous punch-ins** | To prevent duplicate attendance records during concurrent requests. | Both requests calculate the IST attendance date and attempt insertion. The unique `(emp_code, date)` index allows one to succeed; `DuplicateKeyError` becomes HTTP 409. No preliminary read is needed. | Test under higher concurrency. |
| 3 | **Leaderboard ties** | To include everyone tied at the requested cutoff. | MongoDB `$rank` assigns equal ranks to tied employees. Filtering with `rank <= limit` after ranking includes all cutoff ties. Results are ordered by late minutes and `emp_code`. | Keep ordering deterministic. |
| 4 | **Employees without logs** | To ensure headcount remains accurate. | Start aggregation from employees who joined by month-end, use `$lookup` for attendance, and preserve unmatched employees with `$unwind`. | Test months with zero attendance records. |
| 5 | **100x data growth** | To avoid repeatedly aggregating large collections. | Maintain monthly per-employee totals in a separate collection, updated after punches and corrections. | Add reconciliation to prevent inconsistent totals. |