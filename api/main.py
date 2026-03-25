from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.config import settings
from api.services.cache import TTLCache
from api.services.chain_client import ChainClient
from api.services.price_client import PriceClient

from api.routes.price import router as price_router, init_price_router
from api.routes.miner import router as miner_router, init_miner_router
from api.routes.neuron import router as neuron_router, init_neuron_router
from api.routes.subnet import router as subnet_router, init_subnet_router
from api.routes.emissions import router as emissions_router, init_emissions_router
from api.routes.portfolio import router as portfolio_router, init_portfolio_router

cache = TTLCache()
chain_client = ChainClient(cache)
price_client = PriceClient(cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await chain_client.startup()
    await price_client.startup()

    # Initialize routes with service instances
    init_price_router(price_client)
    init_miner_router(chain_client, price_client)
    init_neuron_router(chain_client, price_client)
    init_subnet_router(chain_client, price_client)
    init_emissions_router(chain_client, price_client)
    init_portfolio_router(chain_client, price_client)

    yield

    await price_client.shutdown()
    await chain_client.shutdown()


app = FastAPI(
    title="OpenTaoAPI",
    description=(
        "Open-source Bittensor network explorer API. "
        "Self-hostable alternative to TaoStats and TaoMarketCap with no rate limits.\n\n"
        "**Data sources:** Bittensor chain (AsyncSubtensor) + MEXC (TAO price)\n\n"
        "**Web UI:** Portfolio viewer at `/`, subnets at `/subnets`, "
        "miners/validators at `/subnet/{netuid}/miners`\n\n"
        "**Source:** [GitHub](https://github.com)"
    ),
    version="0.2.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "price", "description": "TAO price from MEXC"},
        {"name": "portfolio", "description": "Cross-subnet coldkey portfolio"},
        {"name": "miner", "description": "TaoStats-compatible miner endpoint"},
        {"name": "neuron", "description": "Neuron lookup by UID, hotkey, or coldkey"},
        {"name": "subnet", "description": "Subnet info, metagraph, miners, and validators"},
        {"name": "emissions", "description": "Daily/monthly emission estimates"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(price_router, prefix="/api/v1")
app.include_router(miner_router, prefix="/api/v1")
app.include_router(neuron_router, prefix="/api/v1")
app.include_router(subnet_router, prefix="/api/v1")
app.include_router(emissions_router, prefix="/api/v1")
app.include_router(portfolio_router, prefix="/api/v1")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/health")
async def health():
    return {"status": "ok", "network": settings.bittensor_network}


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/subnets")
async def subnets_page():
    return FileResponse(FRONTEND_DIR / "subnets.html")


@app.get("/subnet/{netuid}/miners")
async def miners_page(netuid: int):
    return FileResponse(FRONTEND_DIR / "miners.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
