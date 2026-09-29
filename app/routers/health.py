from fastapi import APIRouter

router = APIRouter(
    tags=["check health"]
)


@router.get("/health")
async def health():
    return {"status": "ok"}
