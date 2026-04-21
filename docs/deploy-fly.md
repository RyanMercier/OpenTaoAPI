# Deploying OpenTaoAPI to Fly.io

Fly's free tier is well suited to this project: always-on VMs, persistent volumes for the SQLite database, and no surprise egress charges at small scale. Railway is fine too, but its free tier sleeps and you lose the DB every cold start.

## One-time setup

```bash
# 1. Install flyctl if you don't have it.
curl -L https://fly.io/install.sh | sh

# 2. Log in.
fly auth login

# 3. From the project root:
fly launch --no-deploy --copy-config=false --name opentao-<your-suffix>
```

`fly launch` will generate a `fly.toml`. Before deploying:

- Set `internal_port = 8000` (matches the `Dockerfile`'s `API_PORT`).
- Add a mount for the SQLite volume:

```toml
[[mounts]]
  source = "opentao_data"
  destination = "/app/data"
```

Create the volume:

```bash
fly volumes create opentao_data --size 3   # 3 GB; ~18 months of full-subnet snapshots
```

## Environment

```bash
fly secrets set HISTORY_POLL_INTERVAL=1800
# Optional: point at your own subtensor node instead of public Finney
# fly secrets set SUBTENSOR_ENDPOINT=wss://your-validator:9944
```

`ARCHIVE_ENDPOINT` defaults to the public `wss://archive.chain.opentensor.ai:443/` — only override if you're running an archive node of your own.

## Deploy

```bash
fly deploy
fly status
fly logs
```

Hit `https://opentao-<your-suffix>.fly.dev/health` and confirm:

```json
{
  "status": "ok",
  "network": "finney",
  "poller": {
    "stale": false,
    "consecutive_failures": 0,
    "poller_restarts": 0
  }
}
```

## Populate history

SSH into the running machine and run the backfill scripts once. Both are idempotent and resumable.

```bash
fly ssh console -C "python -m scripts.backfill --all-subnets --days 7 --concurrency 8"
fly ssh console -C "python -m scripts.backfill_prices"
```

The live poller keeps everything fresh from there.

## Updates

```bash
fly deploy
```

That's it. The volume persists across deploys, so the historical database survives.

## Troubleshooting

- **`stale=true` on /health**: the poller is hung or crash-looping. Check `fly logs` — the supervisor prints `Poller crashed (restart #N)` with a full traceback.
- **Cold start is slow**: first metagraph fetch takes ~15-20s; Fly's health checks should allow at least 60s.
- **DB got corrupted**: stop the app, `fly ssh console -C "rm /app/data/opentao.db"`, restart, and re-run backfill.
