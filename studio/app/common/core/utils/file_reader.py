import json
import os
from abc import ABC, abstractmethod
from io import BufferedReader

from studio.app.common.schemas.outputs import (
    JsonTimeSeriesData,
    OutputData,
    PaginatedLineResult,
    PlotMetaData,
)


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


class ContentUnitReader(ABC):
    """Abstract base class for defining how to read content units"""

    @abstractmethod
    def is_unit_start(self, line: bytes) -> bool:
        """Determine if a line starts a new content unit"""
        pass

    @abstractmethod
    def parse(self, content: bytes) -> dict:
        """Parse a content unit into a structured format"""
        pass

    def validate(self, content: bytes) -> bool:
        """
        Condition to check whether line should be included in output data.
        """
        return True


class LineReader(ContentUnitReader):
    """Simple line reader that treats each line as a unit"""

    def is_unit_start(self, line: bytes) -> bool:
        return True

    def parse(self, content: bytes) -> dict:
        return {"raw": content}


class FileReader:
    def __init__(self, file_path, **kwargs):
        if not os.path.exists(file_path):
            raise Exception(f"{file_path} does not exist.")
        self.file_path = file_path
        self.unit_reader = LineReader()

    def _read_forward(
        self, file: BufferedReader, offset: int, limit: int
    ) -> PaginatedLineResult:
        file.seek(offset)

        units = []
        current_unit_buffer = b""
        while len(units) < limit:
            line = file.readline()
            if not line:
                break

            if self.unit_reader.is_unit_start(line):
                if current_unit_buffer:
                    if self.unit_reader.validate(current_unit_buffer):
                        units.append(current_unit_buffer)
                current_unit_buffer = line
            else:
                current_unit_buffer += line

        next_offset = file.tell() - len(current_unit_buffer)
        return PaginatedLineResult(
            next_offset=next_offset,
            prev_offset=offset,
            data=[line.decode().strip() for line in units],
        )

    def _read_backward(
        self, file: BufferedReader, offset: int, limit: int, read_chunk_size=1024
    ) -> PaginatedLineResult:
        file.seek(0, 2)
        file_size = file.tell()
        next_offset = offset = min(offset, file_size)

        segment = b""
        current_unit_buffer = b""
        units = []
        while offset > 0 and len(units) < limit:
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
            if buffer_lines and offset > 0:
                segment = buffer_lines.pop(0)

            # Prepend valid lines to `lines`
            len_buf_lines = len(buffer_lines)
            for i in range(len_buf_lines - 1, -1, -1):
                line = buffer_lines.pop(i)

                current_unit_buffer = line + current_unit_buffer
                if self.unit_reader.is_unit_start(line):
                    if self.unit_reader.validate(current_unit_buffer):
                        units.insert(0, current_unit_buffer)
                        current_unit_buffer = b""
                        if len(units) == limit:
                            segment += b"".join(buffer_lines)
                            break
                    else:
                        current_unit_buffer = b""

        prev_offset = offset
        if segment:
            prev_offset = offset + len(segment)

        return PaginatedLineResult(
            next_offset=next_offset,
            prev_offset=prev_offset,
            data=[line.decode().strip() for line in units],
        )

    def read_from_offset(
        self,
        offset: int,
        limit: int = 50,
        reverse: bool = False,
    ) -> PaginatedLineResult:
        with open(self.file_path, "rb") as file:
            if offset == -1:
                file.seek(0, 2)
                offset = file.tell()

            if reverse:
                return self._read_backward(file, offset, limit)
            else:
                return self._read_forward(file, offset, limit)
