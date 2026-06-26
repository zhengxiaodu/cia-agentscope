import uuid
import os
from pathlib import Path
from agentscope.message import DataBlock, URLSource
from pydantic import AnyUrl


class FileService:
    def __init__(self, workdir: str):
        self.workdir = workdir

    async def save_upload(
        self,
        session_id: str | None,
        filename: str,
        content: bytes,
        media_type: str,
    ) -> DataBlock:
        if session_id:
            data_dir = Path(self.workdir) / "data" / session_id
        else:
            data_dir = Path(self.workdir) / "data"

        data_dir.mkdir(parents=True, exist_ok=True)

        unique_name = f"{uuid.uuid4().hex}_{filename}"
        file_path = data_dir / unique_name

        file_path.write_bytes(content)

        return DataBlock(
            id=uuid.uuid4().hex,
            name=filename,
            source=URLSource(
                url=AnyUrl(Path(file_path).resolve().as_uri()),
                media_type=media_type,
            ),
        )

    @staticmethod
    def validate_file_size(content: bytes, max_size_mb: int = 10) -> bool:
        return len(content) <= max_size_mb * 1024 * 1024

    @staticmethod
    def validate_media_type(
        media_type: str,
        allowed_types: list[str],
    ) -> bool:
        return media_type in allowed_types