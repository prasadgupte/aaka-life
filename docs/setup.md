# Aaka Setup Guide

> **Installing with Claude Code?** See [`INSTALL.md`](../INSTALL.md) at the repo root —
> it's the canonical AI-native install guide with phases, credential collection, and
> machine-runnable verify blocks for each step.
>
> Paste the contents of [`CLAUDE_SETUP_PROMPT.md`](../CLAUDE_SETUP_PROMPT.md) into
> Claude Code to begin a guided installation.

---

## Manual / interactive setup (human-driven)

For a fully interactive install run:
```bash
bash admin/aaka.sh deploy
```
This branches on VPS vs. Mac and walks through each step with prompts.

---

## Prerequisites

- **Mac (&Home)**: macOS, Python 3.11+, Docker Desktop (optional for local dev)
- **VPS (&Away)**: Ubuntu 22.04+, Docker CE with Compose plugin

---

## Quick reference

```bash
# Non-interactive AI setup (reads JSON config, no prompts):
bash admin/aaka.sh setup --config /tmp/aaka-setup.json

# Pre-flight check only (no writes):
bash admin/aaka.sh setup --check

# Full health check after setup:
bash admin/aaka.sh diagnose

# Run smoke tests:
bash admin/aaka.sh test
```

---

## VPS Docker CE install

Ubuntu's `docker.io` package doesn't include the Compose plugin. Install Docker CE from Docker's official repo:

```bash
apt-get update -qq
apt-get install -y ca-certificates curl gnupg lsb-release
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

---

## Mac executor reliability (sleep prevention)

The queue worker runs as a launchd agent. For unattended overnight operation:

```bash
bash admin/aaka.sh overnight
```

This checks AC power + Power Nap status and enables them if needed. Power Nap wakes the Mac every 15–30 min to run background agents when the lid is closed on AC power.

---

## Prod vs. dev containers

Always use `docker-compose.prod.yml` for production operations on VPS:
```bash
docker compose -f docker-compose.prod.yml up -d --build sensor
docker compose -f docker-compose.prod.yml exec sensor bash
```

Using bare `docker compose` starts `aaka-sensor-dev` (dev container) which will crash on VPS.
