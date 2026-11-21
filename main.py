from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

DB_PATH = Path("counterparty.db")
CACHE_TTL_HOURS = 24


class CompanyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    LIQUIDATED = "LIQUIDATED"
    BANKRUPT = "BANKRUPT"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CompanyResponse(BaseModel):
    inn: str
    name: str
    status: CompanyStatus
    risk_level: RiskLevel
    updated_at: datetime
    source: str


class BatchItemResult(BaseModel):
    inn: str
    is_valid_format: bool
    found: bool
    name: str | None = None
    status: CompanyStatus | None = None
    risk_level: RiskLevel | None = None
    source: str | None = None
    error: str | None = None


class BatchCheckRequest(BaseModel):
    inns: list[str] = Field(..., max_length=10)


class BatchCheckResponse(BaseModel):
    total: int
    items: list[BatchItemResult]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                inn TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def validate_inn(inn: str) -> bool:
    if len(inn) != 10 or not inn.isdigit():
        return False
    digits = [int(c) for c in inn]
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    checksum = (sum(d * w for d, w in zip(digits[:9], weights)) % 11) % 10
    return digits[9] == checksum


FNS_MOCK_REGISTRY = {
    "7707083893": {"name": 'ПАО "Сбербанк"', "status": CompanyStatus.ACTIVE, "risk_level": RiskLevel.LOW},
    "7736207543": {"name": 'ООО "Яндекс"', "status": CompanyStatus.ACTIVE, "risk_level": RiskLevel.LOW},
    "7701107869": {"name": 'ООО "Траст-Инвест"', "status": CompanyStatus.LIQUIDATED, "risk_level": RiskLevel.MEDIUM},
    "7710140679": {"name": 'ЗАО "СтройКомплекс"', "status": CompanyStatus.BANKRUPT, "risk_level": RiskLevel.HIGH},
}


def fetch_from_external_fns(inn: str) -> dict:
    if inn == "0000000000":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="EXTERNAL_SERVICE_UNAVAILABLE",
        )

    company = FNS_MOCK_REGISTRY.get(inn)
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="COMPANY_NOT_FOUND",
        )
    return company


def check_single_company(inn: str) -> CompanyResponse:
    if not validate_inn(inn):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INVALID_INN",
        )

    now = datetime.now(timezone.utc)

    with get_db() as conn:
        row = conn.execute("SELECT * FROM companies WHERE inn = ?", (inn,)).fetchone()

        if row:
            updated_at = datetime.fromisoformat(row["updated_at"])
            if now - updated_at < timedelta(hours=CACHE_TTL_HOURS):
                return CompanyResponse(
                    inn=row["inn"],
                    name=row["name"],
                    status=CompanyStatus(row["status"]),
                    risk_level=RiskLevel(row["risk_level"]),
                    updated_at=updated_at,
                    source="cache",
                )

        external_data = fetch_from_external_fns(inn)
        now_str = now.isoformat()

        conn.execute(
            """
            INSERT INTO companies (inn, name, status, risk_level, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(inn) DO UPDATE SET
                name = excluded.name,
                status = excluded.status,
                risk_level = excluded.risk_level,
                updated_at = excluded.updated_at
            """,
            (
                inn,
                external_data["name"],
                external_data["status"].value,
                external_data["risk_level"].value,
                now_str,
            ),
        )

        return CompanyResponse(
            inn=inn,
            name=external_data["name"],
            status=external_data["status"],
            risk_level=external_data["risk_level"],
            updated_at=now,
            source="external_api",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Counterparty Check Service",
    description="Сервис проверки контрагентов по ИНН с кэшированием ответов ФНС",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/v1/companies/{inn}", response_model=CompanyResponse)
def get_company(inn: str):
    return check_single_company(inn)


@app.post("/api/v1/companies/batch", response_model=BatchCheckResponse)
def batch_check(request: BatchCheckRequest):
    results = []
    for raw_inn in request.inns:
        inn = raw_inn.strip()
        if not validate_inn(inn):
            results.append(
                BatchItemResult(
                    inn=inn,
                    is_valid_format=False,
                    found=False,
                    error="INVALID_INN",
                )
            )
            continue

        try:
            company = check_single_company(inn)
            results.append(
                BatchItemResult(
                    inn=inn,
                    is_valid_format=True,
                    found=True,
                    name=company.name,
                    status=company.status,
                    risk_level=company.risk_level,
                    source=company.source,
                )
            )
        except HTTPException as exc:
            results.append(
                BatchItemResult(
                    inn=inn,
                    is_valid_format=True,
                    found=False,
                    error=str(exc.detail),
                )
            )

    return BatchCheckResponse(total=len(results), items=results)
