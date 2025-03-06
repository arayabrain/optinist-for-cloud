from typing import List

from fastapi import APIRouter, HTTPException, Query
from typing_extensions import Optional

from studio.app.common.core.utils.log_reader import LogLevel, LogReader
from studio.app.common.schemas.outputs import PaginatedLineResult

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
        ge=0,
        description="Max number of log unit to return.",
    ),
    reverse: bool = Query(
        default=True,
        description="Fetch logs in reverse order.",
    ),
    search: Optional[str] = Query(default=None),
    levels: List[LogLevel] = Query(default=[LogLevel.ALL]),
):
    try:
        stop_offset = None
        log_reader = LogReader(levels=levels)

        if search:
            text_pos = log_reader.get_text_position(search, offset, reverse)
            stop_offset = log_reader.get_unit_position(search, text_pos, reverse)
            if stop_offset is None:
                return PaginatedLineResult(
                    next_offset=offset,
                    prev_offset=offset,
                    data=[],
                )

            limit = 0
            if reverse:
                reverse = False
                offset, stop_offset = stop_offset, offset

        return log_reader.read_from_offset(
            offset=offset,
            stop_offset=stop_offset,
            limit=limit,
            reverse=reverse,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
