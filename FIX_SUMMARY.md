# LBDL Logger NameError - Quick Fix Summary

## Problem
```
NameError: name 'logger' is not defined
Location: /app/app/main.py, line 914 (in library_cover_candidates function)
Endpoint: GET /api/library/track/{id}/cover-candidates
```

## What Was Wrong
The nested function `_do()` inside `library_cover_candidates()` tried to use the module-level `logger` object, but it wasn't accessible in the nested function's scope.

## The Fix
Changed line 901 from:
```python
def _do():
```

To:
```python
def _do(logger=logger):
```

This captures the module-level `logger` as a default parameter, making it available inside the nested function when it runs in a separate thread via `loop.run_in_executor()`.

## Why This Works
- Default parameters are evaluated when the function is *defined* (in the module scope)
- The parameter `logger=logger` takes the module-level logger and assigns it as a default
- When `_do()` is called, this logger is automatically passed, eliminating the NameError
- This is thread-safe and the standard pattern for passing context to thread pool executors

## Files Modified
- `main.py`: Line 901 only

## Testing
The fix resolves the error on these logger calls within `_do()`:
- Line 914: `logger.info("[cover-candidates] custom query=%r", q)`
- Line 922: `logger.info("[cover-candidates] auto: artist=%r title=%r album=%r", ...)`
- Line 1000: `logger.info("[cover-candidates] found %d unique covers", ...)`

All three will now work correctly without NameError.
