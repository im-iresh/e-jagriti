# Docker — Offline Setup Guide for Air-Gapped Linux Servers

This guide assumes:
- **Internet machine** — your developer laptop (Windows/Mac/Linux) that has internet
- **Target server** — the air-gapped Linux machine that hosts your services (no internet)
- File transfer between the two is possible via USB drive, LAN SCP, shared folder, etc.

---

## Part 1 — Download Everything on the Internet Machine

### 1.1 Docker static binaries (works on any Linux distro — no package manager needed)

Go to: https://download.docker.com/linux/static/stable/x86_64/

Download the latest tarball, e.g. `docker-27.x.x.tgz`

Or using curl on the internet machine (Linux/Mac):
```bash
# Check https://download.docker.com/linux/static/stable/x86_64/ for the latest version
curl -LO https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz
```

### 1.2 Docker Compose plugin (standalone binary)

Go to: https://github.com/docker/compose/releases/latest

Download `docker-compose-linux-x86_64`

Or:
```bash
curl -LO https://github.com/docker/compose/releases/download/v2.29.1/docker-compose-linux-x86_64
```

### 1.3 Pull and save all Docker images

On the internet machine, install Docker normally (desktop or engine), then:

```bash
# Pull all images the project needs (from docker-compose.yml)
docker pull postgres:15-alpine
docker pull redis:7-alpine

# Build the api image from source (run from the repo root)
docker build -t ejagriti-api:latest ./api

# Save everything into a single tar archive
docker save \
  postgres:15-alpine \
  redis:7-alpine \
  ejagriti-api:latest \
  | gzip > ejagriti_images.tar.gz
```

> If the ingestion service container also needs to run, build and add it:
> ```bash
> docker build -t ejagriti-ingestion:latest ./ingestion
> # add ejagriti-ingestion:latest to the docker save command above
> ```

### 1.4 Copy the project files

You need these files on the server:
```
docker-27.x.x.tgz
docker-compose-linux-x86_64
ejagriti_images.tar.gz
docker-compose.yml          ← from the repo root
.env                        ← your filled-in copy of .env.example
```

---

## Part 2 — Transfer Files to the Air-Gapped Server

**Via USB drive:**
```bash
# On internet machine — copy to USB
cp docker-27.x.x.tgz docker-compose-linux-x86_64 ejagriti_images.tar.gz /media/usb/

# On server — copy from USB (mount path varies, use 'lsblk' to find it)
cp /media/usb/* /opt/docker-setup/
```

**Via SCP (if machines are on the same LAN):**
```bash
scp docker-27.x.x.tgz docker-compose-linux-x86_64 ejagriti_images.tar.gz \
  user@192.168.x.x:/opt/docker-setup/
```

---

## Part 3 — Install Docker on the Air-Gapped Server

SSH into the server, then:

```bash
cd /opt/docker-setup

# Extract the binaries
tar xzvf docker-27.x.x.tgz

# Move all binaries to /usr/local/bin (on PATH for all users)
sudo cp docker/* /usr/local/bin/

# Verify
docker --version
```

### 3.1 Create the Docker system group and data directory

```bash
sudo groupadd docker
sudo usermod -aG docker $USER       # lets your user run docker without sudo
sudo mkdir -p /var/lib/docker
```

### 3.2 Create the systemd service so Docker starts on boot

```bash
sudo tee /etc/systemd/system/docker.service > /dev/null << 'EOF'
[Unit]
Description=Docker Application Container Engine
After=network-online.target firewalld.service containerd.service
Wants=network-online.target
Requires=docker.socket

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutStartSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
Delegate=yes
KillMode=process
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/docker.socket > /dev/null << 'EOF'
[Unit]
Description=Docker Socket for the API

[Socket]
ListenStream=/var/run/docker.sock
SocketMode=0660
SocketUser=root
SocketGroup=docker

[Install]
WantedBy=sockets.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now docker.socket
sudo systemctl enable --now docker.service
sudo systemctl status docker           # should say: active (running)
```

> **Log out and back in** after `usermod -aG docker` for the group to take effect.

### 3.3 Install Docker Compose

```bash
sudo cp docker-compose-linux-x86_64 /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
docker-compose --version
```

---

## Part 4 — Load Images and Run the Stack

```bash
# Load all images from the archive (takes a minute — it's unpacking layers)
docker load -i /opt/docker-setup/ejagriti_images.tar.gz

# Confirm they are loaded
docker images
# REPOSITORY             TAG           IMAGE ID       SIZE
# ejagriti-api           latest        ...
# postgres               15-alpine     ...
# redis                  7-alpine      ...
```

### 4.1 Place the project files

```bash
mkdir -p /opt/ejagriti
cp /opt/docker-setup/docker-compose.yml /opt/ejagriti/
cp /opt/docker-setup/.env /opt/ejagriti/
cd /opt/ejagriti
```

### 4.2 Edit docker-compose.yml to use pre-built images

The `docker-compose.yml` currently has `build:` blocks. Since you have no internet and the images are already loaded, replace `build:` with `image:` so compose doesn't try to rebuild:

```bash
# In /opt/ejagriti/docker-compose.yml, change the api service from:
#   build:
#     context: ./api
#     dockerfile: Dockerfile
# to:
#   image: ejagriti-api:latest

# Same for ingestion:
#   image: ejagriti-ingestion:latest
```

You can do this with sed:
```bash
# Preview first
grep -n "build:" docker-compose.yml

# Then edit manually with nano/vi, or use sed (adjust service names as needed):
# This is just a reminder — edit the file directly, it's safer
nano docker-compose.yml
```

### 4.3 Fill in .env and start the stack

```bash
# Open and fill in the real values
nano /opt/ejagriti/.env

# Start everything (detached — runs in background)
docker-compose up -d

# Check all containers are running
docker-compose ps

# Watch live logs
docker-compose logs -f api
```

API is now reachable at `http://server-ip:8000`

---

## Part 5 — Day-to-Day Operations Cheat Sheet

```bash
# All commands run from /opt/ejagriti/

# --- Status ---
docker-compose ps                    # list containers + status
docker-compose logs api              # last logs for api
docker-compose logs -f api           # follow logs live (Ctrl+C to exit)
docker stats                         # live CPU/memory usage for all containers

# --- Stop / Start ---
docker-compose stop                  # graceful stop (keeps containers)
docker-compose start                 # restart stopped containers
docker-compose restart api           # restart just the api container
docker-compose down                  # stop AND remove containers (data volume preserved)

# --- Updating the API (when you get a new build) ---
# On internet machine: build and save the new image
docker build -t ejagriti-api:latest ./api
docker save ejagriti-api:latest | gzip > ejagriti-api-new.tar.gz
# Transfer ejagriti-api-new.tar.gz to server, then:
docker load -i ejagriti-api-new.tar.gz
docker-compose up -d --no-deps api   # recreate only the api container, leave DB/Redis alone

# --- Database access ---
docker-compose exec postgres psql -U ejagriti -d ejagriti

# --- Shell inside a container (debugging) ---
docker-compose exec api sh
docker-compose exec postgres sh

# --- Clean up old/unused images (free disk space) ---
docker image prune -f
```

---

## Part 6 — Running Multiple Services on the Same Machine

Since the machine already hosts 5–6 services, you have two options:

### Option A — Each service has its own docker-compose.yml (recommended)

```
/opt/ejagriti/       docker-compose.yml   (ports: 8000)
/opt/service-b/      docker-compose.yml   (ports: 8001)
/opt/service-c/      docker-compose.yml   (ports: 8002)
```

Each stack is fully independent. Run `docker-compose up -d` inside each folder.
Use different host ports (8000, 8001 …) — map them in each compose file:
```yaml
ports:
  - "8001:8000"   # host:container
```

### Option B — One shared docker-compose.yml for all services

All services defined in a single file. Works, but harder to manage independently.

---

## Part 7 — Persisting Data Across Restarts

`docker-compose down` removes containers but **named volumes survive** (your postgres data is safe).

To also wipe the database:
```bash
docker-compose down -v     # WARNING: deletes the postgres_data volume — all data gone
```

To back up the database:
```bash
docker-compose exec postgres pg_dump -U ejagriti ejagriti > backup_$(date +%F).sql
```

To restore:
```bash
cat backup_2025-01-01.sql | docker-compose exec -T postgres psql -U ejagriti -d ejagriti
```

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `docker: command not found` | `echo $PATH` — ensure `/usr/local/bin` is in it |
| `permission denied /var/run/docker.sock` | Log out and back in after `usermod -aG docker` |
| Container exits immediately | `docker-compose logs <service>` — read the error |
| Port already in use | `ss -tlnp \| grep 8000` — another process owns the port |
| `image not found` error on `up` | Did you run `docker load`? Check `docker images` |
| Database container healthy but API can't connect | Ensure `DATABASE_URL` in `.env` uses service name `postgres`, not `localhost` |
