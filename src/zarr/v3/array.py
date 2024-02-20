from __future__ import annotations

# Notes on what I've changed here:
# 1. Split Array into AsyncArray and Array
# 3. Added .size and .attrs methods
# 4. Temporarily disabled the creation of ArrayV2
# 5. Added from_dict to AsyncArray

# Questions to consider:
# 1. Was splitting the array into two classes really necessary?
# 2. Do we really need runtime_configuration? Specifically, the asyncio_loop seems problematic


from dataclasses import dataclass, replace

import json
from typing import Any, Dict, Iterable, Literal, Optional, Tuple, Union

import numpy as np
from zarr.v3.abc.codec import Codec


# from zarr.v3.array_v2 import ArrayV2
from zarr.v3.codecs import BytesCodec
from zarr.v3.common import (
    ZARR_JSON,
    ChunkCoords,
    Selection,
    concurrent_map,
)
from zarr.v3.config import RuntimeConfiguration
from zarr.v3.indexing import BasicIndexer, all_chunk_coords
from zarr.v3.chunk_grids import RegularChunkGrid
from zarr.v3.chunk_key_encodings import DefaultChunkKeyEncoding, V2ChunkKeyEncoding
from zarr.v3.metadata import ArrayMetadata
from zarr.v3.store import StoreLike, StorePath, make_store_path
from zarr.v3.sync import sync


def parse_array_metadata(data: Any):
    if isinstance(data, ArrayMetadata):
        return data
    elif isinstance(data, dict):
        return ArrayMetadata.from_dict(data)
    else:
        raise TypeError


@dataclass(frozen=True)
class AsyncArray:
    metadata: ArrayMetadata
    store_path: StorePath
    runtime_configuration: RuntimeConfiguration

    @property
    def codecs(self):
        return self.metadata.codecs

    def __init__(
        self,
        metadata: ArrayMetadata,
        store_path: StorePath,
        runtime_configuration: RuntimeConfiguration,
    ):
        metadata_parsed = parse_array_metadata(metadata)

        object.__setattr__(self, "metadata", metadata_parsed)
        object.__setattr__(self, "store_path", store_path)
        object.__setattr__(self, "runtime_configuration", runtime_configuration)

    @classmethod
    async def create(
        cls,
        store: StoreLike,
        *,
        shape: ChunkCoords,
        dtype: Union[str, np.dtype],
        chunk_shape: ChunkCoords,
        fill_value: Optional[Any] = None,
        chunk_key_encoding: Union[
            Tuple[Literal["default"], Literal[".", "/"]],
            Tuple[Literal["v2"], Literal[".", "/"]],
        ] = ("default", "/"),
        codecs: Optional[Iterable[Union[Codec, Dict[str, Any]]]] = None,
        dimension_names: Optional[Iterable[str]] = None,
        attributes: Optional[Dict[str, Any]] = None,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
        exists_ok: bool = False,
    ) -> AsyncArray:
        store_path = make_store_path(store)
        if not exists_ok:
            assert not await (store_path / ZARR_JSON).exists()

        codecs = list(codecs) if codecs is not None else [BytesCodec()]

        if fill_value is None:
            if dtype == np.dtype("bool"):
                fill_value = False
            else:
                fill_value = 0

        metadata = ArrayMetadata(
            shape=shape,
            data_type=dtype,
            chunk_grid=RegularChunkGrid(chunk_shape=chunk_shape),
            chunk_key_encoding=(
                V2ChunkKeyEncoding(separator=chunk_key_encoding[1])
                if chunk_key_encoding[0] == "v2"
                else DefaultChunkKeyEncoding(separator=chunk_key_encoding[1])
            ),
            fill_value=fill_value,
            codecs=codecs,
            dimension_names=tuple(dimension_names) if dimension_names else None,
            attributes=attributes or {},
        )
        runtime_configuration = runtime_configuration or RuntimeConfiguration()

        array = cls(
            metadata=metadata,
            store_path=store_path,
            runtime_configuration=runtime_configuration,
        )

        await array._save_metadata()
        return array

    @classmethod
    def from_dict(
        cls,
        store_path: StorePath,
        data: Dict[str, Any],
        runtime_configuration: RuntimeConfiguration,
    ) -> AsyncArray:
        metadata = ArrayMetadata.from_dict(data)
        async_array = cls(
            metadata=metadata, store_path=store_path, runtime_configuration=runtime_configuration
        )
        return async_array

    @classmethod
    async def open(
        cls,
        store: StoreLike,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
    ) -> AsyncArray:
        store_path = make_store_path(store)
        zarr_json_bytes = await (store_path / ZARR_JSON).get()
        assert zarr_json_bytes is not None
        return cls.from_dict(
            store_path,
            json.loads(zarr_json_bytes),
            runtime_configuration=runtime_configuration,
        )

    @classmethod
    async def open_auto(
        cls,
        store: StoreLike,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
    ) -> AsyncArray:  # TODO: Union[AsyncArray, ArrayV2]
        store_path = make_store_path(store)
        v3_metadata_bytes = await (store_path / ZARR_JSON).get()
        if v3_metadata_bytes is not None:
            return cls.from_dict(
                store_path,
                json.loads(v3_metadata_bytes),
                runtime_configuration=runtime_configuration or RuntimeConfiguration(),
            )
        else:
            raise ValueError("no v2 support yet")
            # return await ArrayV2.open(store_path)

    @property
    def ndim(self) -> int:
        return len(self.metadata.shape)

    @property
    def shape(self) -> ChunkCoords:
        return self.metadata.shape

    @property
    def size(self) -> int:
        return np.prod(self.metadata.shape)

    @property
    def dtype(self) -> np.dtype:
        return self.metadata.dtype

    @property
    def attrs(self) -> dict:
        return self.metadata.attributes

    async def getitem(self, selection: Selection):
        assert isinstance(self.metadata.chunk_grid, RegularChunkGrid)
        indexer = BasicIndexer(
            selection,
            shape=self.metadata.shape,
            chunk_shape=self.metadata.chunk_grid.chunk_shape,
        )

        # setup output array
        out = np.zeros(
            indexer.shape,
            dtype=self.metadata.dtype,
            order=self.runtime_configuration.order,
        )

        # reading chunks and decoding them
        await self.codecs.read_batched(
            [
                (
                    self.store_path
                    / self.metadata.chunk_key_encoding.encode_chunk_key(chunk_coords),
                    self.metadata.get_chunk_spec(chunk_coords),
                    chunk_selection,
                    out_selection,
                )
                for chunk_coords, chunk_selection, out_selection in indexer
            ],
            out,
            self.runtime_configuration,
        )

        if out.shape:
            return out
        else:
            return out[()]

    async def _save_metadata(self) -> None:
        await (self.store_path / ZARR_JSON).set(self.metadata.to_bytes())

    async def setitem(self, selection: Selection, value: np.ndarray) -> None:
        assert isinstance(self.metadata.chunk_grid, RegularChunkGrid)
        chunk_shape = self.metadata.chunk_grid.chunk_shape
        indexer = BasicIndexer(
            selection,
            shape=self.metadata.shape,
            chunk_shape=chunk_shape,
        )

        sel_shape = indexer.shape

        # check value shape
        if np.isscalar(value):
            # setting a scalar value
            pass
        else:
            if not hasattr(value, "shape"):
                value = np.asarray(value, self.metadata.dtype)
            assert value.shape == sel_shape
            if value.dtype.name != self.metadata.dtype.name:
                value = value.astype(self.metadata.dtype, order="A")

        # merging with existing data and encoding chunks
        await self.codecs.write_batched(
            [
                (
                    self.store_path
                    / self.metadata.chunk_key_encoding.encode_chunk_key(chunk_coords),
                    self.metadata.get_chunk_spec(chunk_coords),
                    chunk_selection,
                    out_selection,
                )
                for chunk_coords, chunk_selection, out_selection in indexer
            ],
            value,
            self.runtime_configuration,
        )

    async def resize(
        self, new_shape: ChunkCoords, delete_outside_chunks: bool = True
    ) -> AsyncArray:
        assert len(new_shape) == len(self.metadata.shape)
        new_metadata = replace(self.metadata, shape=new_shape)

        # Remove all chunks outside of the new shape
        assert isinstance(self.metadata.chunk_grid, RegularChunkGrid)
        chunk_shape = self.metadata.chunk_grid.chunk_shape
        chunk_key_encoding = self.metadata.chunk_key_encoding
        old_chunk_coords = set(all_chunk_coords(self.metadata.shape, chunk_shape))
        new_chunk_coords = set(all_chunk_coords(new_shape, chunk_shape))

        if delete_outside_chunks:

            async def _delete_key(key: str) -> None:
                await (self.store_path / key).delete()

            await concurrent_map(
                [
                    (chunk_key_encoding.encode_chunk_key(chunk_coords),)
                    for chunk_coords in old_chunk_coords.difference(new_chunk_coords)
                ],
                _delete_key,
                self.runtime_configuration.concurrency,
            )

        # Write new metadata
        await (self.store_path / ZARR_JSON).set(new_metadata.to_bytes())
        return replace(self, metadata=new_metadata)

    async def update_attributes(self, new_attributes: Dict[str, Any]) -> AsyncArray:
        new_metadata = replace(self.metadata, attributes=new_attributes)

        # Write new metadata
        await (self.store_path / ZARR_JSON).set(new_metadata.to_bytes())
        return replace(self, metadata=new_metadata)

    def __repr__(self):
        return f"<AsyncArray {self.store_path} shape={self.shape} dtype={self.dtype}>"

    async def info(self):
        return NotImplemented


@dataclass(frozen=True)
class Array:
    _async_array: AsyncArray

    @classmethod
    def create(
        cls,
        store: StoreLike,
        *,
        shape: ChunkCoords,
        dtype: Union[str, np.dtype],
        chunk_shape: ChunkCoords,
        fill_value: Optional[Any] = None,
        chunk_key_encoding: Union[
            Tuple[Literal["default"], Literal[".", "/"]],
            Tuple[Literal["v2"], Literal[".", "/"]],
        ] = ("default", "/"),
        codecs: Optional[Iterable[Union[Codec, Dict[str, Any]]]] = None,
        dimension_names: Optional[Iterable[str]] = None,
        attributes: Optional[Dict[str, Any]] = None,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
        exists_ok: bool = False,
    ) -> Array:
        async_array = sync(
            AsyncArray.create(
                store=store,
                shape=shape,
                dtype=dtype,
                chunk_shape=chunk_shape,
                fill_value=fill_value,
                chunk_key_encoding=chunk_key_encoding,
                codecs=codecs,
                dimension_names=dimension_names,
                attributes=attributes,
                runtime_configuration=runtime_configuration,
                exists_ok=exists_ok,
            ),
            runtime_configuration.asyncio_loop,
        )
        return cls(async_array)

    @classmethod
    def from_dict(
        cls,
        store_path: StorePath,
        data: Dict[str, Any],
        runtime_configuration: RuntimeConfiguration,
    ) -> Array:
        async_array = AsyncArray.from_dict(
            store_path=store_path, data=data, runtime_configuration=runtime_configuration
        )
        return cls(async_array)

    @classmethod
    def open(
        cls,
        store: StoreLike,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
    ) -> Array:
        async_array = sync(
            AsyncArray.open(store, runtime_configuration=runtime_configuration),
            runtime_configuration.asyncio_loop,
        )
        return cls(async_array)

    @classmethod
    def open_auto(
        cls,
        store: StoreLike,
        runtime_configuration: RuntimeConfiguration = RuntimeConfiguration(),
    ) -> Array:  # TODO: Union[Array, ArrayV2]:
        async_array = sync(
            AsyncArray.open_auto(store, runtime_configuration),
            runtime_configuration.asyncio_loop,
        )
        return cls(async_array)

    @property
    def ndim(self) -> int:
        return self._async_array.ndim

    @property
    def shape(self) -> ChunkCoords:
        return self._async_array.shape

    @property
    def size(self) -> int:
        return self._async_array.size

    @property
    def dtype(self) -> np.dtype:
        return self._async_array.dtype

    @property
    def attrs(self) -> dict:
        return self._async_array.attrs

    @property
    def metadata(self) -> ArrayMetadata:
        return self._async_array.metadata

    @property
    def store_path(self) -> StorePath:
        return self._async_array.store_path

    def __getitem__(self, selection: Selection):
        return sync(
            self._async_array.getitem(selection),
            self._async_array.runtime_configuration.asyncio_loop,
        )

    def __setitem__(self, selection: Selection, value: np.ndarray) -> None:
        sync(
            self._async_array.setitem(selection, value),
            self._async_array.runtime_configuration.asyncio_loop,
        )

    def resize(self, new_shape: ChunkCoords) -> Array:
        return sync(
            self._async_array.resize(new_shape),
            self._async_array.runtime_configuration.asyncio_loop,
        )

    def update_attributes(self, new_attributes: Dict[str, Any]) -> Array:
        return sync(
            self._async_array.update_attributes(new_attributes),
            self._async_array.runtime_configuration.asyncio_loop,
        )

    def __repr__(self):
        return f"<Array {self.store_path} shape={self.shape} dtype={self.dtype}>"

    def info(self):
        return sync(
            self._async_array.info(),
            self._async_array.runtime_configuration.asyncio_loop,
        )