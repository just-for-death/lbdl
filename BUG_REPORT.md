# Bug Report: NameError in library_cover_candidates Function

## Issue Summary
**Error:** `NameError: name 'logger' is not defined`  
**Location:** `/app/app/main.py`, line 914 (in the `_do()` function nested within `library_cover_candidates()`)  
**HTTP Endpoint:** `GET /api/library/track/{id}/cover-candidates`  
**Status Code:** 500 Internal Server Error

## Root Cause
The `logger` object is defined at the module level (line 4):
```python
logger = logging.getLogger("lbdl.main")
```

However, when the `_do()` function (a nested function defined inside `library_cover_candidates()`) attempts to access `logger` on line 914, Python raises a `NameError` because the nested function's local scope and the function's local scope don't include `logger`.

### Why This Happens
In Python, when a nested function needs access to a variable from an outer scope, it either:
1. Must be explicitly passed as a parameter
2. Must be declared as `nonlocal` (if modifying)
3. Must be accessed from the enclosing scope (which Python attempts to do via closure)

However, the `_do()` function is defined as a regular nested function without proper closure capture, and the module-level `logger` is not being properly accessed.

## Affected Code Lines
- **Line 914:** `logger.info("[cover-candidates] custom query=%r", q)`
- **Line 922:** `logger.info("[cover-candidates] auto: artist=%r title=%r album=%r", artist, title, album)`
- **Line 1000:** `logger.info("[cover-candidates] found %d unique covers", len(deduped))`

## Solution
The fix is to pass `logger` as a parameter to the `_do()` function since it's executed in a separate thread via `loop.run_in_executor()`. When using thread executors, it's a best practice to pass all required objects explicitly rather than relying on closure.

### Option 1: Pass logger as parameter (Recommended)
```python
def _do(logger=logger):  # Default parameter captures current scope's logger
    # function body remains the same
```

### Option 2: Import logger inside _do()
```python
def _do():
    from app.main import logger  # Import at function level
    # rest of function
```

### Option 3: Store logger in a way accessible from _do()
```python
nonlocal_logger = logger
def _do():
    # use nonlocal_logger
```

## Recommended Fix
Use **Option 1** (default parameter capture) as it's:
- Cleaner and more Pythonic
- Makes thread safety explicit
- Doesn't require additional imports
- Minimal code changes

## Implementation
Replace line 901:
```python
    def _do():
```

With:
```python
    def _do(logger_instance=logger):
```

Then replace all instances of `logger.info()` within `_do()` with `logger_instance.info()`.

Or more simply, just add the parameter without renaming:
```python
    def _do(logger=logger):
```

And keep all `logger.info()` calls as-is, since the parameter will shadow the module-level variable.
