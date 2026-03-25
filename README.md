# OpenTaoAPI

Open-source, self-hostable Bittensor network explorer and API. A drop-in alternative to TaoStats and TaoMarketCap with no rate limits.

**Web UI** at `http://localhost:8000` | **Swagger docs** at `http://localhost:8000/docs`

## Features

- REST API with full subnet, neuron, emission, and portfolio data
- TaoStats-compatible `/miner/` endpoint for drop-in replacement
- Web dashboard: portfolio viewer, subnets overview, miners/validators tables
- Direct chain queries via Bittensor SDK (no third-party APIs except MEXC for price)
- In-memory caching with configurable TTLs
- Self-hostable with Docker or conda

## Quick Start

### With conda

```bash
conda create -n tao python=3.11 -y
conda activate tao
pip install -r requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

First startup takes ~15-20s for the initial metagraph sync. Subsequent requests are instant from cache.

### With Docker

```bash
docker-compose up -d
```

## Web UI

| Page | URL | Description |
|------|-----|-------------|
| Subnets | `/subnets` | All subnets ranked by market cap with emission %, price, volume |
| Miners | `/subnet/{netuid}/miners` | Miner table with incentive, stake, daily emission (alpha + USD) |
| Validators | `/subnet/{netuid}/miners` then Validators tab | Validator table with stake, VTrust, dividends, dominance, daily emission |
| Portfolio | `/` | Coldkey lookup showing total balance, per-subnet breakdown, daily yield |

## API Endpoints

### Price

```
GET /api/v1/price/tao
```

Current TAO/USDT from MEXC. Cached 30s.

### Portfolio

```
GET /api/v1/portfolio/{coldkey}
```

Cross-subnet portfolio for a coldkey. Returns total balance (TAO + USD), free balance, staked balance, and per-subnet breakdown with alpha balance, TAO equivalent, price, daily yield.

### Miner (TaoStats-compatible)

```
GET /api/v1/miner/{coldkey}/{netuid}
```

Response format matches the TaoStats `/api/miner/` endpoint. Includes coldkey balance, alpha balances across all subnets, hotkey details with emission data, registration status, and mining rank.

### Subnets

```
GET /api/v1/subnets                         # All subnets with market cap, emission %, price, volume
GET /api/v1/subnet/{netuid}/info             # Subnet hyperparams and pool data
GET /api/v1/subnet/{netuid}/neurons          # Paginated neuron list (?page=1&per_page=50)
GET /api/v1/subnet/{netuid}/metagraph        # Full metagraph (?refresh=true to bypass cache)
GET /api/v1/subnet/{netuid}/miners           # Miners with daily emission (?sort=incentive&order=desc)
GET /api/v1/subnet/{netuid}/validators       # Validators with stake, dividends, daily emission
```

### Neurons

```
GET /api/v1/neuron/{netuid}/{uid}            # Single neuron by UID
GET /api/v1/neuron/coldkey/{coldkey}          # All neurons for a coldkey
GET /api/v1/neuron/hotkey/{hotkey}            # Neuron by hotkey
```

### Emissions

```
GET /api/v1/emissions/{netuid}/{uid}
```

Emission breakdown: alpha per epoch, alpha per block, TAO per block, daily/monthly estimates in alpha, TAO, and USD.

## Usage Examples

### curl

```bash
# TAO price
curl http://localhost:8000/api/v1/price/tao

# Portfolio for a coldkey
curl http://localhost:8000/api/v1/portfolio/5EhrSbeGeiLgsXcJTXXaBCcqrrMubvWcykSwk4Ho6KUd5sQG

# All subnets ranked by market cap
curl http://localhost:8000/api/v1/subnets

# Subnet 51 miners sorted by incentive
curl "http://localhost:8000/api/v1/subnet/51/miners?sort=incentive&order=desc"

# Subnet 51 validators sorted by stake
curl "http://localhost:8000/api/v1/subnet/51/validators?sort=stake&order=desc"

# Miner info (TaoStats-compatible format)
curl http://localhost:8000/api/v1/miner/5GEP69yPWi3qB2tLQdsbv3Fa2JA6wH6szFNP77EqXizEufvM/51

# Emission breakdown for subnet 51, UID 40
curl http://localhost:8000/api/v1/emissions/51/40
```

### Python

```python
import httpx

BASE = "http://localhost:8000/api/v1"

# Portfolio
r = httpx.get(f"{BASE}/portfolio/5EhrSbeGeiLgsXcJTXXaBCcqrrMubvWcykSwk4Ho6KUd5sQG")
p = r.json()
print(f"Balance: {p['total_balance_tao']:.4f} TAO (${p['total_balance_usd']:.2f})")
for sn in p["subnets"]:
    print(f"  SN{sn['netuid']} {sn['name']}: {sn['balance_tao']:.4f} TAO  yield {sn['daily_yield_tao']:.4f}/day")

# Miner data (TaoStats-compatible)
r = httpx.get(f"{BASE}/miner/5GEP69yPWi3qB2tLQdsbv3Fa2JA6wH6szFNP77EqXizEufvM/51")
data = r.json()["data"][0]
print(f"Total balance: {int(data['total_balance']) / 1e9:.4f} TAO")
for hk in data["hotkeys"]:
    print(f"  UID {hk['uid']} rank #{hk['miner_rank']} emission {int(hk['emission']) / 1e9:.4f} alpha/epoch")

# Emissions
r = httpx.get(f"{BASE}/emissions/51/40")
em = r.json()
print(f"Daily: {em['daily_tao']:.4f} TAO (${em['daily_usd']:.2f})")
```

## Configuration

All settings via environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `BITTENSOR_NETWORK` | `finney` | Network: finney, testnet, local |
| `CACHE_TTL_METAGRAPH` | `300` | Metagraph cache seconds |
| `CACHE_TTL_PRICE` | `30` | Price cache seconds |
| `CACHE_TTL_DYNAMIC_INFO` | `120` | Subnet pool data cache seconds |
| `CACHE_TTL_BALANCE` | `60` | Balance/stake cache seconds |
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Port |

## Project Structure

```
OpenTaoAPI/
├── api/
│   ├── main.py                 # FastAPI app, routes, static file serving
│   ├── config.py               # Settings from environment
│   ├── routes/
│   │   ├── price.py            # TAO price from MEXC
│   │   ├── miner.py            # TaoStats-compatible miner endpoint
│   │   ├── neuron.py           # Neuron lookup by UID/hotkey/coldkey
│   │   ├── subnet.py           # Subnet info, metagraph, miners, validators
│   │   ├── emissions.py        # Emission breakdown
│   │   └── portfolio.py        # Cross-subnet portfolio
│   ├── services/
│   │   ├── chain_client.py     # Bittensor SDK wrapper (AsyncSubtensor)
│   │   ├── price_client.py     # MEXC price feed
│   │   ├── cache.py            # In-memory TTL cache
│   │   └── calculations.py     # Emission math
│   └── models/
│       └── schemas.py          # Pydantic response models
├── frontend/
│   ├── index.html              # Portfolio dashboard
│   ├── subnets.html            # Subnets overview
│   └── miners.html             # Miners/validators table
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

## How It Works

**Data sources:**
- Bittensor chain via `AsyncSubtensor` for metagraph, balances, stake info, subnet data
- MEXC public API for TAO/USDT price (no auth required, 500 req/10s limit)

**Emission calculation:**
```
alpha_per_day = meta.E[uid] / tempo * 7200
tao_per_day   = alpha_per_day * (pool.tao_in / pool.alpha_in)
usd_per_day   = tao_per_day * tao_price
```

Where `meta.E[uid]` is alpha per epoch, `tempo` is blocks per epoch (usually 360), and `7200` is blocks per day.

**Validator yield** is proportional to stake share: `yield = emission * (my_stake / total_stake_on_hotkey)`.

**Caching:** metagraph syncs are expensive (~10-20s cold). All queries are cached in-memory with configurable TTLs. Use `?refresh=true` on metagraph endpoints to force a fresh sync.

## Comparison to TaoStats

| Feature | TaoStats | OpenTaoAPI |
|---|---|---|
| Rate limit | 5 req/min (free) | None (self-hosted) |
| API key required | Yes | No |
| Source code | Closed | MIT open source |
| Self-hostable | No | Yes |
| Miner endpoint | `/api/miner/{coldkey}/{netuid}` | `/api/v1/miner/{coldkey}/{netuid}` (compatible format) |
| Web UI | Full explorer | Portfolio, subnets, miners/validators |
| Historical data | Yes | Not yet (current state only) |
| Price source | Multiple | MEXC |

## Support

If this project is useful to you, consider supporting development:

```
TAO: 5EhrSbeGeiLgsXcJTXXaBCcqrrMubvWcykSwk4Ho6KUd5sQG
```

## License

MIT
