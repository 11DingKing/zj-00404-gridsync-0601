from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import acceptance, curtailment, reports, statistics, units
from app.seed import seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_if_empty()
    yield


app = FastAPI(
    title="机组并网与电量结算管理系统",
    version="1.0.0",
    description="管理机组并网验收、日发电上报、限发分摊，并按日/月/批次统计上网电量与结算电量。",
    lifespan=lifespan,
)

app.include_router(units.router)
app.include_router(acceptance.router)
app.include_router(reports.router)
app.include_router(curtailment.router)
app.include_router(statistics.router)


@app.get("/", tags=["root"])
def root():
    return {
        "service": "机组并网与电量结算管理系统",
        "docs": "/docs",
        "endpoints": [
            "/units",
            "/acceptance",
            "/reports",
            "/curtailments",
            "/statistics",
            "/statistics/settlement",
        ],
    }
