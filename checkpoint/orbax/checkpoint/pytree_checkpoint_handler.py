# Copyright 2023 The Orbax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PyTreeCheckpointHandler class.

Implementation of CheckpointHandler interface.
"""

import asyncio
import dataclasses
import functools
import re
from typing import Any, Callable, List, Optional, Tuple

from etils import epath
import jax
from jax.experimental.gda_serialization import serialization
import numpy as np
from orbax.checkpoint import aggregate_handlers
from orbax.checkpoint import lazy_utils
from orbax.checkpoint import transform_utils
from orbax.checkpoint import type_handlers
from orbax.checkpoint import utils
from orbax.checkpoint.async_checkpoint_handler import AsyncCheckpointHandler
from orbax.checkpoint.future import Future
import tensorstore as ts


PyTree = Any
RestoreArgs = type_handlers.RestoreArgs
ArrayRestoreArgs = type_handlers.ArrayRestoreArgs
SaveArgs = type_handlers.SaveArgs
ParamInfo = type_handlers.ParamInfo
TypeHandler = type_handlers.TypeHandler
AggregateHandler = aggregate_handlers.AggregateHandler
MsgpackHandler = aggregate_handlers.MsgpackHandler
TransformFn = Callable[[PyTree, PyTree, PyTree], Tuple[PyTree, PyTree]]

_TYPE_METADATA_FILE = 'type_metadata'
_CHECKPOINT_FILE = 'checkpoint'


async def _create_param_save_dir(param_info: ParamInfo, args: SaveArgs):
  # Directory will be unused.
  path = param_info.path
  if path is None or args.aggregate:
    return
  if jax.process_index() == 0:
    await utils.async_makedirs(path)


def _maybe_set_default_save_args(value, args):
  # If already set, return.
  if isinstance(args, SaveArgs):
    return args
  aggregate = not type_handlers.has_type_handler(type(value))
  return SaveArgs(aggregate=aggregate)


def _maybe_set_default_restore_args(args):
  if isinstance(args, RestoreArgs):
    return args
  return RestoreArgs(restore_type=None)


def _try_array_cast(arr, dtype):
  if dtype is not None:
    if utils.is_scalar(arr):
      arr = np.asarray(arr).astype(dtype).item()
    else:
      if hasattr(arr, 'astype'):
        arr = arr.astype(dtype)
  return arr


def _get_param_names(item: PyTree) -> PyTree:
  """Gets parameter names for PyTree elements."""
  def _param_name_from_keypath(keypath: Tuple[Any, ...]) -> str:
    return '.'.join([str(utils.get_key_name(k)) for k in keypath])

  return jax.tree_util.tree_map_with_path(
      lambda kp, _: _param_name_from_keypath(kp),
      item,
      is_leaf=utils.is_empty_or_leaf,
  )


def _get_param_infos_from_structure(directory: epath.Path,
                                    structure: PyTree) -> PyTree:
  """Construct ParamInfos based on a PyTree."""
  names = _get_param_names(structure)
  is_ocdbt_checkpoint = type_handlers.is_ocdbt_checkpoint(directory)

  def _get_param_info(name, leaf):
    if utils.leaf_is_placeholder(leaf):
      # Leaf is a param name.
      path = directory / utils.name_from_leaf_placeholder(leaf)
    # The following is kept for backwards compatibility.
    elif isinstance(leaf, ts.Spec):
      tspec = leaf.to_json()  # pytype: disable=attribute-error
      # Skip '.', since we need special regex handling for this char.
      pattern = r'\.' + utils.TMP_DIR_SUFFIX[1:] + r'\d+'
      path = re.sub(pattern, '', tspec['kvstore']['path'])
    elif utils.is_supported_aggregation_type(leaf):
      # Value already restored, do not need ts.Spec.
      path = None
    else:
      raise ValueError(f'Unsupported type: {type(leaf)}')
    return ParamInfo(
        name=name,
        path=path,
        aggregate=(path is None),
        is_ocdbt_checkpoint=is_ocdbt_checkpoint,
    )

  return jax.tree_util.tree_map(
      _get_param_info, names, structure, is_leaf=utils.is_empty_or_leaf
  )


def _get_tree_for_aggregation(param_infos, save_args, item):
  """Get tree for aggregated checkpoint."""

  def _get_leaf_for_aggregation(param_info, arg, value):
    if arg.aggregate:  # Param was aggregated, return value after cast.
      if not utils.is_supported_aggregation_type(value):
        value = None
      return _try_array_cast(value, arg.dtype)
    else:  # Placeholder string for non-aggregated value.
      return utils.leaf_placeholder(param_info.name)

  return jax.tree_util.tree_map(
      _get_leaf_for_aggregation, param_infos, save_args, item
  )


def _transform_structure(
    item: PyTree,
    restored: PyTree,
    param_infos: PyTree,
    transforms: PyTree,
    transforms_default_to_original: bool,
) -> Tuple[PyTree, PyTree]:
  """Transforms `restored` and `param_infos` into the structure of `item`.

  After restoring a checkpoint structure (represented by `restored`), we must
  transform it to match the structure of `item` and fill in any missing values.

  Note that `param_infos` must also be transformed since parameter names and
  other information is computed based on the PyTree structure. We first create
  `param_infos` based on the checkpoint structure, otherwise we would not find
  some parameters after doing transformations and rearranging the tree
  structure.

  Args:
    item: a PyTree representing the result structure ("new tree structure").
    restored: a PyTree representing the original tree structure.
    param_infos: A PyTree of ParamInfo having the same structure as `restored`.
    transforms: provides instructions on how to transform the input trees. See
      transform_utils.
    transforms_default_to_original: See transform_utils.

  Returns:
    A tuple of `item`, `param_infos`, which have
    been transformed from the original trees using `transforms`.
  """
  if item is None:
    if transforms is not None:
      msg = ('If providing `transforms`, must provide `item` matching structure'
             ' of expected result.')
      raise ValueError(msg)
    item = restored
  else:
    if transforms is None:
      param_infos = utils.deserialize_tree(item, param_infos)
      item = utils.deserialize_tree(item, restored)
    else:
      if transform_utils.has_multi_value_functions(transforms):
        raise ValueError('Found disallowed `multi_value_fn` in `transforms`.')
      item = transform_utils.apply_transformations(
          restored, transforms, item, transforms_default_to_original)
      # param_infos must be transformed because the ts.Spec of saved params
      # may correspond to the original structure rather than the new.
      param_infos = transform_utils.apply_transformations(
          param_infos, transforms, item, transforms_default_to_original)

      def _create_param_info_if_already_restored(x):
        return ParamInfo(aggregate=True) if not isinstance(x, ParamInfo) else x

      param_infos = jax.tree_util.tree_map(
          _create_param_info_if_already_restored, param_infos)
  return item, param_infos


def _apply_value_transforms(item: PyTree, value_transforms: PyTree) -> PyTree:
  if value_transforms is None:
    return item
  # Structure is not being altered, only values.
  return transform_utils.apply_transformations(item, value_transforms, item)


class PyTreeCheckpointHandler(AsyncCheckpointHandler):
  """A CheckpointHandler implementation for any PyTree structure.

  The PyTree is assumed to be a nested dictionary with array values represented
  as array-like objects (see type_handlers for supported objects). If not
  `jax.Array`, arrays are expected to be fully replicated.
  """

  def __init__(
      self,
      aggregate_filename: Optional[str] = None,
      concurrent_gb: int = 96,
      use_ocdbt: bool = False,
  ):
    """Creates PyTreeCheckpointHandler.

    Args:
      aggregate_filename: name that the aggregated checkpoint should be saved
        as.
      concurrent_gb: max concurrent GB that are allowed to be read.
      use_ocdbt: enables Tensorstore OCDBT driver.
    """
    self._aggregate_handler = aggregate_handlers.get_aggregate_handler()
    if aggregate_filename is None:
      aggregate_filename = _CHECKPOINT_FILE
    self._aggregate_filename = aggregate_filename
    self._concurrent_gb = concurrent_gb
    self._use_ocdbt = use_ocdbt

  def _get_param_names(self, item: PyTree) -> PyTree:
    """Gets parameter names for PyTree elements."""
    return _get_param_names(item)

  def _get_param_infos(self, item: PyTree, directory: epath.Path,
                       save_args: PyTree) -> PyTree:
    """Returns parameter information for elements in `item`.

    At minimum, this method should extract the names of each parameter for
    saving/restoring.

    Args:
      item: a PyTree to extract information from.
      directory: a directory where checkpoint files are located.
      save_args: PyTree matching item containing SaveArgs.

    Returns:
      A PyTree matching `item` of ParamInfo.
    """
    if not item:
      raise ValueError('Found empty item')
    names = self._get_param_names(item)

    def _param_info(name, args):
      return ParamInfo(
          name=name, path=(directory / name), aggregate=args.aggregate)

    return jax.tree_util.tree_map(_param_info, names, save_args)

  async def _write_aggregate_file(self, directory: epath.Path, item: PyTree,
                                  param_infos: PyTree, save_args: PyTree):
    ser_item = _get_tree_for_aggregation(param_infos, save_args, item)
    await self._aggregate_handler.serialize(
        directory / self._aggregate_filename, ser_item)

  async def async_save(
      self,
      directory: epath.Path,
      item: PyTree,
      save_args: Optional[PyTree] = None) -> Optional[List[Future]]:
    """Saves a PyTree from a given training step.

    This operation is compatible with a multi-host, multi-device setting. Tree
    leaf values must be supported by type_handlers. Standard supported types
    include scalars, np.ndarray, jax.Array, string.

    After saving, all files will be located in directory/

    Saves an additional file to directory/checkpoint on host 0 which
    contains the serialized structure of `item`, along with any parameters that
    request aggregation.

    Args:
      directory: save location directory.
      item: a PyTree to be saved.
      save_args: a PyTree matching `item` which consists of SaveArgs objects as
        values.

    Returns:
      A Future that will commit the data to `directory` when awaited. Copying
      the data from its source will be awaited in this function.
    """
    item = await lazy_utils.maybe_get_tree_async(item)

    # Because of empty states, the user-provided args may not contain
    # all necessary arguments. These should be filled in with default args.
    save_args = jax.tree_util.tree_map(
        _maybe_set_default_save_args,
        item,
        item if save_args is None else save_args,
        is_leaf=utils.is_empty_or_leaf,
    )
    param_infos = self._get_param_infos(item, directory, save_args)
    if not self._use_ocdbt:
      # Create directories in parallel.
      await asyncio.gather(
          *jax.tree_util.tree_flatten(
              jax.tree_util.tree_map(
                  _create_param_save_dir, param_infos, save_args
              )
          )[0]
      )
      utils.sync_global_devices(
          'PyTreeCheckpointHandler:create_param_save_dirs'
      )

    async def serialize(value, info, args):
      if args.aggregate:
        return  # skip serialize now, include in aggregated file
      handler = type_handlers.get_type_handler(type(value))
      return await handler.serialize(value, info, args)

    future_tree = jax.tree_util.tree_map(
        serialize, item, param_infos, save_args, is_leaf=utils.is_empty_or_leaf
    )
    copy_futures, _ = jax.tree_util.tree_flatten(future_tree)
    assert isinstance(copy_futures, list)
    # Await copy futures.
    commit_futures = await asyncio.gather(*copy_futures)
    commit_futures, _ = jax.tree_util.tree_flatten(commit_futures)
    await self._write_aggregate_file(directory, item, param_infos, save_args)
    return commit_futures

  def save(self, directory: epath.Path, item: Any, *args, **kwargs):
    """Saves the provided item.

    Blocks until both copy and commit complete.

    See async_save.

    Args:
      directory: the directory to save to.
      item: the item to be saved.
      *args: additional arguments for save.
      **kwargs: additional arguments for save.
    """

    async def async_save(*args, **kwargs):
      commit_futures = await self.async_save(*args, **kwargs)  # pytype: disable=bad-return-type
      # Futures are already running, so sequential waiting is equivalent to
      # concurrent waiting.
      if commit_futures:  # May be None.
        for future in commit_futures:
          future.result()  # Block on result.

    asyncio.run(async_save(directory, item, *args, **kwargs))
    utils.sync_global_devices('PyTreeCheckpointHandler:save')

  async def _maybe_deserialize(self, info: ParamInfo, value: Any,
                               args: RestoreArgs) -> Any:
    """Deserializes using handler or returns already restored value.

    If the ParamInfo indicates that the parameter was aggregated, then it must
    have already been restored. In this case, we simply perform a cast and
    convert to LazyArray if requested.

    Otherwise, we deserialize using an appropriate TypeHandler, converting to
    LazyArray and casting if requested.

    Args:
      info: ParamInfo
      value: a tree value which may have already been restored. Not relevant if
        info.aggregate is False.
      args: RestoreArgs for TypeHandler restoration.

    Returns:
      A deserialized parameter.
    """
    if not isinstance(args, RestoreArgs):
      return value
    if info.aggregate:  # Already restored from AggregateHandler.
      value = _try_array_cast(value, args.dtype)
      if args.lazy:
        value = lazy_utils.LazyValue(lazy_utils.identity(value))
      return value

    handler = type_handlers.get_type_handler(args.restore_type)
    value = lazy_utils.LazyValue(lambda: handler.deserialize(info, args))
    if not args.lazy:
      value = await value.get_async()
    return value

  def restore(
      self,
      directory: epath.Path,
      item: Optional[PyTree] = None,
      restore_args: Optional[PyTree] = None,
      transforms: Optional[PyTree] = None,
      transforms_default_to_original: bool = True,
      transform_fn: Optional[TransformFn] = None,
  ) -> PyTree:
    """Restores a PyTree from the checkpoint directory at the given step.

    Optional arguments meshes and mesh_axes define how each array in the
    restored tree should be partitioned. For more information, see below and see
    pjit documentation.

    Args:
      directory: save location directory.
      item: provides the structure for the restored item. If not provided, will
        infer the structure from the saved checkpoint. Transformations will not
        be run.
      restore_args: optional object containing additional arguments for
        restoration. It should be a PyTree matching the structure of `item`, and
        should contain a RestoreArgs object for every value. If `item` is not
        provided, should match the structure of the checkpoint.
      transforms: a PyTree of transformations that should be applied to the
        saved item in order to obtain a final structure. See `transform_utils`
        for further information.
      transforms_default_to_original: See transform_utils.apply_transformations.
      transform_fn: A function which accepts the `item` argument, a PyTree
        checkpoint structure and a PyTree of ParamInfos based on the checkpoint.
        Returns a transformed PyTree matching the desired return tree structure,
        and a matching ParamInfo tree.

    Returns:
      A PyTree matching the structure of `item`. If `lazy` restoration is
      enabled, leaves will be returned as `LazyValue`.

    Raises:
      FileNotFoundError: `directory` does not exist or is missing required files
      ValueError: `transforms` is provided without `item`.
      ValueError: `transforms` contains elements with `multi_value_fn`.
    """
    if not directory.exists():
      raise FileNotFoundError(
          f'Requested directory for restore does not exist at {directory}')

    restored_structure = self.structure(directory)
    param_infos = _get_param_infos_from_structure(directory, restored_structure)

    if transform_fn is not None and transforms is not None:
      raise ValueError('Cannot provide both `transforms` and `transform_fn`.')
    value_transforms = None
    if transform_fn is None:
      if transforms is not None:
        transforms, value_transforms = transform_utils.separate_value_functions(
            transforms
        )
      item, param_infos = _transform_structure(
          item,
          restored_structure,
          param_infos,
          transforms,
          transforms_default_to_original,
      )
    else:
      item, param_infos = transform_fn(item, restored_structure, param_infos)

    if restore_args is None:
      restore_args = jax.tree_util.tree_map(lambda x: RestoreArgs(), item)

    async def _async_restore(param_infos, item, restore_args):
      concurrent_bytes = self._concurrent_gb * 10**9
      # Construction must take place here so that it is within the same async
      # method, to prevent errors resulting from different event loops, and
      # cannot be created below this level because there must be a single object
      # for the entire restore call.
      byte_limiter = serialization._LimitInFlightBytes(concurrent_bytes)  # pylint: disable=protected-access
      param_infos = jax.tree_util.tree_map(
          functools.partial(dataclasses.replace, byte_limiter=byte_limiter),
          param_infos,
      )
      future_arrays = jax.tree_util.tree_map(
          self._maybe_deserialize,
          param_infos,
          item,
          restore_args,
      )
      future_arrays, tree_structure = jax.tree_util.tree_flatten(future_arrays)
      result = await asyncio.gather(*future_arrays)
      return jax.tree_util.tree_unflatten(tree_structure, result)

    restored_item = asyncio.run(_async_restore(param_infos, item, restore_args))
    restored_item = _apply_value_transforms(restored_item, value_transforms)

    utils.sync_global_devices('PyTreeCheckpointHandler:restore')
    return restored_item

  def structure(self, directory: epath.Path) -> PyTree:
    """Restores the saved PyTree structure without regard for its leaf values.

    Args:
      directory: the directory to restore from.

    Returns:
      The structure of the checkpointed PyTree. Leaves may be of any type.

    Raises:
      FileNotFoundError: if the checkpoint is not found.
    """
    if (directory / self._aggregate_filename).exists():
      return self._aggregate_handler.deserialize(
          directory / self._aggregate_filename
      )
    else:
      raise FileNotFoundError(f'Checkpoint does not exist at {directory}.')
