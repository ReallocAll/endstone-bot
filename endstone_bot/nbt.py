"""Strict little-endian Bedrock NBT helpers used for level.dat validation.

The writer never serializes a complete NBT tree.  These helpers only parse and
locate fields so level_dat.py can apply a minimal binary patch.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

MAX_NBT_DEPTH = 256
MAX_COLLECTION_LENGTH = 1_000_000


@dataclass(frozen=True)
class FieldInfo:
    tag_type: int
    payload_offset: int


class NBTReader:
    def __init__(self, data: bytes, offset: int = 0, depth: int = 0) -> None:
        self.data = data
        self.pos = offset
        self.depth = depth

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise EOFError("NBT 数据意外结束")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def u8(self) -> int:
        return struct.unpack("<B", self.read(1))[0]

    def i8(self) -> int:
        return struct.unpack("<b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def string(self) -> str:
        length = self.u16()
        return self.read(length).decode("utf-8")

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_NBT_DEPTH:
            raise ValueError(f"NBT 递归深度超限: {self.depth}")

    def _leave(self) -> None:
        self.depth -= 1

    @staticmethod
    def _check_count(count: int, kind: str) -> None:
        if count < 0 or count > MAX_COLLECTION_LENGTH:
            raise ValueError(f"NBT {kind} 长度异常: {count}")

    def skip_payload(self, tag_type: int) -> None:
        if tag_type == TAG_BYTE:
            self.read(1)
        elif tag_type == TAG_SHORT:
            self.read(2)
        elif tag_type in (TAG_INT, TAG_FLOAT):
            self.read(4)
        elif tag_type in (TAG_LONG, TAG_DOUBLE):
            self.read(8)
        elif tag_type == TAG_BYTE_ARRAY:
            count = self.i32()
            self._check_count(count, "byte array")
            self.read(count)
        elif tag_type == TAG_STRING:
            self.read(self.u16())
        elif tag_type == TAG_LIST:
            element_type = self.u8()
            count = self.i32()
            self._check_count(count, "list")
            if count and element_type == TAG_END:
                raise ValueError("非空 NBT list 的元素类型不能是 TAG_END")
            self._enter()
            try:
                for _ in range(count):
                    self.skip_payload(element_type)
            finally:
                self._leave()
        elif tag_type == TAG_COMPOUND:
            self._enter()
            try:
                while True:
                    child_type = self.u8()
                    if child_type == TAG_END:
                        break
                    self.string()
                    self.skip_payload(child_type)
            finally:
                self._leave()
        elif tag_type == TAG_INT_ARRAY:
            count = self.i32()
            self._check_count(count, "int array")
            self.read(count * 4)
        elif tag_type == TAG_LONG_ARRAY:
            count = self.i32()
            self._check_count(count, "long array")
            self.read(count * 8)
        else:
            raise ValueError(f"未知 NBT 标签类型: {tag_type}")

    def payload(self, tag_type: int) -> Any:
        if tag_type == TAG_BYTE:
            return self.i8()
        if tag_type == TAG_SHORT:
            return self.i16()
        if tag_type == TAG_INT:
            return self.i32()
        if tag_type == TAG_LONG:
            return self.i64()
        if tag_type == TAG_FLOAT:
            return self.f32()
        if tag_type == TAG_DOUBLE:
            return self.f64()
        if tag_type == TAG_BYTE_ARRAY:
            count = self.i32()
            self._check_count(count, "byte array")
            return list(struct.unpack(f"<{count}b", self.read(count))) if count else []
        if tag_type == TAG_STRING:
            return self.string()
        if tag_type == TAG_LIST:
            element_type = self.u8()
            count = self.i32()
            self._check_count(count, "list")
            if count and element_type == TAG_END:
                raise ValueError("非空 NBT list 的元素类型不能是 TAG_END")
            self._enter()
            try:
                return [self.payload(element_type) for _ in range(count)]
            finally:
                self._leave()
        if tag_type == TAG_COMPOUND:
            self._enter()
            try:
                return self.compound()
            finally:
                self._leave()
        if tag_type == TAG_INT_ARRAY:
            count = self.i32()
            self._check_count(count, "int array")
            return list(struct.unpack(f"<{count}i", self.read(count * 4))) if count else []
        if tag_type == TAG_LONG_ARRAY:
            count = self.i32()
            self._check_count(count, "long array")
            return list(struct.unpack(f"<{count}q", self.read(count * 8))) if count else []
        raise ValueError(f"未知 NBT 标签类型: {tag_type}")

    def compound(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            tag_type = self.u8()
            if tag_type == TAG_END:
                return result
            name = self.string()
            if name in result:
                raise ValueError(f"NBT Compound 中存在重复字段: {name}")
            result[name] = self.payload(tag_type)

    def root(self) -> dict[str, Any]:
        if self.u8() != TAG_COMPOUND:
            raise ValueError("level.dat 根标签不是 Compound")
        self.string()
        return self.compound()


def parse_level_dat_header(data: bytes) -> tuple[int, int]:
    if len(data) < 9:
        raise ValueError("level.dat 太短")
    version, declared_length = struct.unpack("<II", data[:8])
    actual_length = len(data) - 8
    if declared_length != actual_length:
        raise ValueError(
            f"level.dat NBT 长度头不匹配: declared={declared_length}, actual={actual_length}"
        )
    return version, declared_length


def read_bedrock_nbt(data: bytes, *, has_header: bool = False, strict: bool = True) -> dict[str, Any]:
    offset = 0
    if has_header:
        parse_level_dat_header(data)
        offset = 8
    reader = NBTReader(data, offset)
    value = reader.root()
    if strict and reader.pos != len(data):
        raise ValueError(f"NBT 根标签后存在 {len(data) - reader.pos} 个尾随字节")
    return value


def get_root_body_offset(data: bytes, *, has_header: bool = False) -> int:
    offset = 8 if has_header else 0
    if has_header:
        parse_level_dat_header(data)
    reader = NBTReader(data, offset)
    if reader.u8() != TAG_COMPOUND:
        raise ValueError("根标签不是 Compound")
    reader.string()
    return reader.pos


def scan_compound_fields(data: bytes, compound_body_offset: int) -> tuple[dict[str, FieldInfo], int]:
    reader = NBTReader(data, compound_body_offset)
    fields: dict[str, FieldInfo] = {}
    while True:
        tag_offset = reader.pos
        tag_type = reader.u8()
        if tag_type == TAG_END:
            return fields, tag_offset
        name = reader.string()
        if name in fields:
            raise ValueError(f"NBT Compound 中存在重复字段: {name}")
        fields[name] = FieldInfo(tag_type=tag_type, payload_offset=reader.pos)
        reader.skip_payload(tag_type)


def make_byte_tag_bytes(name: str, value: int) -> bytes:
    encoded = name.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("NBT tag name 太长")
    return (
        struct.pack("<B", TAG_BYTE)
        + struct.pack("<H", len(encoded))
        + encoded
        + struct.pack("<b", int(value))
    )


def make_compound_tag_bytes(name: str, body: bytes) -> bytes:
    encoded = name.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("NBT tag name 太长")
    return (
        struct.pack("<B", TAG_COMPOUND)
        + struct.pack("<H", len(encoded))
        + encoded
        + body
        + struct.pack("<B", TAG_END)
    )
