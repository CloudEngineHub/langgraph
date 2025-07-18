from __future__ import annotations

import logging
import os
import pickle
import random
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, ExitStack
from types import TracebackType
from typing import Any

from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

logger = logging.getLogger(__name__)


class InMemorySaver(
    BaseCheckpointSaver[str], AbstractContextManager, AbstractAsyncContextManager
):
    """An in-memory checkpoint saver.

    This checkpoint saver stores checkpoints in memory using a defaultdict.

    Note:
        Only use `InMemorySaver` for debugging or testing purposes.
        For production use cases we recommend installing [langgraph-checkpoint-postgres](https://pypi.org/project/langgraph-checkpoint-postgres/) and using `PostgresSaver` / `AsyncPostgresSaver`.

        If you are using the LangGraph Platform, no checkpointer needs to be specified. The correct managed checkpointer will be used automatically.

    Args:
        serde: The serializer to use for serializing and deserializing checkpoints. Defaults to None.

    Examples:

            import asyncio

            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.graph import StateGraph

            builder = StateGraph(int)
            builder.add_node("add_one", lambda x: x + 1)
            builder.set_entry_point("add_one")
            builder.set_finish_point("add_one")

            memory = InMemorySaver()
            graph = builder.compile(checkpointer=memory)
            coro = graph.ainvoke(1, {"configurable": {"thread_id": "thread-1"}})
            asyncio.run(coro)  # Output: 2
    """

    # thread ID ->  checkpoint NS -> checkpoint ID -> checkpoint mapping
    storage: defaultdict[
        str,
        dict[str, dict[str, tuple[tuple[str, bytes], tuple[str, bytes], str | None]]],
    ]
    # (thread ID, checkpoint NS, checkpoint ID) -> (task ID, write idx)
    writes: defaultdict[
        tuple[str, str, str],
        dict[tuple[str, int], tuple[str, str, tuple[str, bytes], str]],
    ]
    blobs: dict[
        tuple[
            str, str, str, str | int | float
        ],  # thread id, checkpoint ns, channel, version
        tuple[str, bytes],
    ]

    def __init__(
        self,
        *,
        serde: SerializerProtocol | None = None,
        factory: type[defaultdict] = defaultdict,
    ) -> None:
        super().__init__(serde=serde)
        self.storage = factory(lambda: defaultdict(dict))
        self.writes = factory(dict)
        self.blobs = factory()
        self.stack = ExitStack()
        if factory is not defaultdict:
            self.stack.enter_context(self.storage)  # type: ignore[arg-type]
            self.stack.enter_context(self.writes)  # type: ignore[arg-type]
            self.stack.enter_context(self.blobs)  # type: ignore[arg-type]

    def __enter__(self) -> InMemorySaver:
        return self.stack.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self.stack.__exit__(exc_type, exc_value, traceback)

    async def __aenter__(self) -> InMemorySaver:
        return self.stack.__enter__()

    async def __aexit__(
        self,
        __exc_type: type[BaseException] | None,
        __exc_value: BaseException | None,
        __traceback: TracebackType | None,
    ) -> bool | None:
        return self.stack.__exit__(__exc_type, __exc_value, __traceback)

    def _load_blobs(
        self, thread_id: str, checkpoint_ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        channel_values: dict[str, Any] = {}
        for k, v in versions.items():
            kk = (thread_id, checkpoint_ns, k, v)
            if kk in self.blobs:
                vv = self.blobs[kk]
                if vv[0] != "empty":
                    channel_values[k] = self.serde.loads_typed(vv)
        return channel_values

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Get a checkpoint tuple from the in-memory storage.

        This method retrieves a checkpoint tuple from the in-memory storage based on the
        provided config. If the config contains a "checkpoint_id" key, the checkpoint with
        the matching thread ID and timestamp is retrieved. Otherwise, the latest checkpoint
        for the given thread ID is retrieved.

        Args:
            config: The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.
        """
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        if checkpoint_id := get_checkpoint_id(config):
            if saved := self.storage[thread_id][checkpoint_ns].get(checkpoint_id):
                checkpoint, metadata, parent_checkpoint_id = saved
                writes = self.writes[(thread_id, checkpoint_ns, checkpoint_id)].values()
                checkpoint_: Checkpoint = self.serde.loads_typed(checkpoint)
                return CheckpointTuple(
                    config=config,
                    checkpoint={
                        **checkpoint_,
                        "channel_values": self._load_blobs(
                            thread_id, checkpoint_ns, checkpoint_["channel_versions"]
                        ),
                    },
                    metadata=self.serde.loads_typed(metadata),
                    pending_writes=[
                        (id, c, self.serde.loads_typed(v)) for id, c, v, _ in writes
                    ],
                    parent_config=(
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": parent_checkpoint_id,
                            }
                        }
                        if parent_checkpoint_id
                        else None
                    ),
                )
        else:
            if checkpoints := self.storage[thread_id][checkpoint_ns]:
                checkpoint_id = max(checkpoints.keys())
                checkpoint, metadata, parent_checkpoint_id = checkpoints[checkpoint_id]
                writes = self.writes[(thread_id, checkpoint_ns, checkpoint_id)].values()
                checkpoint_ = self.serde.loads_typed(checkpoint)
                return CheckpointTuple(
                    config={
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": checkpoint_id,
                        }
                    },
                    checkpoint={
                        **checkpoint_,
                        "channel_values": self._load_blobs(
                            thread_id, checkpoint_ns, checkpoint_["channel_versions"]
                        ),
                    },
                    metadata=self.serde.loads_typed(metadata),
                    pending_writes=[
                        (id, c, self.serde.loads_typed(v)) for id, c, v, _ in writes
                    ],
                    parent_config=(
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": parent_checkpoint_id,
                            }
                        }
                        if parent_checkpoint_id
                        else None
                    ),
                )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from the in-memory storage.

        This method retrieves a list of checkpoint tuples from the in-memory storage based
        on the provided criteria.

        Args:
            config: Base configuration for filtering checkpoints.
            filter: Additional filtering criteria for metadata.
            before: List checkpoints created before this configuration.
            limit: Maximum number of checkpoints to return.

        Yields:
            Iterator[CheckpointTuple]: An iterator of matching checkpoint tuples.
        """
        thread_ids = (config["configurable"]["thread_id"],) if config else self.storage
        config_checkpoint_ns = (
            config["configurable"].get("checkpoint_ns") if config else None
        )
        config_checkpoint_id = get_checkpoint_id(config) if config else None
        for thread_id in thread_ids:
            for checkpoint_ns in self.storage[thread_id].keys():
                if (
                    config_checkpoint_ns is not None
                    and checkpoint_ns != config_checkpoint_ns
                ):
                    continue

                for checkpoint_id, (
                    checkpoint,
                    metadata_b,
                    parent_checkpoint_id,
                ) in sorted(
                    self.storage[thread_id][checkpoint_ns].items(),
                    key=lambda x: x[0],
                    reverse=True,
                ):
                    # filter by checkpoint ID from config
                    if config_checkpoint_id and checkpoint_id != config_checkpoint_id:
                        continue

                    # filter by checkpoint ID from `before` config
                    if (
                        before
                        and (before_checkpoint_id := get_checkpoint_id(before))
                        and checkpoint_id >= before_checkpoint_id
                    ):
                        continue

                    # filter by metadata
                    metadata = self.serde.loads_typed(metadata_b)
                    if filter and not all(
                        query_value == metadata.get(query_key)
                        for query_key, query_value in filter.items()
                    ):
                        continue

                    # limit search results
                    if limit is not None and limit <= 0:
                        break
                    elif limit is not None:
                        limit -= 1

                    writes = self.writes[
                        (thread_id, checkpoint_ns, checkpoint_id)
                    ].values()

                    checkpoint_: Checkpoint = self.serde.loads_typed(checkpoint)

                    yield CheckpointTuple(
                        config={
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": checkpoint_id,
                            }
                        },
                        checkpoint={
                            **checkpoint_,
                            "channel_values": self._load_blobs(
                                thread_id,
                                checkpoint_ns,
                                checkpoint_["channel_versions"],
                            ),
                        },
                        metadata=metadata,
                        parent_config=(
                            {
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": checkpoint_ns,
                                    "checkpoint_id": parent_checkpoint_id,
                                }
                            }
                            if parent_checkpoint_id
                            else None
                        ),
                        pending_writes=[
                            (id, c, self.serde.loads_typed(v)) for id, c, v, _ in writes
                        ],
                    )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Save a checkpoint to the in-memory storage.

        This method saves a checkpoint to the in-memory storage. The checkpoint is associated
        with the provided config.

        Args:
            config: The config to associate with the checkpoint.
            checkpoint: The checkpoint to save.
            metadata: Additional metadata to save with the checkpoint.
            new_versions: New versions as of this write

        Returns:
            RunnableConfig: The updated config containing the saved checkpoint's timestamp.
        """
        c = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        for k, v in new_versions.items():
            self.blobs[(thread_id, checkpoint_ns, k, v)] = (
                self.serde.dumps_typed(values[k]) if k in values else ("empty", b"")
            )
        self.storage[thread_id][checkpoint_ns].update(
            {
                checkpoint["id"]: (
                    self.serde.dumps_typed(c),
                    self.serde.dumps_typed(get_checkpoint_metadata(config, metadata)),
                    config["configurable"].get("checkpoint_id"),  # parent
                )
            }
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Save a list of writes to the in-memory storage.

        This method saves a list of writes to the in-memory storage. The writes are associated
        with the provided config.

        Args:
            config: The config to associate with the writes.
            writes: The writes to save.
            task_id: Identifier for the task creating the writes.
            task_path: Path of the task creating the writes.

        Returns:
            RunnableConfig: The updated config containing the saved writes' timestamp.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        outer_key = (thread_id, checkpoint_ns, checkpoint_id)
        outer_writes_ = self.writes.get(outer_key)
        for idx, (c, v) in enumerate(writes):
            inner_key = (task_id, WRITES_IDX_MAP.get(c, idx))
            if inner_key[1] >= 0 and outer_writes_ and inner_key in outer_writes_:
                continue

            self.writes[outer_key][inner_key] = (
                task_id,
                c,
                self.serde.dumps_typed(v),
                task_path,
            )

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints and writes associated with a thread ID.

        Args:
            thread_id: The thread ID to delete.

        Returns:
            None
        """
        if thread_id in self.storage:
            del self.storage[thread_id]
        for k in list(self.writes.keys()):
            if k[0] == thread_id:
                del self.writes[k]
        for k in list(self.blobs.keys()):
            if k[0] == thread_id:
                del self.blobs[k]

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronous version of get_tuple.

        This method is an asynchronous wrapper around get_tuple that runs the synchronous
        method in a separate thread using asyncio.

        Args:
            config: The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.
        """
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Asynchronous version of list.

        This method is an asynchronous wrapper around list that runs the synchronous
        method in a separate thread using asyncio.

        Args:
            config: The config to use for listing the checkpoints.

        Yields:
            AsyncIterator[CheckpointTuple]: An asynchronous iterator of checkpoint tuples.
        """
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronous version of put.

        Args:
            config: The config to associate with the checkpoint.
            checkpoint: The checkpoint to save.
            metadata: Additional metadata to save with the checkpoint.
            new_versions: New versions as of this write

        Returns:
            RunnableConfig: The updated config containing the saved checkpoint's timestamp.
        """
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronous version of put_writes.

        This method is an asynchronous wrapper around put_writes that runs the synchronous
        method in a separate thread using asyncio.

        Args:
            config: The config to associate with the writes.
            writes: The writes to save, each as a (channel, value) pair.
            task_id: Identifier for the task creating the writes.
            task_path: Path of the task creating the writes.

        Returns:
            None
        """
        return self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints and writes associated with a thread ID.

        Args:
            thread_id: The thread ID to delete.

        Returns:
            None
        """
        return self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"


MemorySaver = InMemorySaver  # Kept for backwards compatibility


class PersistentDict(defaultdict):
    """Persistent dictionary with an API compatible with shelve and anydbm.

    The dict is kept in memory, so the dictionary operations run as fast as
    a regular dictionary.

    Write to disk is delayed until close or sync (similar to gdbm's fast mode).

    Input file format is automatically discovered.
    Output file format is selectable between pickle, json, and csv.
    All three serialization formats are backed by fast C implementations.

    Adapted from https://code.activestate.com/recipes/576642-persistent-dict-with-multiple-standard-file-format/

    """

    def __init__(self, *args: Any, filename: str, **kwds: Any) -> None:
        self.flag = "c"  # r=readonly, c=create, or n=new
        self.mode = None  # None or an octal triple like 0644
        self.format = "pickle"  # 'csv', 'json', or 'pickle'
        self.filename = filename
        super().__init__(*args, **kwds)

    def sync(self) -> None:
        "Write dict to disk"
        if self.flag == "r":
            return
        tempname = self.filename + ".tmp"
        fileobj = open(tempname, "wb" if self.format == "pickle" else "w")
        try:
            self.dump(fileobj)
        except Exception:
            os.remove(tempname)
            raise
        finally:
            fileobj.close()
        shutil.move(tempname, self.filename)  # atomic commit
        if self.mode is not None:
            os.chmod(self.filename, self.mode)

    def close(self) -> None:
        self.sync()
        self.clear()

    def __enter__(self) -> PersistentDict:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def dump(self, fileobj: Any) -> None:
        if self.format == "pickle":
            pickle.dump(dict(self), fileobj, 2)
        else:
            raise NotImplementedError("Unknown format: " + repr(self.format))

    def load(self) -> None:
        # try formats from most restrictive to least restrictive
        if self.flag == "n":
            return
        with open(self.filename, "rb" if self.format == "pickle" else "r") as fileobj:
            for loader in (pickle.load,):
                fileobj.seek(0)
                try:
                    return self.update(loader(fileobj))
                except EOFError:
                    return
                except Exception:
                    logger.error(f"Failed to load file: {fileobj.name}")
                    raise
            raise ValueError("File not in a supported format")
