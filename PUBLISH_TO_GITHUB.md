# Publishing lbdl to GitHub — Step-by-Step

## Prerequisites
- Git installed: `git --version`
- GitHub account: https://github.com
- GitHub CLI (optional but easy): https://cli.github.com

---

## Option A — GitHub CLI (fastest)

```bash
# 1. Install GitHub CLI if you haven't
#    macOS:   brew install gh
#    Ubuntu:  sudo apt install gh
#    Windows: winget install GitHub.cli

# 2. Login
gh auth login

# 3. Go into the project folder
cd /path/to/lbdl

# 4. Create repo + push in one command
gh repo create lbdl \
  --public \
  --description "ListenBrainz & Invidious playlist downloader with Deezer-like UI" \
  --push \
  --source .

# Done! GitHub will print the repo URL.
```

---

## Option B — Manual (git + github.com)

### Step 1 — Create the repo on GitHub

1. Go to https://github.com/new
2. Fill in:
   - **Repository name**: `lbdl`
   - **Description**: `ListenBrainz & Invidious playlist downloader`
   - **Visibility**: Public (or Private)
   - ❌ Do **NOT** tick "Add a README" or "Add .gitignore" — you already have them
3. Click **Create repository**
4. Copy the HTTPS URL shown: `https://github.com/YOUR_USERNAME/lbdl.git`

### Step 2 — Initialise git locally

```bash
cd /path/to/lbdl

# Initialise (skip if already a git repo)
git init

# Set main as default branch
git branch -M main

# Stage everything
git add .

# First commit
git commit -m "feat: initial release with ListenBrainz + Invidious support"
```

### Step 3 — Push to GitHub

```bash
# Add remote (replace with YOUR URL)
git remote add origin https://github.com/YOUR_USERNAME/lbdl.git

# Push
git push -u origin main
```

---

## Recommended Repository Settings

After pushing, go to your repo on GitHub:

### Topics (makes the repo discoverable)
Settings → scroll down → **Topics**:
```
music  downloader  listenbrainz  invidious  youtube  fastapi  yt-dlp  docker  pwa  self-hosted
```

### About (right sidebar on repo page)
Click the ⚙️ gear next to "About":
- Description: `Self-hosted playlist downloader for ListenBrainz & Invidious — Deezer-like UI, synced lyrics, cover art`
- Website: `http://localhost:8032` or your public URL if deployed
- ✅ Tick "Use your GitHub Pages website"

### Enable GitHub Pages (optional — for a live demo page)
Settings → Pages → Source: **Deploy from a branch** → **main** / `/docs` (or use Actions)

---

## Subsequent Pushes

```bash
# After making changes:
git add .
git commit -m "fix: describe what you changed"
git push
```

## Creating a Release

```bash
# Tag the version
git tag -a v2.0.0 -m "Release v2.0.0 — Invidious support + Deezer UI"
git push origin v2.0.0

# Then on GitHub: Releases → Draft a new release → choose tag v2.0.0
# Paste CHANGELOG.md content into the release notes
```

---

## Docker Hub (optional — so others can `docker pull` without building)

```bash
# Login
docker login

# Build with a tag
docker build -t YOUR_DOCKERHUB_USERNAME/lbdl:latest .
docker build -t YOUR_DOCKERHUB_USERNAME/lbdl:2.0.0 .

# Push
docker push YOUR_DOCKERHUB_USERNAME/lbdl:latest
docker push YOUR_DOCKERHUB_USERNAME/lbdl:2.0.0
```

Then update `compose.yaml` to use `image: YOUR_DOCKERHUB_USERNAME/lbdl:latest`
instead of `build: .` so users don't need to build from source.
