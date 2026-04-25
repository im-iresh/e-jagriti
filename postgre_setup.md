# PostgreSQL 16 Primary / Replica Setup Guide
### For Ubuntu 22.04 — Air-Gapped (No Internet) Production Machines

---

## Before You Begin — Understanding What We're Setting Up

### What is a Primary/Replica setup?

Think of it like this:
- The **Primary** server is where your application writes data (INSERT, UPDATE, DELETE).
- The **Replica** server is a live, read-only copy of the primary. It receives every change the primary makes, almost instantly.

This gives you two things:
1. **High Availability** — if the primary crashes, you can promote the replica to become the new primary and keep running.
2. **Read Scaling** — your application can send read-only queries (SELECT) to the replica, reducing load on the primary.

### PostgreSQL Version for This Project

Your project uses:
- `psycopg2-binary==2.9.9` — supports PostgreSQL **9.4 to 16**
- `SQLAlchemy==2.0.30` — requires PostgreSQL **9.6 minimum**
- Your schema (partial indexes, `plpgsql` triggers) — requires **PostgreSQL 9.6+**

**Minimum: PostgreSQL 12. This guide uses PostgreSQL 16** (the latest stable, recommended for production).

### How PostgreSQL replication works (simplified)

PostgreSQL records every change to the database in a log called the **WAL** (Write-Ahead Log). Think of it as a detailed diary of every action taken. The replica connects to the primary and continuously reads this diary, replaying each entry to keep itself in sync. This is called **streaming replication**.

---

## Our Setup

| Role    | IP Address    | Hostname     |
|---------|---------------|--------------|
| Primary | 192.168.1.10  | pg-primary   |
| Replica | 192.168.1.20  | pg-replica   |

> **Replace these IPs with your actual server IPs before running any command.**

---

## Phase 1 — Download Packages on an Internet-Connected Machine

Because your production servers have no internet access, you must download all required files on a separate machine that *does* have internet, then physically transfer them.

### 1.1 — Why we use Docker here

We need to download packages that are compatible with Ubuntu 22.04. The safest way is to download them *from inside* a matching Ubuntu 22.04 environment. Docker lets us do that without needing a second Ubuntu machine.

If you don't have Docker, any Ubuntu 22.04 machine (laptop, VM) will work the same way — just skip the `docker run` line and run the commands directly.

### 1.2 — Start a matching Ubuntu 22.04 environment

```bash
# This creates a temporary Ubuntu 22.04 container.
# /tmp/pg16-debs on your machine is shared with /output inside the container.
docker run --rm -it -v /tmp/pg16-debs:/output ubuntu:22.04 bash
```

You are now *inside* the container. All commands below run inside it.

```bash
# Update the package list and install basic tools we need
apt-get update
apt-get install -y wget gnupg curl apt-utils dpkg-dev
```

### 1.3 — Add the official PostgreSQL repository

PostgreSQL maintains their own apt repository (called PGDG) that always has the latest versions. The Ubuntu default repo often has older versions.

```bash
# Download the PGDG signing key (this proves packages are genuine, not tampered)
wget -O /tmp/pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc

# IMPORTANT: Verify the key fingerprint matches exactly before continuing.
# Run this and check the output:
gpg --show-keys /tmp/pgdg.asc
# You should see: B97B 0AFC AA1A 47F0 44F2  44A0 7FCC 7D46 ACCC 4CF8
# If it's different, stop — the key may be compromised.

# Install the key into the system's trusted keyring
install -d /usr/share/keyrings
gpg --dearmor -o /usr/share/keyrings/pgdg.gpg /tmp/pgdg.asc

# Tell apt where to find PostgreSQL packages
# "jammy" is Ubuntu 22.04's codename
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list

apt-get update
```

### 1.4 — Download all packages

```bash
mkdir -p /output/debs

# --download-only tells apt: figure out what's needed but don't install anything,
# just download the .deb files.
# This includes postgresql-16 AND all its dependencies automatically.
apt-get install --download-only --reinstall -y \
  postgresql-16 \
  postgresql-client-16 \
  postgresql-common \
  postgresql-client-common \
  libpq5 \
  libpq-dev \
  sysstat \
  net-tools

# Copy all downloaded .deb files to our output folder
cp /var/cache/apt/archives/*.deb /output/debs/

# Also save the GPG key files — we need them on the production servers
cp /tmp/pgdg.asc /output/pgdg.asc
cp /usr/share/keyrings/pgdg.gpg /output/pgdg.gpg
```

### 1.5 — Create a local package index

This index file tells apt how to find packages from our local folder (instead of the internet).

```bash
cd /output/debs
dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
```

### 1.6 — Create checksum file for integrity verification

A checksum is like a fingerprint for a file. If even one byte changes during transfer, the fingerprint will be different. We create checksums now, then verify them on the production servers to make sure nothing was corrupted during transfer.

```bash
cd /output
sha256sum debs/*.deb pgdg.asc pgdg.gpg > SHA256SUMS

# You should see a list of files each with a long hash next to them
cat SHA256SUMS
```

### 1.7 — Bundle everything into a single file

```bash
tar -czf pg16-airgap-bundle.tar.gz -C /output .

# Exit the Docker container
exit
```

Your bundle is now at `/tmp/pg16-debs/pg16-airgap-bundle.tar.gz` on your internet-connected machine.

### 1.8 — Transfer to both production servers

Use whichever method is available to you (USB drive, SCP from a jump host, shared network drive, etc.):

```bash
# If you can reach the servers via SSH:
scp /tmp/pg16-debs/pg16-airgap-bundle.tar.gz admin@192.168.1.10:/tmp/
scp /tmp/pg16-debs/pg16-airgap-bundle.tar.gz admin@192.168.1.20:/tmp/

# If using a USB drive, copy the file to the drive and plug it into each server.
# The file will usually appear at /media/usb/ or /mnt/usb/ on the server.
```

---

## Phase 2 — Install PostgreSQL on BOTH Servers

> Run every command in this phase on **both** the primary (192.168.1.10) **and** the replica (192.168.1.20), one server at a time. SSH into each server and follow the same steps.

### 2.1 — Extract the bundle

```bash
# Create a folder to work in
mkdir -p /opt/pg16-install

# Extract the bundle
tar -xzf /tmp/pg16-airgap-bundle.tar.gz -C /opt/pg16-install

# Go into that folder
cd /opt/pg16-install
```

### 2.2 — Verify file integrity

This is the checksum verification step. Every file must pass.

```bash
sha256sum -c SHA256SUMS
```

Expected output: every line ends with `OK`, like:
```
debs/postgresql-16_16.3-1.pgdg22.04+1_amd64.deb: OK
debs/postgresql-client-16_16.3-1.pgdg22.04+1_amd64.deb: OK
...
```

> **If any line says FAILED, stop immediately.** The file was corrupted during transfer. Re-transfer the bundle and try again. Installing corrupted packages can cause hard-to-debug problems later.

### 2.3 — Register the PostgreSQL signing key

```bash
install -d /usr/share/keyrings
cp /opt/pg16-install/pgdg.gpg /usr/share/keyrings/pgdg.gpg
chmod 644 /usr/share/keyrings/pgdg.gpg
```

### 2.4 — Configure apt to use the local package folder

Instead of downloading from the internet, we point apt at our local folder.

```bash
echo "deb [trusted=yes] file:///opt/pg16-install/debs ./" \
  > /etc/apt/sources.list.d/pg16-local.list

# Refresh apt so it knows about our local packages
apt-get update
```

### 2.5 — Install PostgreSQL 16

```bash
apt-get install -y postgresql-16 postgresql-client-16 postgresql-common sysstat
```

This will install PostgreSQL from the local .deb files. You will not need internet.

> **If apt-get fails**, use the direct fallback method:
> ```bash
> cd /opt/pg16-install/debs
> dpkg -i *.deb
> apt-get install -f -y   # Fix any unresolved dependency errors
> ```

### 2.6 — Verify the installation

```bash
pg_lsclusters
```

Expected output:
```
Ver  Cluster  Port  Status  Owner     Data directory                Log file
16   main     5432  online  postgres  /var/lib/postgresql/16/main   /var/log/postgresql/...
```

```bash
psql --version
# Should print: psql (PostgreSQL) 16.x
```

### 2.7 — Stop PostgreSQL before we configure it

We need to change several configuration files before PostgreSQL starts handling connections. Always stop it first so there are no conflicts.

```bash
systemctl stop postgresql

# Confirm it stopped
systemctl status postgresql
# Should say: inactive (dead)
```

---

## Phase 3 — Configure the PRIMARY Server (192.168.1.10)

> SSH into **192.168.1.10** and run the commands in this phase.

### Understanding the config files

PostgreSQL has two main configuration files:
- **`postgresql.conf`** — controls *how* PostgreSQL behaves (memory, connections, replication settings, etc.)
- **`pg_hba.conf`** — controls *who* is allowed to connect and how they must authenticate. HBA stands for "Host-Based Authentication".

Both files live in `/etc/postgresql/16/main/`.

### 3.1 — Edit postgresql.conf

Open the file:
```bash
nano /etc/postgresql/16/main/postgresql.conf
```

Find each setting listed below (use `Ctrl+W` in nano to search), uncomment it (remove the `#` at the start of the line), and set the value. If a line doesn't exist, add it at the bottom of the file.

```ini
# --- Who can connect ---
# '*' means listen on all network interfaces, so the replica can connect.
# Alternatively, set it to your primary's own IP: '192.168.1.10'
listen_addresses = '*'
port = 5432

# --- Replication settings ---

# wal_level controls how much information is written to the WAL log.
# 'minimal' (the default) does NOT include enough info for replication.
# 'replica' is what we need — it writes enough for a standby server to follow along.
# WARNING: if you leave this as 'minimal', replication will silently fail.
wal_level = replica

# How many replica connections are allowed simultaneously.
# We need at least 2: one for the live replica stream, one for the initial
# base backup operation (pg_basebackup uses a second connection).
max_wal_senders = 5

# How much WAL history to keep on disk (in megabytes).
# If the replica falls behind (e.g. it restarted), it needs to catch up by
# reading old WAL entries. If those entries were already deleted from disk,
# the replica can't reconnect and you have to start over.
# 1024 MB = 1 GB is a safe starting point.
wal_keep_size = 1024

# Allow the replica to serve read-only queries to your application.
# This setting on the primary has no effect on the primary itself,
# but it must be 'on' for the replica to allow read queries.
hot_standby = on

# --- Logging (helps you see what's happening) ---
log_replication_commands = on
```

Save and exit: `Ctrl+X`, then `Y`, then `Enter`.

### 3.2 — Edit pg_hba.conf

This file controls who can connect to PostgreSQL and how they must prove their identity. Rules are read **top to bottom** — the first matching rule wins.

```bash
nano /etc/postgresql/16/main/pg_hba.conf
```

Scroll to the bottom of the file (past the existing rules) and **add this line**:

```
# Allow the replica server to connect for streaming replication.
# The word 'replication' here is a special keyword — it does NOT mean
# a database called "replication". It grants the right to stream WAL data.
# Replace 192.168.1.20/32 with your actual replica IP.
host    replication     replicator      192.168.1.20/32         scram-sha-256
```

> **Do not delete the existing lines** in pg_hba.conf — they allow the postgres system user to connect locally, which is needed for administration.

Save and exit: `Ctrl+X`, then `Y`, then `Enter`.

### 3.3 — Create the replication user

PostgreSQL needs a dedicated database user for the replica to authenticate as. We'll call it `replicator`.

First, start PostgreSQL temporarily so we can run SQL:
```bash
systemctl start postgresql
```

Now connect to the database as the `postgres` superuser:
```bash
sudo -u postgres psql
```

You are now in the `psql` prompt (looks like `postgres=#`). Run:

```sql
-- Create the replication user.
-- REPLICATION privilege is what allows this user to stream WAL data.
-- It does NOT give access to your application's tables.
CREATE ROLE replicator WITH
  LOGIN
  REPLICATION
  ENCRYPTED PASSWORD 'StrongRepl1cationPass!';

-- Change this password to something strong before going to production.

-- Verify the user was created correctly:
\du replicator
-- You should see: replicator | Replication
```

Exit the psql prompt:
```sql
\q
```

### 3.4 — Create a replication slot

A **replication slot** is a bookmark on the primary that says: "Don't delete WAL entries until the replica with this slot has read them."

Without a slot, if your replica goes offline for any reason (restart, network issue), the primary might delete the WAL entries the replica needs to catch up. With a slot, the primary holds onto those entries until the replica reconnects.

```bash
sudo -u postgres psql
```

```sql
SELECT pg_create_physical_replication_slot('replica_slot_1');

-- Verify:
SELECT slot_name, slot_type, active FROM pg_replication_slots;
-- You should see: replica_slot_1 | physical | f
-- 'f' (false) means no replica is connected yet — that's expected

\q
```

> **Important warning about slots:** A replication slot that is never consumed will cause WAL to accumulate on the primary's disk indefinitely. If your replica is offline for many days, your primary's disk can fill up and PostgreSQL will stop accepting writes entirely. Always monitor this (covered in Phase 7).

### 3.5 — Restart the primary and verify settings

```bash
systemctl restart postgresql

# Check that replication settings are active
sudo -u postgres psql -c "SHOW wal_level;"
# Must show: replica

sudo -u postgres psql -c "SHOW max_wal_senders;"
# Must show: 5

sudo -u postgres psql -c "SELECT pg_is_in_recovery();"
# Must show: f  (meaning: this server is NOT in recovery — it's the primary)
```

---

## Phase 4 — Configure the REPLICA Server (192.168.1.20)

> SSH into **192.168.1.20** and run the commands in this phase.

### 4.1 — Wipe the default data directory

When PostgreSQL was installed, it created an empty database cluster in `/var/lib/postgresql/16/main/`. We need to replace this entirely with a copy of the primary's data using `pg_basebackup`.

The tool will refuse to run if the destination folder has any files in it, so we must empty it first.

```bash
systemctl stop postgresql

# Wipe all contents of the data directory
# WARNING: triple-check you are on the REPLICA (192.168.1.20) before running this
rm -rf /var/lib/postgresql/16/main/*

# Confirm it is empty
ls /var/lib/postgresql/16/main/
# Should show nothing (empty output)
```

### 4.2 — Clone the primary using pg_basebackup

`pg_basebackup` connects to the primary and copies its entire data directory to the replica. This is the initial "snapshot" the replica needs before it can start streaming changes.

```bash
sudo -u postgres pg_basebackup \
  --host=192.168.1.10 \
  --port=5432 \
  --username=replicator \
  --pgdata=/var/lib/postgresql/16/main \
  --wal-method=stream \
  --slot=replica_slot_1 \
  --checkpoint=fast \
  --progress \
  --verbose
```

**What each flag means:**
- `--host` — the IP of the primary server
- `--username` — the replication user we created
- `--pgdata` — where to store the data on this replica
- `--wal-method=stream` — streams WAL data in real-time during the backup so the replica is immediately up-to-date when the backup finishes
- `--slot=replica_slot_1` — uses our replication slot (protects WAL on primary while backup is in progress)
- `--checkpoint=fast` — tells the primary to take a checkpoint immediately (faster backup start; without this you may wait minutes)
- `--progress` — shows a progress bar
- `--verbose` — shows what files are being copied

When prompted:
```
Password:
```
Type: `StrongRepl1cationPass!` (or whatever password you set)

This may take a few minutes depending on how much data the primary has. You will see files being copied. Wait for it to finish.

**If it fails with "could not connect to the server":**
1. Make sure the primary is running: `systemctl status postgresql` (on the primary)
2. Make sure the replica IP is correct in the primary's `pg_hba.conf`
3. Test network connectivity: `nc -zv 192.168.1.10 5432` (from the replica)
4. Check that `listen_addresses = '*'` is set in the primary's `postgresql.conf`

### 4.3 — Create the standby signal file

This is a small but critical step. PostgreSQL 12 and later uses a file called `standby.signal` to know that a server should start in "standby mode" (as a replica that follows a primary) rather than as a standalone primary.

**If this file is missing, PostgreSQL will start as an independent primary — which means you'll have two writable primaries that immediately start diverging. This is called split-brain and is very hard to recover from.**

```bash
touch /var/lib/postgresql/16/main/standby.signal

# Set correct ownership (PostgreSQL process runs as the 'postgres' OS user)
chown postgres:postgres /var/lib/postgresql/16/main/standby.signal
chmod 600 /var/lib/postgresql/16/main/standby.signal

# Verify the file exists
ls -la /var/lib/postgresql/16/main/standby.signal
# Should show: -rw------- 1 postgres postgres 0 ... standby.signal
```

### 4.4 — Configure the replica's postgresql.conf

`pg_basebackup` copied the primary's `postgresql.conf` to the replica. Now we need to add replica-specific settings that tell it *where to find the primary and how to connect*.

```bash
nano /etc/postgresql/16/main/postgresql.conf
```

Add these lines at the **bottom of the file**:

```ini
# --- Replica Connection Settings ---

# primary_conninfo tells this replica where to find the primary and how to connect.
# This is the most important setting on the replica.
primary_conninfo = 'host=192.168.1.10 port=5432 user=replicator password=StrongRepl1cationPass! application_name=pg-replica sslmode=prefer'

# The replication slot name we created on the primary.
# This ensures the primary won't delete WAL that we haven't consumed yet.
primary_slot_name = 'replica_slot_1'

# Allow read-only queries on this replica
hot_standby = on

# hot_standby_feedback: tells the primary which rows the replica is currently
# reading. This prevents the primary from cleaning up (VACUUM) rows that the
# replica still needs, which would cause "snapshot too old" errors on the replica.
hot_standby_feedback = on
```

Save and exit.

> **Security note on the password in plain text:** For better security, you can store the password in a `.pgpass` file instead:
> ```bash
> echo "192.168.1.10:5432:replication:replicator:StrongRepl1cationPass!" \
>   >> /var/lib/postgresql/.pgpass
> chmod 600 /var/lib/postgresql/.pgpass
> chown postgres:postgres /var/lib/postgresql/.pgpass
> ```
> Then in `postgresql.conf`, remove the `password=...` part from `primary_conninfo`. PostgreSQL will look it up from `.pgpass` automatically.

### 4.5 — Fix file permissions

The data directory must be owned exclusively by the `postgres` OS user. If it's readable by other users, PostgreSQL will refuse to start with a permissions error.

```bash
chown -R postgres:postgres /var/lib/postgresql/16/main
chmod 700 /var/lib/postgresql/16/main
```

`pg_basebackup` should have set these correctly, but it's always good to verify.

### 4.6 — Start the replica and check the logs

```bash
systemctl start postgresql

# Watch the log in real time — this is the most important step
tail -f /var/log/postgresql/postgresql-16-main.log
```

**What you should see (success):**
```
LOG:  entering standby mode
LOG:  redo starts at 0/X000028
LOG:  consistent recovery state reached at 0/X000100
LOG:  database system is ready to accept read only connections
LOG:  started streaming WAL from primary at 0/X000000 on timeline 1
```

The last line is the most important: **"started streaming WAL"** means replication is working.

Press `Ctrl+C` to stop watching the log.

**If you see error messages instead:**

| Error message | What it means | How to fix |
|---|---|---|
| `could not connect to the primary server` | Network or authentication problem | Check `primary_conninfo`, `pg_hba.conf` on primary, and network connectivity |
| `requested WAL segment has already been removed` | Primary deleted WAL the replica needs | Re-run pg_basebackup to get a fresh copy |
| `data directory has wrong ownership` | File ownership is wrong | Run `chown -R postgres:postgres /var/lib/postgresql/16/main` |
| `data directory has wrong permissions` | Directory permissions too open | Run `chmod 700 /var/lib/postgresql/16/main` |

---

## Phase 5 — Verify That Replication Is Working

### 5.1 — Check replication status on the PRIMARY

SSH into the primary (192.168.1.10):

```bash
sudo -u postgres psql
```

```sql
-- This view shows all currently connected replicas.
-- If the replica is working, you will see one row here.
SELECT
  application_name,
  client_addr,
  state,
  pg_size_pretty(sent_lsn - replay_lsn) AS lag,
  sync_state
FROM pg_stat_replication;
```

**Good output:**
```
 application_name | client_addr   | state     | lag   | sync_state
------------------+---------------+-----------+-------+-----------
 pg-replica       | 192.168.1.20  | streaming | 0 bytes | async
```

- `state = streaming` — the replica is live and streaming
- `lag = 0 bytes` — the replica is fully caught up
- `sync_state = async` — changes go to the replica asynchronously (default; the primary doesn't wait for the replica to confirm before acknowledging writes to the app)

```sql
-- Also confirm this server is the primary (not in recovery)
SELECT pg_is_in_recovery();
-- Must return: f

\q
```

### 5.2 — Check replication status on the REPLICA

SSH into the replica (192.168.1.20):

```bash
sudo -u postgres psql
```

```sql
-- This view shows the WAL receiver — the process that pulls data from the primary
SELECT
  status,
  sender_host,
  sender_port,
  received_lsn,
  last_msg_receipt_time
FROM pg_stat_wal_receiver;
```

**Good output:**
```
  status   | sender_host   | sender_port
-----------+---------------+------------
 streaming | 192.168.1.10  | 5432
```

```sql
-- Confirm this server is a replica (in recovery mode)
SELECT pg_is_in_recovery();
-- Must return: t

-- Check how far behind the replica is (should be near 0 seconds)
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;

\q
```

### 5.3 — End-to-end data test

This is the final proof that replication is actually working. We will write data on the primary and read it on the replica.

**On the PRIMARY (192.168.1.10):**
```sql
sudo -u postgres psql

CREATE DATABASE replication_test;
\c replication_test
CREATE TABLE test_table (id SERIAL PRIMARY KEY, message TEXT, inserted_at TIMESTAMPTZ DEFAULT now());
INSERT INTO test_table (message) VALUES ('hello from primary'), ('replication works');
SELECT * FROM test_table;
\q
```

**On the REPLICA (192.168.1.20) — within a second or two:**
```sql
sudo -u postgres psql

\c replication_test
SELECT * FROM test_table;
-- You should see the same 2 rows that you inserted on the primary

-- Confirm the replica refuses writes (it's read-only)
INSERT INTO test_table (message) VALUES ('this should fail');
-- Expected: ERROR: cannot execute INSERT in a read-only transaction

\q
```

**Clean up on the PRIMARY:**
```sql
sudo -u postgres psql
\c postgres
DROP DATABASE replication_test;
\q
```

---

## Phase 6 — Manual Failover (What to Do If the Primary Crashes)

This section covers how to promote the replica to become the new primary when the original primary has failed and cannot recover quickly.

### 6.1 — Confirm the primary is actually down

**This is the most important step.** Never promote the replica while the primary is still running, even if it seems unresponsive. If both servers are writable at the same time (called "split-brain"), your data will diverge and reconciling it is extremely painful.

Run from the replica server:
```bash
# Test if the primary is reachable
pg_isready -h 192.168.1.10 -p 5432
# If the primary is down: "192.168.1.10:5432 - no response"
# If the primary is UP:   "192.168.1.10:5432 - accepting connections"
#                         DO NOT PROMOTE in this case

# Another test
nc -zv 192.168.1.10 5432
# Connection refused or timeout = primary is down (safe to proceed)
# Connected = primary is still running (do NOT promote)
```

Also physically verify or check with your team that the primary server is offline.

### 6.2 — Promote the replica

Once you are certain the primary is down:

```bash
# SSH into the REPLICA (192.168.1.20)

# Method 1: SQL command (preferred — works if PostgreSQL is running)
sudo -u postgres psql -c "SELECT pg_promote();"
# Returns: t (true) = promotion started
```

If psql is not available:
```bash
# Method 2: pg_ctl command (works even without psql)
sudo -u postgres /usr/lib/postgresql/16/bin/pg_ctl promote \
  -D /var/lib/postgresql/16/main
```

### 6.3 — Verify promotion

```bash
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"
# Must return: f  (false = no longer in recovery = now a primary)

# Check the log for confirmation
tail -n 30 /var/log/postgresql/postgresql-16-main.log
```

Look for these lines in the log:
```
LOG:  received promote request
LOG:  redo done at X/XXXXXXXX
LOG:  selected new timeline ID: 2
LOG:  database system is ready to accept connections
```

"selected new timeline ID: 2" means the replica successfully promoted to a new primary on timeline 2 (the original primary was on timeline 1).

### 6.4 — Update your application

Change your application's database connection string to point to the new primary: `192.168.1.20:5432`.

### 6.5 — Rebuild the old primary as a new replica (when it's repaired)

Once the old primary (192.168.1.10) is repaired and booted:

```bash
# SSH into the OLD primary (192.168.1.10)
systemctl stop postgresql
rm -rf /var/lib/postgresql/16/main/*

# Take a fresh base backup FROM the new primary (192.168.1.20)
sudo -u postgres pg_basebackup \
  --host=192.168.1.20 \
  --port=5432 \
  --username=replicator \
  --pgdata=/var/lib/postgresql/16/main \
  --wal-method=stream \
  --checkpoint=fast \
  --progress \
  --verbose

# Create standby signal
touch /var/lib/postgresql/16/main/standby.signal
chown postgres:postgres /var/lib/postgresql/16/main/standby.signal

# Update postgresql.conf to point to the new primary
nano /etc/postgresql/16/main/postgresql.conf
# Change: primary_conninfo = 'host=192.168.1.20 ...'

systemctl start postgresql
```

---

## Phase 7 — Monitoring and Health Checks

You should run health checks regularly to catch problems before they cause an outage.

### 7.1 — Create the health check script

Save this file on **both** servers at `/usr/local/bin/pg-replication-check.sh`:

```bash
nano /usr/local/bin/pg-replication-check.sh
```

Paste the following content:

```bash
#!/usr/bin/env bash
# PostgreSQL Replication Health Check Script
# Checks whether this server is healthy and replication is working.

set -euo pipefail

echo "======================================"
echo "PG Replication Health: $(hostname)"
echo "Time: $(date)"
echo "======================================"

# Step 1: Is PostgreSQL running?
if ! systemctl is-active --quiet postgresql; then
  echo "CRITICAL: PostgreSQL is NOT running! Start it with: systemctl start postgresql"
  exit 1
fi
echo "OK: PostgreSQL service is running"

# Step 2: Is this the primary or replica?
IS_REPLICA=$(sudo -u postgres psql -Atc "SELECT pg_is_in_recovery();" 2>/dev/null)

if [[ "$IS_REPLICA" == "t" ]]; then
  # ---- REPLICA CHECKS ----
  echo "ROLE: REPLICA (read-only standby)"

  # Is the WAL receiver connected?
  WR_STATUS=$(sudo -u postgres psql -Atc \
    "SELECT status FROM pg_stat_wal_receiver;" 2>/dev/null || echo "ERROR")
  echo "WAL Receiver Status: ${WR_STATUS:-NOT CONNECTED}"

  if [[ "$WR_STATUS" != "streaming" ]]; then
    echo "WARNING: Replica is NOT actively streaming from the primary!"
    echo "  -> Check primary is running and pg_hba.conf allows connection"
  fi

  # How far behind is the replica?
  DELAY=$(sudo -u postgres psql -Atc \
    "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::int::text, 'no data yet');" \
    2>/dev/null)
  echo "Replication delay: ${DELAY} seconds"

  if [[ "$DELAY" != "no data yet" && "$DELAY" -gt 300 ]]; then
    echo "WARNING: Replica is more than 5 minutes behind the primary!"
  fi

else
  # ---- PRIMARY CHECKS ----
  echo "ROLE: PRIMARY (read-write)"

  # How many replicas are connected?
  REPLICA_COUNT=$(sudo -u postgres psql -Atc \
    "SELECT COUNT(*) FROM pg_stat_replication WHERE state='streaming';" 2>/dev/null)
  echo "Connected streaming replicas: ${REPLICA_COUNT:-0}"

  if [[ "${REPLICA_COUNT:-0}" -eq 0 ]]; then
    echo "WARNING: No replica is currently connected!"
  fi

  # Show per-replica details
  echo ""
  echo "--- Replica Details ---"
  sudo -u postgres psql -c "
    SELECT
      application_name AS replica,
      client_addr AS ip,
      state,
      pg_size_pretty(sent_lsn - replay_lsn) AS lag,
      sync_state
    FROM pg_stat_replication;" 2>/dev/null

  # Check replication slot health
  echo ""
  echo "--- Replication Slot Status ---"
  sudo -u postgres psql -c "
    SELECT
      slot_name,
      active,
      pg_size_pretty(
        pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)
      ) AS wal_size_retained
    FROM pg_replication_slots;" 2>/dev/null
  echo "  (If wal_size_retained is growing very large, the replica may be down)"
fi

# Disk usage check
echo ""
echo "--- Disk Usage ---"
df -h /var/lib/postgresql
echo ""
echo "======================================"
```

Make it executable:
```bash
chmod +x /usr/local/bin/pg-replication-check.sh
```

### 7.2 — Run it manually

```bash
/usr/local/bin/pg-replication-check.sh
```

### 7.3 — Schedule it to run automatically every 5 minutes

```bash
# Open the system cron (runs as root)
crontab -e
```

Add this line at the bottom:
```
*/5 * * * * /usr/local/bin/pg-replication-check.sh >> /var/log/pg-replication-check.log 2>&1
```

Save and exit. The check will now run every 5 minutes and log output to `/var/log/pg-replication-check.log`.

### 7.4 — What to watch for (alert thresholds)

| What to check | Where to look | Alert if... |
|---|---|---|
| Replica connected | `pg_stat_replication` on primary | 0 replicas streaming |
| Replication lag | `sent_lsn - replay_lsn` on primary | > 100 MB |
| Replication time delay | `pg_last_xact_replay_timestamp()` on replica | > 5 minutes |
| Slot WAL retained | `pg_replication_slots.wal_size_retained` | > 5 GB |
| Disk space | `df -h /var/lib/postgresql` | > 80% full |

---

## Quick Reference: Important File Locations

| What | Where |
|---|---|
| Main configuration | `/etc/postgresql/16/main/postgresql.conf` |
| Access control rules | `/etc/postgresql/16/main/pg_hba.conf` |
| Data directory | `/var/lib/postgresql/16/main/` |
| Standby signal file (replica only) | `/var/lib/postgresql/16/main/standby.signal` |
| PostgreSQL log | `/var/log/postgresql/postgresql-16-main.log` |
| Password file (optional) | `/var/lib/postgresql/.pgpass` |
| Health check script | `/usr/local/bin/pg-replication-check.sh` |
| Health check log | `/var/log/pg-replication-check.log` |

## Quick Reference: Useful Commands

```bash
# Start / stop / restart / check status
systemctl start postgresql
systemctl stop postgresql
systemctl restart postgresql
systemctl status postgresql

# Connect to the database as the postgres admin user
sudo -u postgres psql

# Watch the PostgreSQL log in real time
tail -f /var/log/postgresql/postgresql-16-main.log

# Check if replication is working (run on primary)
sudo -u postgres psql -c "SELECT application_name, state, pg_size_pretty(sent_lsn - replay_lsn) AS lag FROM pg_stat_replication;"

# Check if this server is primary (f) or replica (t)
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"
```

---

## Common Pitfalls Summary

| Mistake | Consequence | Prevention |
|---|---|---|
| `wal_level` left as default (`minimal`) | Replication silently never works | Always set `wal_level = replica` |
| Forgot to create `standby.signal` | Replica starts as a second primary (split-brain) | Always verify the file exists before `systemctl start` |
| Promoting while primary is alive | Two writable primaries, data diverges | Always run `pg_isready` check first |
| Stale replication slot | Primary disk fills up, PostgreSQL stops | Monitor `pg_replication_slots` regularly |
| Using `wal_keep_segments` (old name) | PostgreSQL 16 ignores it silently | Use `wal_keep_size` (in MB) |
| Wrong IP in pg_hba.conf | Replica cannot authenticate | Double-check the `/32` CIDR notation |
| Data directory not empty before pg_basebackup | Tool refuses to run | `rm -rf /var/lib/postgresql/16/main/*` first |
| Wrong file ownership on data dir | PostgreSQL refuses to start | `chown -R postgres:postgres /var/lib/postgresql/16/main` |
