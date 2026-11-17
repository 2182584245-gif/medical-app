"""Account-bound private avatar cache; never exposes a path to JavaScript."""
from __future__ import annotations

import base64
import binascii
import json
import os
import hashlib
from pathlib import Path
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_AVATAR_BYTES = 256_000
MAX_AVATAR_DATA = 350_000


def validate_avatar_data(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("data:image/png;base64,") or len(value) > MAX_AVATAR_DATA:
        raise ValueError("请选择应用转换后的 PNG 头像。")
    try:
        data = base64.b64decode(value.split(",", 1)[1], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("头像图片无法读取。") from None
    if not 45 <= len(data) <= MAX_AVATAR_BYTES or data[:8] != PNG_SIGNATURE:
        raise ValueError("头像图片格式或大小不符合要求。")
    offset, width, height, channels = 8, 0, 0, 0
    compressed = bytearray()
    ended = False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if length > MAX_AVATAR_BYTES or end > len(data):
            raise ValueError("头像图片不完整。")
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        expected = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk) & 0xffffffff != expected:
            raise ValueError("头像图片校验失败。")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError("头像图片头无效。")
            width, height, bits, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if not (1 <= width <= 256 and 1 <= height <= 256) or bits != 8 or color not in (2, 6) or any((compression, filtering, interlace)):
                raise ValueError("头像应为不超过256像素的应用转换图片。")
            channels = 3 if color == 2 else 4
        elif kind == b"IHDR":
            raise ValueError("头像图片结构无效。")
        if kind == b"IDAT":
            compressed.extend(chunk)
        if kind == b"IEND":
            if length or end != len(data):
                raise ValueError("头像图片结尾无效。")
            ended = True
            break
        offset = end
    if not ended or not compressed:
        raise ValueError("头像图片不完整。")
    expected_size = (width * channels + 1) * height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected_size + 1)
        if len(pixels) != expected_size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError
        if any(pixels[row * (width * channels + 1)] > 4 for row in range(height)):
            raise ValueError
    except (ValueError, zlib.error):
        raise ValueError("头像图片像素无法读取。") from None
    return value


class AvatarStore:
    def __init__(self, private_root, *, vault=None, endpoint=''):
        self.root = Path(private_root).resolve() / "member-avatars"
        self.vault=vault
        self.purpose='mobile149-avatar/'+hashlib.sha256(endpoint.rstrip('/').encode()).hexdigest()

    def _path(self, actor: int) -> Path:
        if type(actor) is not int or actor <= 0:
            raise ValueError("请先登录会员账号。")
        return self.root / (str(actor) + (".sealed" if self.vault is not None else ".json"))

    def get(self, actor: int) -> dict:
        path = self._path(actor)
        if self.root.is_symlink():raise ValueError('头像存储不可用。')
        if self.vault is not None and not path.exists():
            legacy=self.root/(str(actor)+'.json')
            if legacy.is_file() and not legacy.is_symlink() and legacy.stat().st_size<=360000:
                try:
                    value=json.loads(legacy.read_text(encoding='utf8'))
                    return self.save(actor,value['avatar_data'],value['sync_pending'])
                except (ValueError,KeyError,TypeError):pass
        if not path.is_file() or path.is_symlink():
            return {"avatar_data": "", "sync_pending": False}
        try:
            if path.stat().st_size > (500_000 if self.vault is not None else 360_000):
                raise ValueError
            text=path.read_text(encoding='utf8')
            if self.vault is not None:text=str(self.vault.open(actor,self.purpose,text))
            value = json.loads(text)
            data = validate_avatar_data(value["avatar_data"])
            if type(value["sync_pending"]) is not bool:
                raise ValueError
            return {"avatar_data": data, "sync_pending": value["sync_pending"]}
        except Exception:
            if self.vault is not None:raise ValueError('本机头像暂时无法解密，请重新登录后同步。') from None
            return {"avatar_data": "", "sync_pending": False}

    def save(self, actor: int, data: str, pending: bool) -> dict:
        path = self._path(actor)
        data = validate_avatar_data(data)
        if type(pending) is not bool:
            raise ValueError("头像同步状态无效。")
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError("头像存储不可用。")
        self.root.mkdir(parents=True, exist_ok=True)
        value = {"avatar_data": data, "sync_pending": pending}
        temporary = path.with_suffix(".tmp")
        if temporary.is_symlink():
            raise ValueError("头像存储不可用。")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
                stream.write(str(self.vault.seal(actor,self.purpose,text)) if self.vault is not None else text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if self.vault is not None:
                verified=json.loads(str(self.vault.open(actor,self.purpose,path.read_text(encoding='utf8'))))
                if verified!=value:raise ValueError('头像加密校验失败，原资料已保留。')
                legacy=self.root/(str(actor)+'.json')
                if legacy.is_file() and not legacy.is_symlink():legacy.unlink()
        finally:
            if temporary.exists():
                temporary.unlink()
        return value

    def delete(self,actor:int):
        self._path(actor)
        if self.root.is_symlink():raise ValueError('头像存储不可用。')
        for suffix in ('.sealed','.json','.tmp'):
            path=self.root/(str(actor)+suffix)
            if path.is_symlink():raise ValueError('头像存储不可用。')
            path.unlink(missing_ok=True)
