"""会话历史生成文件：验证归属、列举备份、按需恢复二进制原件。"""
import asyncio
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("请提供历史文件清单中的相对路径")
    value = value.replace("\\", "/")
    if value.startswith("/") or ":" in value or any(p in (".", "..") for p in value.split("/")):
        raise ValueError("历史文件路径必须位于当前会话内")
    return str(PurePosixPath(value))


class HistoricalFileService:
    """每轮构造一次；不持有全局用户状态，不下载模型提供的 URL。"""

    def __init__(self, session_service, adapter, user_id, session_id, persist_dir,
                 *, restored_files=None):
        self.session_service = session_service
        self.adapter = adapter
        self.user_id = str(user_id or "")
        self.session_id = str(session_id or "")
        self.persist_dir = Path(persist_dir) if persist_dir else None
        self.restored_files = restored_files if restored_files is not None else {}
        self._locks = {}

    async def _index(self):
        sid = self.session_id
        if not self.user_id or not sid or sid in (".", "..") or any(c in sid for c in "/\\:\x00"):
            raise PermissionError("会话不存在或无权访问历史文件")
        meta = await self.session_service.dao.get_session_meta(sid)
        if not meta or str(meta.get("user_id") or "") != self.user_id or meta.get("deleted_at"):
            raise PermissionError("会话不存在或无权访问历史文件")
        rows = await self.session_service.load_session_files(sid)
        files = []
        for row in rows:
            try:
                path = _relative_path(row.get("path", ""))
            except ValueError:
                continue
            files.append({**row, "path": path})
        return files

    def _backed_up(self, row):
        expected = f"/persist-files/{self.session_id}/{row['path']}"
        url = row.get("url") or ""
        return bool(self.persist_dir) and (
            url == expected or unquote(urlsplit(url).path) == expected
        )

    async def list_files(self, query: str = "", offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset 必须非负，limit 必须在 1 到 100 之间")
        rows = await self._index()
        if query:
            needle = query.casefold()
            rows = [r for r in rows if needle in r['path'].casefold() or needle in str(r.get('name', '')).casefold()]
        files = [{
            "name": r.get("name") or PurePosixPath(r["path"]).name,
            "path": r["path"], "size": r.get("size"),
            "created_at": r.get("created_at"), "message_pair_id": r.get("message_pair_id"),
            "backed_up": self._backed_up(r),
        } for r in rows[offset:offset + limit]]
        next_offset = offset + len(files)
        return {"files": files, "total": len(rows),
                "next_offset": next_offset if next_offset < len(rows) else None,
                "note": "历史清单不代表文件已在工作区。修改前调用 RestoreHistoryFile，使用精确 path；不同版本优先按文件名、路径和消息配对标识确认。"}

    def _read_backup(self, row):
        if not self._backed_up(row):
            raise ValueError("该历史文件没有可用的持久化备份，请重新上传原文件")
        root = self.persist_dir.resolve()
        session_root = root / self.session_id
        path = session_root.joinpath(*PurePosixPath(row['path']).parts).resolve()
        # 保留未 resolve 的会话边界，拒绝会话目录或子目录链接到另一会话。
        if not path.is_relative_to(session_root):
            raise ValueError("历史文件备份路径不在当前会话内")
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("历史文件备份暂不可用，请检查共享持久化存储或重新上传原文件") from exc
        except OSError as exc:
            raise ValueError("历史文件备份暂时无法读取，请检查持久化存储后重试") from exc
        size = row.get("size")
        if size is not None and len(content) != int(size):
            raise ValueError("历史文件备份大小与记录不一致，已停止恢复，请检查备份")
        return content

    async def restore_file(self, file_path: str):
        requested = _relative_path(file_path)
        rows = await self._index()
        exact = [r for r in rows if r['path'] == requested]
        matches = exact or [r for r in rows if PurePosixPath(r['path']).name == requested]
        if not matches:
            raise ValueError("当前会话没有该历史文件，请先调用 ListHistoryFiles 确认路径")
        if len(matches) != 1:
            raise ValueError("存在多个同名历史文件，请使用 ListHistoryFiles 返回的精确相对路径")
        row = matches[0]
        path = row['path']
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            if await self.adapter.file_signature(path) is not None:
                return self._result(path, False)
            content = await asyncio.to_thread(self._read_backup, row)
            restored, signature = await self.adapter.restore_file_if_missing(path, content)
            if restored:
                self.restored_files[path] = signature
            return self._result(path, restored)

    def _result(self, path, restored):
        return {"status": "restored" if restored else "already_present",
                "path": f"{self.adapter.workdir.rstrip('/')}/{path}",
                "message": "文件已可供当前工作区读取；修改时另存新文件名，保留历史原件。"}
