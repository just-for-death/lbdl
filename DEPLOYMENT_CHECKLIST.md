# LBDL Microservices - Deployment Checklist

## Pre-Deployment

### System Requirements
- [ ] Docker & Docker Compose installed
- [ ] 4GB+ RAM available
- [ ] 1GB+ free disk space for services
- [ ] Ports available: 8032 (API), 5672 (RabbitMQ), 6379 (Redis)

### File Preparation
- [ ] Copy all files from outputs to project directory
- [ ] Verify docker-compose.yaml exists
- [ ] Verify all Dockerfile.* files present (4 files)
- [ ] Verify all service .py files present (4 files)
- [ ] Verify all docker-entrypoint-*.sh files present (4 files)
- [ ] Verify requirements-updated.txt exists

### Configuration
- [ ] Update docker-compose.yaml with correct volume paths
  - [ ] Change `/mnt/Nazi/lbdl` to your actual path (appears in 4 places)
  - [ ] Verify `lbdl-api` volumes
  - [ ] Verify `lbdl-downloader` volumes
  - [ ] Verify `lbdl-tagger` volumes
  - [ ] Verify `lbdl-sync` volumes
- [ ] Update requirements.txt
  - [ ] `cp requirements-updated.txt requirements.txt`
- [ ] Backup existing data (optional but recommended)
  - [ ] Backup music directory
  - [ ] Backup config directory

### Optional Configurations
- [ ] AcoustID API Key (for fingerprinting)
  - [ ] Register at https://acoustid.org
  - [ ] Add to docker-compose.yaml environment
- [ ] ListenBrainz Token (for playlist syncing)
  - [ ] Get token from https://listenbrainz.org/
  - [ ] Add to docker-compose.yaml environment
- [ ] Adjust worker concurrency (if needed)
  - [ ] WORKER_CONCURRENCY for downloader (default: 3)
  - [ ] WORKER_CONCURRENCY for tagger (default: 2)
- [ ] Adjust cron schedule (if needed)
  - [ ] LBDL_SCHEDULER_CRON (default: `0 */2 * * *`)

---

## Build Phase

### Building Images
- [ ] Run: `docker compose build`
  - [ ] API image builds successfully
  - [ ] Downloader image builds successfully
  - [ ] Tagger image builds successfully
  - [ ] Sync image builds successfully
  - [ ] No build errors in output

### Image Verification
- [ ] Check images created: `docker images | grep lbdl`
  - [ ] lbdl-api exists
  - [ ] lbdl-downloader exists
  - [ ] lbdl-tagger exists
  - [ ] lbdl-sync exists

---

## Startup Phase

### Services Starting
- [ ] Run: `docker compose up -d`
- [ ] Check all services running: `docker compose ps`
  - [ ] lbdl-api status: Up or healthy
  - [ ] lbdl-downloader status: Up
  - [ ] lbdl-tagger status: Up
  - [ ] lbdl-sync status: Up
  - [ ] lbdl-redis status: Up (healthy)
  - [ ] lbdl-rabbitmq status: Up (healthy)

### Service Health Checks
- [ ] API health: `curl http://localhost:8032/api/health`
  - [ ] Returns 200 OK
- [ ] Redis health: `docker compose exec lbdl-redis redis-cli ping`
  - [ ] Returns PONG
- [ ] RabbitMQ health: `docker compose exec lbdl-rabbitmq rabbitmqctl status`
  - [ ] Returns status info

### Log Verification
- [ ] Check API logs: `docker compose logs lbdl-api | head -50`
  - [ ] No critical errors
  - [ ] "Uvicorn running on" message
- [ ] Check Downloader logs: `docker compose logs lbdl-downloader | head -20`
  - [ ] "Download worker started" message
  - [ ] No connection errors
- [ ] Check Tagger logs: `docker compose logs lbdl-tagger | head -20`
  - [ ] "Tagging worker started" message
  - [ ] No connection errors
- [ ] Check Redis logs: `docker compose logs lbdl-redis | head -20`
  - [ ] Ready to accept connections
- [ ] Check RabbitMQ logs: `docker compose logs lbdl-rabbitmq | head -30`
  - [ ] Boot complete
  - [ ] Ready for connections

---

## UI Testing Phase

### Web Interface
- [ ] Navigate to http://localhost:8032
  - [ ] Page loads within 5 seconds
  - [ ] UI renders completely
  - [ ] No JavaScript errors in browser console
- [ ] Verify sections visible
  - [ ] Library view loads
  - [ ] Playlists section visible
  - [ ] Settings accessible
  - [ ] Status/stats panel shows

### Library Scan
- [ ] Library scan runs automatically (wait 30 seconds)
  - [ ] "Scanning..." indicator appears
  - [ ] Scan completes
  - [ ] Existing music appears in library

### Settings
- [ ] Open Settings
  - [ ] Form loads without errors
  - [ ] Can modify settings (try changing cron)
  - [ ] Save button works
  - [ ] Settings persist after reload

---

## First Sync Test

### Manual Sync (if available via UI)
- [ ] Add test playlist (any public ListenBrainz playlist)
- [ ] Click "Sync" button
  - [ ] Page responds immediately (doesn't freeze)
  - [ ] Playlist appears in list
  - [ ] Sync appears to complete quickly

### Monitor Queue
- [ ] Check RabbitMQ queue: `docker compose exec lbdl-rabbitmq rabbitmqctl list_queues`
  - [ ] Should show queues with message counts
  - [ ] download queue has messages (if just synced)
- [ ] Watch downloader: `docker compose logs -f lbdl-downloader`
  - [ ] Messages appear showing download progress
  - [ ] No errors

### Monitor Tagger
- [ ] Once download starts, watch: `docker compose logs -f lbdl-tagger`
  - [ ] Eventually shows tagging messages
  - [ ] Metadata being fetched

### Verify State
- [ ] Check Redis state: `docker compose exec lbdl-redis redis-cli`
  - [ ] `KEYS "lbdl:track:*"` returns track keys
  - [ ] `GET "lbdl:track:{track_id}"` returns JSON state

---

## Load Test Phase (Optional)

### Stress Testing
- [ ] Add 10+ track playlist
- [ ] Monitor resource usage: `docker stats`
  - [ ] No service crashes
  - [ ] Memory usage stable
  - [ ] CPU reasonable
- [ ] Verify all tracks process
  - [ ] Check final queue depth: `rabbitmqctl list_queues`
  - [ ] Should be empty or near-empty
- [ ] Check library updates
  - [ ] All tracks appear
  - [ ] Tags are correct
  - [ ] Cover art embedded (if configured)

---

## Production Verification

### File Organization
- [ ] Music directory structure:
  - [ ] Artists created: `/app/music/{artist}/`
  - [ ] Albums organized: `/app/music/{artist}/{year} - {album}/`
  - [ ] Tracks named: `/app/music/{artist}/{album}/{track_num} {title}.opus`

### Metadata Quality
- [ ] Sample tracks:
  - [ ] ID3/Vorbis tags present
  - [ ] Artist correct
  - [ ] Title correct
  - [ ] Album correct
  - [ ] Cover art embedded (verify with audio player)

### Error Handling
- [ ] Simulate download error:
  - [ ] Turn off network briefly
  - [ ] Check Redis shows failed status
  - [ ] Track doesn't cause system crash
- [ ] Simulate invalid track:
  - [ ] Add non-existent video ID
  - [ ] Check logs show error
  - [ ] System continues processing

### Logging
- [ ] Logs contain useful info
  - [ ] Download logs show file paths
  - [ ] Tagging logs show metadata source
  - [ ] Timestamps present for debugging

---

## Performance Validation

### Timing Tests
- [ ] Single track download: Record time
  - [ ] Typical: 60-120 seconds
  - [ ] Log shows progress
- [ ] Single track tagging: Record time
  - [ ] Typical: 10-30 seconds
  - [ ] AcoustID/MB lookups logged
- [ ] Batch processing (5 tracks): Record total time
  - [ ] Expect: ~2-5 minutes (parallel)
  - [ ] Not: ~12-15 minutes (sequential)

### Concurrency Test
- [ ] Check 3 downloads in parallel:
  - [ ] Add 5-track playlist
  - [ ] Watch logs show 3 downloading simultaneously
  - [ ] Next worker picks up after one completes
- [ ] Check 2 tagging in parallel:
  - [ ] Watch logs show 2 tagging simultaneously
  - [ ] No queue starvation

### Memory Usage
- [ ] `docker stats` shows per-service memory:
  - [ ] API: < 300MB
  - [ ] Each downloader: < 150MB
  - [ ] Each tagger: < 200MB
  - [ ] Redis: < 100MB
  - [ ] RabbitMQ: < 100MB

---

## Monitoring Setup (Optional)

### Log Aggregation
- [ ] Set up log collection (optional):
  - [ ] `docker compose logs` command works
  - [ ] Can grep specific service logs
  - [ ] Log rotation configured (if external)

### Health Dashboard (Optional)
- [ ] Create monitoring script:
  - [ ] Checks all services running
  - [ ] Checks queue depths
  - [ ] Checks Redis state
  - [ ] Alerts on issues

### Alerting (Optional)
- [ ] Configure Gotify (if available):
  - [ ] URL configured in settings
  - [ ] Token configured
  - [ ] Test notification sent
- [ ] Email alerts (if available):
  - [ ] SMTP configured
  - [ ] Test email sent

---

## Rollback Plan

### Backup & Recovery
- [ ] Music backups verified
  - [ ] Original files safe
  - [ ] Can restore if needed
- [ ] Config backups verified
  - [ ] playlists.json backed up
  - [ ] settings.json backed up
- [ ] Know how to rollback:
  - [ ] `docker compose down`
  - [ ] Switch back to old compose file
  - [ ] `docker compose up -d`

---

## Post-Deployment Optimization

### Tuning
- [ ] If downloads too slow: Increase WORKER_CONCURRENCY
  - [ ] Edit docker-compose.yaml
  - [ ] Run: `docker compose up -d lbdl-downloader`
- [ ] If memory issues: Decrease WORKER_CONCURRENCY
  - [ ] Edit docker-compose.yaml
  - [ ] Run: `docker compose up -d`
- [ ] If metadata mismatches: Configure AcoustID key
  - [ ] Add to environment
  - [ ] Restart: `docker compose restart lbdl-tagger`

### Regular Maintenance
- [ ] Weekly: Check logs for errors
- [ ] Weekly: Verify queue depths normal
- [ ] Monthly: Check disk usage
- [ ] Monthly: Review failed tracks

---

## Handoff Checklist

### Documentation
- [ ] User has QUICKSTART.md
- [ ] User has ARCHITECTURE.md
- [ ] User has troubleshooting guide
- [ ] User knows where to find logs

### Support
- [ ] User knows how to check service status
- [ ] User knows how to view logs
- [ ] User knows how to restart services
- [ ] User has emergency contact

### Automation
- [ ] Cron schedule configured
- [ ] Auto-sync running on schedule
- [ ] Monitoring in place
- [ ] Alerting configured (if applicable)

---

## Final Sign-Off

- [ ] All services running and healthy
- [ ] UI accessible and functional
- [ ] First sync completed successfully
- [ ] Files organized correctly
- [ ] Tags applied correctly
- [ ] No critical errors in logs
- [ ] Performance acceptable
- [ ] User trained on system
- [ ] Documentation provided
- [ ] Rollback plan documented

---

**Deployment Status: ✅ COMPLETE**

Date: ________________
Deployed by: ________________
Verified by: ________________

---

## Quick Reference

### Emergency Commands

```bash
# Check status
docker compose ps

# View logs
docker compose logs -f

# Restart all services
docker compose restart

# Restart specific service
docker compose restart lbdl-downloader

# Hard reset (dangerous!)
docker compose down
docker compose up -d

# Check queue depth
docker compose exec lbdl-rabbitmq rabbitmqctl list_queues

# Check Redis
docker compose exec lbdl-redis redis-cli KEYS "lbdl:*"

# Clear stuck state (nuclear option)
docker compose exec lbdl-redis redis-cli FLUSHDB
```

### Key Endpoints

- Web UI: http://localhost:8032
- API: http://localhost:8032/api/
- API Docs: http://localhost:8032/docs
- RabbitMQ: (internal port 5672)
- Redis: (internal port 6379)

---

**Good luck with your deployment! 🚀**
