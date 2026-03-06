# Setup Instructions - Missing Files

## Problem

Docker build is failing with:
```
target lbdl-sync: failed to solve: failed to compute cache key: 
"/app": not found
```

This happens because the microservices need the original LBDL project files:
- `app/` directory (with library.py, organizer.py, etc.)
- `static/` directory (web UI files)
- `sync.py` (original sync script)

## Solution

The ZIP package only contains the NEW microservices files. You need to merge them with your existing LBDL project.

### Step 1: Prepare Your Project

You should have an existing LBDL installation with:
```
/path/to/lbdl/
├── app/                    ← Already exists
│   ├── main.py
│   ├── library.py
│   ├── organizer.py
│   └── __init__.py
├── static/                 ← Already exists
│   └── index.html
├── sync.py                 ← Already exists
└── requirements.txt        ← Original requirements
```

### Step 2: Extract the ZIP

```bash
cd /path/to/lbdl/
unzip LBDL-Microservices-Complete.zip
```

This will ADD the new files:
```
├── docker-compose.yaml     ← NEW
├── Dockerfile.api          ← NEW
├── Dockerfile.downloader   ← NEW
├── Dockerfile.tagger       ← NEW
├── Dockerfile.sync         ← NEW
├── docker-entrypoint-*.sh  ← NEW (4 files)
├── shared_queue.py         ← NEW
├── downloader_service.py   ← NEW
├── tagger_service.py       ← NEW
├── sync_service.py         ← NEW
├── main.py                 ← UPDATED (logger fix)
└── requirements-updated.txt ← NEW
```

### Step 3: Update Files

#### Replace main.py
```bash
# Backup the original
cp app/main.py app/main.py.backup

# Use the fixed version from the ZIP
cp main.py app/main.py
```

#### Update requirements.txt
```bash
# Backup original
cp requirements.txt requirements.txt.backup

# Replace with updated version (has aio-pika and redis)
cp requirements-updated.txt requirements.txt
```

#### Keep Original Files Intact
DO NOT delete or modify:
- `app/library.py`
- `app/organizer.py`
- `static/` directory
- `sync.py` (original, kept for reference)

### Step 4: Update docker-compose.yaml

Edit `docker-compose.yaml` and change the volume paths from:
```yaml
/mnt/Nazi/lbdl/music:/app/music
/mnt/Nazi/lbdl/config:/app/config
```

To your actual paths:
```yaml
/your/actual/music/path:/app/music
/your/actual/config/path:/app/config
```

This path appears in 4 places (one for each service):
- lbdl-api
- lbdl-downloader
- lbdl-tagger
- lbdl-sync

### Step 5: Verify File Structure

Before building, your directory should have:

```
/path/to/lbdl/
├── app/
│   ├── __init__.py
│   ├── library.py           ✓ Original
│   ├── main.py              ✓ Updated (with logger fix)
│   └── organizer.py         ✓ Original
├── static/
│   └── index.html           ✓ Original
├── docker-compose.yaml      ✓ NEW
├── Dockerfile.api           ✓ NEW
├── Dockerfile.downloader    ✓ NEW
├── Dockerfile.tagger        ✓ NEW
├── Dockerfile.sync          ✓ NEW
├── docker-entrypoint-api.sh
├── docker-entrypoint-downloader.sh
├── docker-entrypoint-tagger.sh
├── docker-entrypoint-sync.sh
├── shared_queue.py          ✓ NEW
├── downloader_service.py    ✓ NEW
├── tagger_service.py        ✓ NEW
├── sync_service.py          ✓ NEW
├── sync.py                  ✓ Original (kept as reference)
└── requirements.txt         ✓ Updated (aio-pika + redis added)
```

### Step 6: Build & Run

Now you can build and start the containers:

```bash
docker compose build
docker compose up -d
```

Verify all services are running:
```bash
docker compose ps
```

You should see 6 services:
- lbdl-api
- lbdl-downloader
- lbdl-tagger
- lbdl-sync
- lbdl-redis
- lbdl-rabbitmq

### Step 7: Access the UI

Open your browser and go to:
```
http://localhost:8032
```

## Troubleshooting

### Build Still Fails: "app: not found"

**Check:** Are you in the right directory?
```bash
pwd  # Should show your LBDL project directory
ls -la app/  # Should list app directory contents
```

**Fix:** Make sure you extracted the zip IN your LBDL project:
```bash
cd /path/to/lbdl/
unzip LBDL-Microservices-Complete.zip
```

### Build Fails: "requirements.txt: not found"

**Check:** Did you copy requirements-updated.txt to requirements.txt?
```bash
cp requirements-updated.txt requirements.txt
```

### Build Fails: "sync.py: not found"

**Check:** Do you have the original sync.py?
```bash
ls -la sync.py
```

**If missing:** That's ok, but the Dockerfile.sync references it. Either:
1. Keep your original sync.py (recommended)
2. Or comment out the COPY line in Dockerfile.sync

The new sync_service.py is the modern async version anyway.

### Containers Start But Errors in Logs

**Check logs:**
```bash
docker compose logs lbdl-api
docker compose logs lbdl-downloader
docker compose logs lbdl-tagger
```

**Common issues:**
- Volume paths wrong (LBDL_DATA_DIR not found)
- Redis/RabbitMQ not ready yet (wait 30 seconds)
- Port 8032 already in use

## What Gets Replaced vs. Added

### These files are UPDATED (from original):
- `main.py` - Logger bug fix (line 901 changed)
- `requirements.txt` - aio-pika and redis added

### These files are NEW (from ZIP):
- docker-compose.yaml
- Dockerfile.* (4 files)
- docker-entrypoint-*.sh (4 files)
- shared_queue.py
- downloader_service.py
- tagger_service.py
- sync_service.py

### These files are UNCHANGED (from original):
- app/library.py
- app/organizer.py
- static/ directory
- sync.py (original)
- config/
- music/ (your data)

## Quick Summary

```bash
# 1. Navigate to your LBDL project
cd /path/to/lbdl

# 2. Extract the ZIP
unzip LBDL-Microservices-Complete.zip

# 3. Update files
cp main.py app/main.py                           # Use fixed version
cp requirements-updated.txt requirements.txt      # Add new deps

# 4. Edit docker-compose.yaml
# Update volume paths (4 places)
nano docker-compose.yaml

# 5. Build and run
docker compose build
docker compose up -d

# 6. Verify
docker compose ps
# Open http://localhost:8032
```

That's it! You're ready to go.

## Still Having Issues?

1. Check the QUICKSTART.md file (in the ZIP)
2. Review DEPLOYMENT_CHECKLIST.md for detailed verification steps
3. Check docker-compose.yaml syntax (YAML indentation matters!)
4. Verify all required directories exist and are accessible
5. Check Docker version: `docker --version` (should be recent)

## Next Steps

Once deployment succeeds:
1. Open http://localhost:8032
2. Library should be visible
3. Add a test playlist
4. Click Sync
5. Watch the downloader logs: `docker compose logs -f lbdl-downloader`

Happy deploying! 🚀
