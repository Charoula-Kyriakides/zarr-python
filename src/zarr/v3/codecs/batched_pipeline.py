from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, TypeVar
import numpy as np
from dataclasses import dataclass

from zarr.v3.abc.codec import (
    Codec,
    ArrayArrayCodec,
    ArrayBytesCodec,
    ArrayBytesCodecPartialDecodeMixin,
    ArrayBytesCodecPartialEncodeMixin,
    BytesBytesCodec,
)
from zarr.v3.codecs.pipeline import CodecPipeline
from zarr.v3.common import concurrent_map
from zarr.v3.indexing import is_total_slice

if TYPE_CHECKING:
    from typing import List, Optional, Tuple
    from zarr.v3.store import StorePath
    from zarr.v3.metadata import RuntimeConfiguration
    from zarr.v3.common import ArraySpec, BytesLike, SliceSelection

T = TypeVar("T")
U = TypeVar("U")


def unzip2(iterable: Iterable[Tuple[T, U]]) -> Tuple[List[T], List[U]]:
    out0: List[T] = []
    out1: List[U] = []
    for item0, item1 in iterable:
        out0.append(item0)
        out1.append(item1)
    return (out0, out1)


def resolve_batched(codec: Codec, chunk_specs: Iterable[ArraySpec]) -> Iterable[ArraySpec]:
    return [codec.resolve_metadata(chunk_spec) for chunk_spec in chunk_specs]


@dataclass(frozen=True)
class BatchedCodecPipeline(CodecPipeline):
    def _codecs_with_resolved_metadata_batched(
        self, chunk_specs: Iterable[ArraySpec]
    ) -> Tuple[
        List[Tuple[ArrayArrayCodec, List[ArraySpec]]],
        Tuple[ArrayBytesCodec, List[ArraySpec]],
        List[Tuple[BytesBytesCodec, List[ArraySpec]]],
    ]:
        aa_codecs_with_spec: List[Tuple[ArrayArrayCodec, List[ArraySpec]]] = []
        for aa_codec in self.array_array_codecs:
            aa_codecs_with_spec.append((aa_codec, chunk_specs))
            chunk_specs = [aa_codec.resolve_metadata(chunk_spec) for chunk_spec in chunk_specs]

        ab_codec_with_spec = (self.array_bytes_codec, chunk_specs)
        chunk_specs = [
            self.array_bytes_codec.resolve_metadata(chunk_spec) for chunk_spec in chunk_specs
        ]

        bb_codecs_with_spec: List[Tuple[BytesBytesCodec, List[ArraySpec]]] = []
        for bb_codec in self.bytes_bytes_codecs:
            bb_codecs_with_spec.append((bb_codec, chunk_specs))
            chunk_specs = [bb_codec.resolve_metadata(chunk_spec) for chunk_spec in chunk_specs]

        return (aa_codecs_with_spec, ab_codec_with_spec, bb_codecs_with_spec)

    async def read_batched(
        self,
        batch_info: Iterable[Tuple[StorePath, ArraySpec, SliceSelection, SliceSelection]],
        out: np.ndarray,
        runtime_configuration: RuntimeConfiguration,
    ) -> None:
        if self.supports_partial_decode:
            chunk_array_batch = await self.decode_partial_batched(
                [
                    (store_path, chunk_selection, chunk_spec)
                    for store_path, chunk_spec, chunk_selection, _ in batch_info
                ],
                runtime_configuration,
            )
            for chunk_array, (_, chunk_spec, _, out_selection) in zip(
                chunk_array_batch, batch_info
            ):
                if chunk_array is not None:
                    out[out_selection] = chunk_array
                else:
                    out[out_selection] = chunk_spec.fill_value
        else:
            chunk_bytes_batch = await concurrent_map(
                [(store_path,) for store_path, _, _, _ in batch_info],
                lambda store_path: store_path.get(),
                runtime_configuration.concurrency,
            )
            chunk_array_batch = await self.decode_batched(
                [
                    (chunk_bytes, chunk_spec)
                    for chunk_bytes, (_, chunk_spec, _, _) in zip(chunk_bytes_batch, batch_info)
                ],
                runtime_configuration,
            )
            for chunk_array, (_, chunk_spec, chunk_selection, out_selection) in zip(
                chunk_array_batch, batch_info
            ):
                if chunk_array is not None:
                    tmp = chunk_array[chunk_selection]
                    out[out_selection] = tmp
                else:
                    out[out_selection] = chunk_spec.fill_value

    async def decode_batched(
        self,
        chunk_bytes_and_specs: Iterable[Tuple[BytesLike, ArraySpec]],
        runtime_configuration: RuntimeConfiguration,
    ) -> Iterable[Optional[np.ndarray]]:
        chunk_bytes_batch, chunk_specs = unzip2(chunk_bytes_and_specs)

        (
            aa_codecs_with_spec,
            ab_codec_with_spec,
            bb_codecs_with_spec,
        ) = self._codecs_with_resolved_metadata_batched(chunk_specs)

        for bb_codec, chunk_spec_batch in bb_codecs_with_spec[::-1]:
            chunk_bytes_batch = await bb_codec.decode_batch(
                zip(chunk_bytes_batch, chunk_spec_batch), runtime_configuration
            )

        ab_codec, chunk_spec_batch = ab_codec_with_spec
        chunk_array_batch = await ab_codec.decode_batch(
            zip(chunk_bytes_batch, chunk_spec_batch), runtime_configuration
        )

        for aa_codec, chunk_spec_batch in aa_codecs_with_spec[::-1]:
            chunk_array_batch = await aa_codec.decode_batch(
                zip(chunk_array_batch, chunk_spec_batch), runtime_configuration
            )

        return chunk_array_batch

    async def decode_partial_batched(
        self,
        batch_info: Iterable[Tuple[StorePath, SliceSelection, ArraySpec]],
        runtime_configuration: RuntimeConfiguration,
    ) -> Iterable[Optional[np.ndarray]]:
        assert self.supports_partial_decode
        assert isinstance(self.array_bytes_codec, ArrayBytesCodecPartialDecodeMixin)
        return await self.array_bytes_codec.decode_partial_batched(
            batch_info, runtime_configuration
        )

    async def encode_batched(
        self,
        chunk_arrays_and_specs: Iterable[Tuple[Optional[np.ndarray], ArraySpec]],
        runtime_configuration: RuntimeConfiguration,
    ) -> Iterable[Optional[BytesLike]]:
        chunk_array_batch, chunk_specs = unzip2(chunk_arrays_and_specs)

        for aa_codec in self.array_array_codecs:
            chunk_array_batch = await aa_codec.encode_batch(
                zip(chunk_array_batch, chunk_specs), runtime_configuration
            )
            chunk_specs = resolve_batched(aa_codec, chunk_specs)

        chunk_bytes_batch = await self.array_bytes_codec.encode_batch(
            zip(chunk_array_batch, chunk_specs), runtime_configuration
        )
        chunk_specs = resolve_batched(self.array_bytes_codec, chunk_specs)

        for bb_codec in self.bytes_bytes_codecs:
            chunk_bytes_batch = await bb_codec.encode_batch(
                zip(chunk_bytes_batch, chunk_specs), runtime_configuration
            )
            chunk_specs = resolve_batched(bb_codec, chunk_specs)

        return chunk_bytes_batch

    async def encode_partial_batched(
        self,
        batch_info: Iterable[Tuple[StorePath, np.ndarray, SliceSelection, ArraySpec]],
        runtime_configuration: RuntimeConfiguration,
    ) -> None:
        assert self.supports_partial_encode
        assert isinstance(self.array_bytes_codec, ArrayBytesCodecPartialEncodeMixin)
        await self.array_bytes_codec.encode_partial_batched(batch_info, runtime_configuration)

    def compute_encoded_size(self, byte_length: int, array_spec: ArraySpec) -> int:
        for codec in self:
            byte_length = codec.compute_encoded_size(byte_length, array_spec)
            array_spec = codec.resolve_metadata(array_spec)
        return byte_length

    async def write_batched(
        self,
        batch_info: Iterable[Tuple[StorePath, ArraySpec, SliceSelection, SliceSelection]],
        value: np.ndarray,
        runtime_configuration: RuntimeConfiguration,
    ) -> None:
        if self.supports_partial_encode:
            await self.encode_partial_batched(
                [
                    (store_path, value[out_selection], chunk_selection, chunk_spec)
                    for store_path, chunk_spec, chunk_selection, out_selection in batch_info
                ],
                runtime_configuration,
            )

        else:
            # Read existing bytes if not total slice
            async def _read_key(store_path: Optional[StorePath]) -> Optional[BytesLike]:
                if store_path is None:
                    return None
                return await store_path.get()

            chunk_bytes_batch = await concurrent_map(
                [
                    (None if is_total_slice(chunk_selection, chunk_spec.shape) else store_path,)
                    for store_path, chunk_spec, chunk_selection, _ in batch_info
                ],
                _read_key,
                runtime_configuration.concurrency,
            )
            chunk_array_batch = await self.decode_batched(
                [
                    (chunk_bytes, chunk_spec)
                    for chunk_bytes, (_, chunk_spec, _, _) in zip(chunk_bytes_batch, batch_info)
                ],
                runtime_configuration,
            )

            def _merge_chunk_array(
                existing_chunk_array: Optional[np.ndarray],
                new_chunk_array_slice: np.ndarray,
                chunk_spec: ArraySpec,
                chunk_selection: SliceSelection,
            ) -> np.ndarray:
                if is_total_slice(chunk_selection, chunk_spec.shape):
                    return new_chunk_array_slice
                if existing_chunk_array is None:
                    chunk_array = np.empty(
                        chunk_spec.shape,
                        dtype=chunk_spec.dtype,
                    )
                    chunk_array.fill(chunk_spec.fill_value)
                else:
                    chunk_array = existing_chunk_array.copy()  # make a writable copy
                chunk_array[chunk_selection] = new_chunk_array_slice
                return chunk_array

            chunk_array_batch = [
                _merge_chunk_array(chunk_array, value[out_selection], chunk_spec, chunk_selection)
                for chunk_array, (_, chunk_spec, chunk_selection, out_selection) in zip(
                    chunk_array_batch, batch_info
                )
            ]

            chunk_array_batch = [
                None if np.all(chunk_array == chunk_spec.fill_value) else chunk_array
                for chunk_array, (_, chunk_spec, _, _) in zip(chunk_array_batch, batch_info)
            ]

            chunk_bytes_batch = await self.encode_batched(
                [
                    (chunk_array, chunk_spec)
                    for chunk_array, (_, chunk_spec, _, _) in zip(chunk_array_batch, batch_info)
                ],
                runtime_configuration,
            )

            async def _write_key(store_path: StorePath, chunk_bytes: Optional[BytesLike]) -> None:
                if chunk_bytes is None:
                    await store_path.delete()
                else:
                    await store_path.set(chunk_bytes)

            await concurrent_map(
                [
                    (store_path, chunk_bytes)
                    for chunk_bytes, (store_path, _, _, _) in zip(chunk_bytes_batch, batch_info)
                ],
                _write_key,
                runtime_configuration.concurrency,
            )
