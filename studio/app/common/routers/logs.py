from typing import List

from fastapi import APIRouter, HTTPException, Query

from studio.app.common.core.utils.log_reader import LogReader
from studio.app.common.schemas.files import LogLevel

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get(
    "",
    summary="Fetch log data with pagination",
)
async def get_log_data(
    offset: int = Query(
        default=-1,
        ge=-1,
        description="The starting position in the log file from which to fetch data."
        "A value of `-1` indicates the request should start from the end of the file",
    ),
    limit: int = Query(
        default=50,
        gt=0,
        description="Max number of log unit to return.",
    ),
    reverse: bool = Query(
        default=True,
        description="Fetch logs in reverse order.",
    ),
    levels: List[LogLevel] = Query(default=[LogLevel.ALL]),
):
    try:
        return LogReader(levels=levels).read_from_offset(
            offset=offset,
            limit=limit,
            reverse=reverse,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
