class FileService:
    """上传文件校验工具（静态方法）。

    历史上的 save_upload 落盘逻辑已随"上传即解析"改造移除：
    上传文件不再写入沙箱/宿主机，解析内容经 upload_files 表注入提示词。
    """

    @staticmethod
    def validate_file_size(content: bytes, max_size_mb: int = 10) -> bool:
        return len(content) <= max_size_mb * 1024 * 1024

    @staticmethod
    def validate_media_type(
        media_type: str,
        allowed_types: list[str],
    ) -> bool:
        return media_type in allowed_types
