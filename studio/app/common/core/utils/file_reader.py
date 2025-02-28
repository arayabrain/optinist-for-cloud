import json
import os
from io import BufferedReader
from typing import List

from studio.app.common.schemas.files import LogLevel
from studio.app.common.schemas.outputs import (
    JsonTimeSeriesData,
    OutputData,
    PaginatedLineResult,
    PlotMetaData,
)
from studio.app.dir_path import DIRPATH


class Reader:
    @classmethod
    def read(cls, filepath):
        with open(filepath, "r") as f:
            data = f.read()
        return data

    @classmethod
    def read_as_output(cls, filepath) -> OutputData:
        return OutputData(cls.read(filepath))


class JsonReader:
    @classmethod
    def read(cls, filepath):
        with open(filepath, "r") as f:
            json_data = json.load(f)
        return json_data

    @classmethod
    def read_as_output(cls, filepath) -> OutputData:
        json_data = cls.read(filepath)
        plot_metadata_path = f"{os.path.splitext(filepath)[0]}.plot-meta.json"
        plot_metadata = cls.read_as_plot_meta(plot_metadata_path)

        return OutputData(
            data=json_data["data"],
            columns=json_data["columns"],
            index=json_data["index"],
            meta=plot_metadata,
        )

    @classmethod
    def read_as_timeseries(cls, filepath) -> JsonTimeSeriesData:
        json_data = cls.read(filepath)
        return JsonTimeSeriesData(
            xrange=list(json_data["data"].keys()),
            data=json_data["data"],
            std=json_data["std"] if "std" in json_data else None,
        )

    @classmethod
    def read_as_plot_meta(cls, filepath) -> PlotMetaData:
        json_data = cls.read(filepath) if os.path.exists(filepath) else {}
        return PlotMetaData(**json_data)


class FileLinesReader:
    def __init__(self, file_path, **kwargs):
        if not os.path.exists(file_path):
            raise Exception(f"{file_path} does not exist.")
        self.file_path = file_path

    def _validate_line(self, line: bytes) -> bool:
        """
        Condition to check whether line should be included in output data.
        Modify this if you want to filter content.
        """
        return True

    def _read_forward_lines(
        self, file: BufferedReader, offset: int, limit: int
    ) -> PaginatedLineResult:
        file.seek(offset)
        lines = []

        while len(lines) < limit:
            line = file.readline()
            if not line:
                break

            if self._validate_line(line):
                lines.append(line)

        next_offset = file.tell()
        return PaginatedLineResult(
            next_offset=next_offset,
            prev_offset=offset,
            data=[line.decode().strip() for line in lines],
        )

    def _read_backward_lines(
        self, file: BufferedReader, offset: int, limit: int, read_chunk_size=1024
    ) -> PaginatedLineResult:
        file.seek(0, 2)
        file_size = file.tell()
        next_offset = offset = min(offset, file_size)

        segment = b""
        lines = []
        while offset > 0 and len(lines) < limit:
            chunk_size = min(read_chunk_size, offset)
            offset -= chunk_size
            file.seek(offset)

            buffer = file.read(chunk_size)

            # Append previous segment
            if segment:
                buffer += segment
                segment = b""

            buffer_lines = buffer.splitlines(keepends=True)

            # first line may be incomplete, save it as segment to append to next chunk
            if buffer_lines:
                if offset > 0:
                    segment = buffer_lines.pop(0)

            # Prepend valid lines to `lines`
            len_buf_lines = len(buffer_lines)
            for i in range(len_buf_lines - 1, -1, -1):
                line = buffer_lines.pop(i)
                if self._validate_line(line):
                    lines.insert(0, line)

                    if len(lines) == limit:
                        segment += b"".join(buffer_lines)
                        break

        prev_offset = offset
        if segment:
            prev_offset = offset + len(segment)

        return PaginatedLineResult(
            next_offset=next_offset,
            prev_offset=prev_offset,
            data=[line.decode().strip() for line in lines],
        )

    def read_lines_from_offset(
        self,
        offset: int,
        limit: int,
        reverse: bool = False,
    ) -> PaginatedLineResult:
        with open(self.file_path, "rb") as file:
            if offset == -1:
                file.seek(0, 2)
                offset = file.tell()

            if reverse:
                return self._read_backward_lines(file, offset, limit)
            else:
                return self._read_forward_lines(file, offset, limit)


class LogReader(FileLinesReader):
    def __init__(
        self,
        file_path=DIRPATH.LOG_FILE_PATH,
        levels: List[LogLevel] = [],
        **kwargs,
    ):
        super().__init__(file_path, **kwargs)
        self.file_path = file_path

        if LogLevel.ALL in levels:
            self.levels = []
        else:
            self.levels = [level.value.encode() for level in levels]

    def _validate_line(self, line: bytes) -> bool:
        if self.levels:
            return any([level in line for level in self.levels])

        return True

    def read_lines(
        self,
        offset: int,
        limit: int = 50,
        reverse: bool = False,
    ):
        return self.read_lines_from_offset(offset=offset, limit=limit, reverse=reverse)
