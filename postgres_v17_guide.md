# PostgreSQL 17 — Installation & Migration Guide

Covers two scenarios:
- **Scenario A** — Fresh install of PostgreSQL 17 on a machine with no existing PostgreSQL (e.g., a new PRD server)
- **Scenario B** — Migrate from PostgreSQL 12 → 17 on a local Ubuntu 20.04 machine where v12 is already running

Ubuntu versions referenced:
- Local machine: Ubuntu 20.04 (Focal Fossa)
- PRD machine: Ubuntu 22.04 (Jammy Jellyfish)

The commands are identical for both OS versions. The `$(lsb_release -cs)` substitution
automatically resolves to the correct codename (`focal` or `jammy`) at runtime.

---

## Part 0 — Concepts You Need to Know

### What is a GPG Key?
When you run `apt install`, your machine downloads packages from the internet.
A GPG (GNU Privacy Guard) key is a cryptographic signing mechanism that proves a package
actually came from the declared source (postgresql.org) and was not tampered with in transit.

- postgresql.org holds a **private key** and uses it to sign every package they publish.
- You download their **public key** (the `.asc` file) and register it with apt.
- apt uses that public key to verify the signature on every package before installing.
- If the signature check fails, apt refuses to install the package.

### What are `pg_*cluster` commands?
These are Debian/Ubuntu-specific wrapper scripts that do not exist on RHEL, CentOS, or macOS.
They are installed automatically as part of the `postgresql-common` package.

| Command                            | What it does                                              |
|------------------------------------|-----------------------------------------------------------|
| `pg_lsclusters`                    | List all PostgreSQL clusters (version, port, status, dir) |
| `pg_createcluster <ver> <name>`    | Create and initialize a new cluster                       |
| `pg_dropcluster <ver> <name>`      | Stop and delete a cluster                                 |
| `pg_ctlcluster <ver> <name> start` | Start a specific cluster                                  |
| `pg_ctlcluster <ver> <name> stop`  | Stop a specific cluster                                   |
| `pg_upgradecluster`                | Upgrade a cluster to a newer PostgreSQL version           |

They allow multiple PostgreSQL versions and multiple clusters to coexist on the same machine,
each on a different port. The underlying PostgreSQL binaries (initdb, pg_ctl, postgres) are
the same — these scripts are just convenient wrappers integrated with systemd.

---

## Part 1 — Add the Official PostgreSQL Apt Repository
(Required for BOTH scenarios — run this first on any machine)

Ubuntu's default apt repository ships an outdated PostgreSQL version (v12 on 20.04, v14 on 22.04).
These steps point apt to postgresql.org directly, where v17 is available.

---

### Step 1 — Install curl and CA certificates

```bash
sudo apt install -y curl ca-certificates
```

**What it does:**
- `curl` — command-line tool used to download files from URLs (needed in the next step)
- `ca-certificates` — a bundle of trusted root certificates that allows your machine to verify
  HTTPS connections. Without this, the curl download in Step 2 would fail SSL verification.
- On most Ubuntu machines these are already installed; the command will just print
  "already installed" and exit cleanly.

---

### Step 2 — Create the directory for the GPG key

```bash
sudo install -d /usr/share/postgresql-common/pgdg
```

**What it does:**
- Creates the directory `/usr/share/postgresql-common/pgdg` where the GPG key will be stored.
- `install -d` is functionally identical to `mkdir -p` but is used here because it sets
  ownership and permissions atomically in one command.
- This is not installing any software — the word "install" here refers to the Unix `install`
  utility, not a package manager.

---

### Step 3 — Download the PostgreSQL GPG public key

```bash
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
```

**What it does:**
- `curl` downloads postgresql.org's public GPG key and saves it to the directory created above.
- `-o <path>` — output file flag. Saves the downloaded content to the specified path instead
  of printing it to the terminal.
- `--fail` — if the server returns an HTTP error (404, 500, etc.), exit with a non-zero error
  code instead of silently saving the error response as the key file. Without this flag, a
  "404 Not Found" HTML page could get saved as your key file, breaking everything downstream.
- The URL `https://www.postgresql.org/media/keys/ACCC4CF8.asc` — `ACCC4CF8` is the short
  fingerprint ID of postgresql.org's GPG key. The `.asc` extension means the key is
  ASCII-armored (text-encoded binary).

---

### Step 4 — Register the postgresql.org apt repository

```bash
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'
```

**Why `sudo sh -c '...'` instead of `sudo echo ... > file`:**
The `>` redirection is handled by your current shell, not by sudo. So `sudo echo ... > file`
runs echo as root but the redirection still happens as your normal user — which cannot write
to `/etc/`. Wrapping everything in `sudo sh -c '...'` launches a new shell as root, so the
redirection also runs as root.

**Breaking down the string written to the file:**

| Section                                          | Meaning                                                                                 |
|--------------------------------------------------|-----------------------------------------------------------------------------------------|
| `deb`                                            | This is a binary package repository (vs `deb-src` for source code)                     |
| `[signed-by=.../apt.postgresql.org.asc]`         | The GPG key apt must use to verify packages from this repository                        |
| `https://apt.postgresql.org/pub/repos/apt`       | Base URL of the repository — where apt fetches the package index and packages           |
| `$(lsb_release -cs)`                             | Resolves at runtime to your Ubuntu codename: `focal` (20.04) or `jammy` (22.04)        |
| `-pgdg`                                          | Appended to the codename — e.g., `jammy-pgdg` is the suite name on postgresql.org      |
| `main`                                           | The component within that repository suite to enable                                    |
| `> /etc/apt/sources.list.d/pgdg.list`            | Writes the line to a new file; apt reads every `.list` file in this directory           |

**Result on Ubuntu 20.04:** `focal-pgdg`
**Result on Ubuntu 22.04:** `jammy-pgdg`
Both suites exist and are actively maintained on postgresql.org.

---

### Step 5 — Refresh package lists

```bash
sudo apt update
```

**What it does:**
Tells apt to re-read all configured repositories including the new postgresql.org one you just
added. After this command, apt knows about postgresql-17 and all other packages in that repo.

---

## Part 2, Scenario A — Fresh Install of PostgreSQL 17 (No Existing PostgreSQL)

Use this on a PRD machine or any machine that does not have PostgreSQL installed.

### Step 6A — Install PostgreSQL 17

```bash
sudo apt install -y postgresql-17
```

**What it does:**
- Installs PostgreSQL 17 from the postgresql.org repository added in Part 1.
- Automatically installs `postgresql-common` (which provides the `pg_*cluster` commands).
- Internally runs `pg_createcluster 17 main` which:
  - Initializes the data directory at `/var/lib/postgresql/17/main`
  - Creates config files at `/etc/postgresql/17/main/`
  - Registers and starts the systemd service on port **5432**

### Step 7A — Verify the installation

```bash
pg_lsclusters
```

Expected output:
```
Ver  Cluster  Port  Status  Owner     Data directory
17   main     5432  online  postgres  /var/lib/postgresql/17/main
```

```bash
sudo -u postgres psql -c "SELECT version();"
```

This connects to PostgreSQL and prints the version string to confirm it is running correctly.

---

## Part 2, Scenario B — Migrate from PostgreSQL 12 to 17 (Local Machine)

Use this when PostgreSQL 12 is already installed and running with data you need to preserve.
The strategy is: dump all data from v12, install v17 alongside it (on a different port),
restore into v17, then switch v17 to port 5432.

### Step 6B — Dump your v12 data

```bash
# Dump the database (custom format — compressed, parallel-restore capable)
pg_dump -U postgres -Fc your_database_name > ~/pg12_backup.dump

# Also dump global objects: roles, users, tablespaces
pg_dumpall -U postgres --globals-only > ~/pg12_globals.sql
```

**What it does:**
- `pg_dump` exports a single database. `-Fc` is "custom format" — compressed and more flexible
  than plain SQL. The dump file can be restored in parallel and you can select specific tables.
- `pg_dumpall --globals-only` exports roles and users, which `pg_dump` does not include.
- Run this while v12 is still running. You are not touching v12 at all yet.

---

### Step 7B — Install PostgreSQL 17 (alongside v12)

```bash
sudo apt install -y postgresql-17
```

Since v12 is already using port 5432, the Debian packaging automatically assigns v17
to port **5433**. Both clusters run simultaneously.

Verify both are running:
```bash
pg_lsclusters
```

Expected output:
```
Ver  Cluster  Port  Status  Owner     Data directory
12   main     5432  online  postgres  /var/lib/postgresql/12/main
17   main     5433  online  postgres  /var/lib/postgresql/17/main
```

---

### Step 8B — Restore into v17

```bash
# Restore global objects (roles/users) first
sudo -u postgres psql -p 5433 -f ~/pg12_globals.sql

# Create the target database in v17
sudo -u postgres psql -p 5433 -c "CREATE DATABASE your_database_name;"

# Restore the data
sudo -u postgres pg_restore -p 5433 -d your_database_name ~/pg12_backup.dump
```

**Note on `-p 5433`:** All commands targeting v17 must include `-p 5433` to reach it instead
of v12. Without it, the default port 5432 (v12) is used.

---

### Step 9B — Fix public schema permissions (PostgreSQL 15+ change)

In PostgreSQL 15 and above, the `CREATE` privilege on the `public` schema is no longer
granted to all users by default. If your application user is not a superuser, Alembic
migrations that create tables will fail with a permission error.

```bash
sudo -u postgres psql -p 5433 -d your_database_name \
  -c "GRANT CREATE ON SCHEMA public TO your_db_user;"
```

---

### Step 10B — Test your application against v17

Temporarily update your `.env` to point to port 5433 and run Alembic migrations:

```bash
# In your api/ directory
DATABASE_URL=postgresql://user:pass@localhost:5433/your_database_name alembic upgrade head
```

Run your application and verify everything works before proceeding.

---

### Step 11B — Switch v17 to port 5432

Once satisfied that everything works on v17:

```bash
# Stop v12
sudo systemctl stop postgresql@12-main

# Edit v17 config to use port 5432
sudo nano /etc/postgresql/17/main/postgresql.conf
# Find the line:  port = 5433
# Change it to:   port = 5432

# Restart v17
sudo systemctl restart postgresql@17-main

# Revert your .env back to port 5432
```

Verify:
```bash
pg_lsclusters
```

Expected output:
```
Ver  Cluster  Port  Status   Owner     Data directory
12   main     5432  down     postgres  /var/lib/postgresql/12/main
17   main     5432  online   postgres  /var/lib/postgresql/17/main
```

---

### Step 12B — (Optional) Remove PostgreSQL 12

Once you have confirmed v17 is stable:

```bash
sudo pg_dropcluster 12 main
sudo apt remove --purge postgresql-12
sudo apt autoremove
```

---

## Part 3 — Custom PGDATA and Logs Directory

By default PostgreSQL stores data in `/var/lib/postgresql/<ver>/main/`.
If you want a different location (different disk, dedicated partition, etc.), follow these steps.

Do this **before restoring any data** — it is much harder to move the data directory after the
fact. These steps replace Step 7A or Step 7B (before restoring data into the new cluster).

---

### Step 1 — Drop the auto-created cluster

When `apt install postgresql-17` runs, it automatically creates a cluster. Drop it first:

```bash
sudo systemctl stop postgresql@17-main
sudo pg_dropcluster 17 main
```

---

### Step 2 — Create your custom directories

```bash
sudo mkdir -p /your/custom/pgdata
sudo mkdir -p /your/custom/pglogs

# PostgreSQL runs as the 'postgres' OS user — it must own these directories
sudo chown postgres:postgres /your/custom/pgdata /your/custom/pglogs

# PGDATA must be mode 700 — PostgreSQL refuses to start if it is world-readable
sudo chmod 700 /your/custom/pgdata
```

---

### Step 3 — Recreate the cluster with custom paths

```bash
sudo pg_createcluster \
  --datadir /your/custom/pgdata \
  --logfile /your/custom/pglogs/postgresql.log \
  17 main
```

This initializes a fresh v17 cluster using your directories.
Port assignment works the same way: 5432 if no other cluster is running, 5433 if v12 is on 5432.

---

### Step 4 — Configure log rotation (optional)

For a logs directory with daily rotation instead of a single log file, edit `postgresql.conf`:

```bash
sudo nano /etc/postgresql/17/main/postgresql.conf
```

Set these values:

```
logging_collector = on
log_directory = '/your/custom/pglogs'
log_filename = 'postgresql-%Y-%m-%d.log'
log_rotation_age = 1d
log_rotation_size = 0
```

Then restart:

```bash
sudo systemctl restart postgresql@17-main
```

---

### Step 5 — Verify the cluster is using your paths

```bash
pg_lsclusters
```

The **Data directory** column should show your custom path.

```bash
sudo -u postgres psql -p 5433 -c "SHOW data_directory;"
sudo -u postgres psql -p 5433 -c "SHOW log_directory;"
```

---

## Part 4 — Post-Install Verification Commands

```bash
# List all clusters and their status
pg_lsclusters

# Connect to a specific cluster and check version
sudo -u postgres psql -p 5432 -c "SELECT version();"

# Check which port PostgreSQL is listening on
sudo ss -tlnp | grep postgres

# Check service status
sudo systemctl status postgresql@17-main

# Tail the PostgreSQL log
sudo tail -f /var/log/postgresql/postgresql-17-main.log
# or if using custom log path:
sudo tail -f /your/custom/pglogs/postgresql.log
```

---

## Quick Reference — Key File Locations (Default Paths)

| Item              | Path                                          |
|-------------------|-----------------------------------------------|
| Config files      | `/etc/postgresql/17/main/postgresql.conf`     |
| Client auth       | `/etc/postgresql/17/main/pg_hba.conf`         |
| Data directory    | `/var/lib/postgresql/17/main/`                |
| Log file          | `/var/log/postgresql/postgresql-17-main.log`  |
| Systemd service   | `postgresql@17-main`                          |
| Socket directory  | `/var/run/postgresql/`                        |
