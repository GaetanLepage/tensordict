# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import ctypes

import dataclasses
import functools
import inspect
import json
import multiprocessing.managers
import multiprocessing.sharedctypes
import numbers
import os
import pickle
import shutil

import sys
import warnings
from copy import copy, deepcopy
from dataclasses import dataclass
from pathlib import Path
from textwrap import indent
from typing import Any, Callable, get_type_hints, List, Sequence, TypeVar

import numpy as np
import tensordict as tensordict_lib

import torch
from tensordict import LazyStackedTensorDict
from tensordict._pytree import _register_td_node
from tensordict._td import is_tensor_collection, NO_DEFAULT, TensorDict, TensorDictBase
from tensordict._tensordict import _unravel_key_to_tuple
from tensordict._torch_func import TD_HANDLED_FUNCTIONS
from tensordict.base import (
    _ACCEPTED_CLASSES,
    _is_tensor_collection,
    _register_tensor_class,
    CompatibleType,
)
from tensordict.utils import (
    _get_repr,
    _is_json_serializable,
    _LOCK_ERROR,
    DeviceType,
    IndexType,
    is_non_tensor,
    is_tensorclass,
    KeyDependentDefaultDict,
    NestedKey,
)
from torch import multiprocessing as mp, Tensor
from torch.multiprocessing import Manager
from torch.utils._pytree import tree_map

T = TypeVar("T", bound=TensorDictBase)
# We use an abstract AnyType instead of Any because Any isn't recognised as a type for python < 3.10
major, minor = sys.version_info[:2]
if (major, minor) < (3, 11):

    class _AnyType:
        def __subclasscheck__(self, subclass):
            return False

else:
    _AnyType = Any

# methods where non_tensordict data should be cleared in the return value
_CLEAR_METADATA = {"all", "any"}
# torch functions where we can wrap the corresponding TensorDict version
_TD_PASS_THROUGH = {
    torch.unbind,
    torch.full_like,
    torch.zeros_like,
    torch.ones_like,
    torch.rand_like,
    torch.empty_like,
    torch.randn_like,
    torch.clone,
    torch.squeeze,
    torch.unsqueeze,
    torch.split,
    torch.permute,
    torch.split,
    torch.stack,
    torch.cat,
    torch.gather,
}
# Methods to be executed from tensordict, any ref to self means 'tensorclass'
_METHOD_FROM_TD = [
    "gather",
    "replace",
]
# Methods to be executed from tensordict, any ref to self means 'self._tensordict'
_FALLBACK_METHOD_FROM_TD = [
    "__abs__",
    "__add__",
    "__iadd__",
    "__imul__",
    "__ipow__",
    "__isub__",
    "__itruediv__",
    "__mul__",
    "__pow__",
    "__sub__",
    "__truediv__",
    "_add_batch_dim",
    "apply",
    "_apply_nest",
    "_fast_apply",
    "apply_",
    "named_apply",
    "_check_unlock",
    "unsqueeze",
    "squeeze",
    "_erase_names",  # TODO: must be specialized
    "_exclude",  # TODO: must be specialized
    "_get_str",
    "_get_tuple",
    "_set_at_tuple",
    "_has_names",
    "_propagate_lock",
    "_propagate_unlock",
    "_remove_batch_dim",
    "is_memmap",
    "is_shared",
    "_select",  # TODO: must be specialized
    "_set_str",
    "_set_tuple",
    "all",
    "any",
    "empty",
    "exclude",
    "expand",
    "expand_as",
    "is_empty",
    "is_shared",
    "items",
    "keys",
    "lock_",
    "masked_fill",
    "masked_fill_",
    "permute",
    "flatten",
    "unflatten",
    "ndimension",
    "rename_",  # TODO: must be specialized
    "reshape",
    "select",
    "to",
    "transpose",
    "unlock_",
    "values",
    "view",
    "zero_",
    "add",
    "add_",
    "mul",
    "mul_",
    "abs",
    "abs_",
    "acos",
    "acos_",
    "exp",
    "exp_",
    "neg",
    "neg_",
    "reciprocal",
    "reciprocal_",
    "sigmoid",
    "sigmoid_",
    "sign",
    "sign_",
    "sin",
    "sin_",
    "sinh",
    "sinh_",
    "tan",
    "tan_",
    "tanh",
    "tanh_",
    "trunc",
    "trunc_",
    "norm",
    "lgamma",
    "lgamma_",
    "frac",
    "frac_",
    "expm1",
    "expm1_",
    "log",
    "log_",
    "log10",
    "log10_",
    "log1p",
    "log1p_",
    "log2",
    "log2_",
    "ceil",
    "ceil_",
    "floor",
    "floor_",
    "round",
    "round_",
    "erf",
    "erf_",
    "erfc",
    "erfc_",
    "asin",
    "asin_",
    "atan",
    "atan_",
    "cos",
    "cos_",
    "cosh",
    "cosh_",
    "lerp",
    "lerp_",
    "addcdiv",
    "addcdiv_",
    "addcmul",
    "addcmul_",
    "sub",
    "sub_",
    "maximum_",
    "maximum",
    "minimum_",
    "minimum",
    "clamp_max_",
    "clamp_max",
    "clamp_min_",
    "clamp_min",
    "pow",
    "pow_",
    "div",
    "div_",
    "sqrt",
    "sqrt_",
]
_FALLBACK_METHOD_FROM_TD_COPY = [
    "_clone",  # TODO: must be specialized
    "clone",  # TODO: must be specialized
    "copy",  # TODO: must be specialized
]


class tensorclass:
    """A decorator to create :obj:`tensorclass` classes.

    :obj:`tensorclass` classes are specialized :obj:`dataclass` instances that
    can execute some pre-defined tensor operations out of the box, such as
    indexing, item assignment, reshaping, casting to device or storage and many
    others.

    Examples:
        >>> from tensordict import tensorclass
        >>> import torch
        >>> from typing import Optional
        >>>
        >>> @tensorclass
        ... class MyData:
        ...     X: torch.Tensor
        ...     y: torch.Tensor
        ...     z: str
        ...     def expand_and_mask(self):
        ...         X = self.X.unsqueeze(-1).expand_as(self.y)
        ...         X = X[self.y]
        ...         return X
        ...
        >>> data = MyData(
        ...     X=torch.ones(3, 4, 1),
        ...     y=torch.zeros(3, 4, 2, 2, dtype=torch.bool),
        ...     z="test"
        ...     batch_size=[3, 4])
        >>> print(data)
        MyData(
            X=Tensor(torch.Size([3, 4, 1]), dtype=torch.float32),
            y=Tensor(torch.Size([3, 4, 2, 2]), dtype=torch.bool),
            z="test"
            batch_size=[3, 4],
            device=None,
            is_shared=False)
        >>> print(data.expand_and_mask())
        tensor([])

    It is also possible to nest tensorclasses instances within each other:
        Examples:
        >>> from tensordict import tensorclass
        >>> import torch
        >>> from typing import Optional
        >>>
        >>> @tensorclass
        ... class NestingMyData:
        ...     nested: MyData
        ...
        >>> nesting_data = NestingMyData(nested=data, batch_size=[3, 4])
        >>> # although the data is stored as a TensorDict, the type hint helps us
        >>> # to appropriately cast the data to the right type
        >>> assert isinstance(nesting_data.nested, type(data))


    """

    def __new__(cls, autocast: bool = False):
        if not isinstance(autocast, bool):
            clz = autocast
            self = super().__new__(cls)
            self.__init__(autocast=False)
            return self.__call__(clz)
        return super().__new__(cls)

    def __init__(self, autocast: bool):
        self.autocast = autocast

    def __call__(self, cls):
        clz = _tensorclass(cls)
        clz.autocast = self.autocast
        return clz


def _tensorclass(cls: T) -> T:
    def __torch_function__(
        cls,
        func: Callable,
        types: tuple[type, ...],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Callable:
        if func not in _TD_PASS_THROUGH or not all(
            issubclass(t, (Tensor, cls, TensorDictBase)) for t in types
        ):
            return NotImplemented

        if kwargs is None:
            kwargs = {}

        # get the output type from the arguments / keyword arguments
        if len(args) > 0:
            tensorclass_instance = args[0]
        else:
            tensorclass_instance = kwargs.get("input", kwargs["tensors"])
        if isinstance(tensorclass_instance, (tuple, list)):
            tensorclass_instance = tensorclass_instance[0]
        args = tuple(_arg_to_tensordict(arg) for arg in args)
        kwargs = {key: _arg_to_tensordict(value) for key, value in kwargs.items()}

        result = TD_HANDLED_FUNCTIONS[func](*args, **kwargs)
        if isinstance(result, (list, tuple)):
            return result.__class__(
                _from_tensordict_with_copy(tensorclass_instance, tensordict_result)
                for tensordict_result in result
            )
        return _from_tensordict_with_copy(tensorclass_instance, result)

    _is_non_tensor = getattr(cls, "_is_non_tensor", False)

    cls = dataclass(cls)
    expected_keys = set(cls.__dataclass_fields__)

    for attr in cls.__dataclass_fields__:
        if attr in dir(TensorDict) and attr not in ("_is_non_tensor", "data"):
            raise AttributeError(
                f"Attribute name {attr} can't be used with @tensorclass"
            )

    cls.fields = classmethod(lambda cls: dataclasses.fields(cls))
    for field in cls.fields():
        if hasattr(cls, field.name):
            delattr(cls, field.name)

    _get_type_hints(cls)
    cls.__init__ = _init_wrapper(cls.__init__)
    cls._from_tensordict = classmethod(_from_tensordict_wrapper(expected_keys))
    cls.from_tensordict = cls._from_tensordict
    if not hasattr(cls, "__torch_function__"):
        cls.__torch_function__ = classmethod(__torch_function__)
    cls.__getstate__ = _getstate
    cls.__setstate__ = _setstate
    # cls.__getattribute__ = object.__getattribute__
    cls.__getattr__ = _getattr
    cls.__setattr__ = _setattr_wrapper(cls.__setattr__, expected_keys)
    # cls.__getattr__ = _getattr
    cls.__getitem__ = _getitem
    cls.__getitems__ = _getitem
    cls.__setitem__ = _setitem
    cls.__repr__ = _repr
    cls.__len__ = _len
    cls.__eq__ = _eq
    cls.__ne__ = _ne
    cls.__or__ = _or
    cls.__xor__ = _xor
    cls.__bool__ = _bool
    cls.non_tensor_items = _non_tensor_items
    # if not hasattr(cls, "keys"):
    #     cls.keys = _keys
    # if not hasattr(cls, "values"):
    #     cls.values = _values
    # if not hasattr(cls, "items"):
    #     cls.items = _items
    if not hasattr(cls, "set"):
        cls.set = _set
    if not hasattr(cls, "set_at_"):
        cls.set_at_ = _set_at_
    if not hasattr(cls, "del_"):
        cls.del_ = _del_
    if not hasattr(cls, "get"):
        cls.get = _get
    if not hasattr(cls, "get_at"):
        cls.get_at = _get_at
    if not hasattr(cls, "unbind"):
        cls.unbind = _unbind
    cls._unbind = _unbind
    if not hasattr(cls, "state_dict"):
        cls.state_dict = _state_dict
    if not hasattr(cls, "load_state_dict"):
        cls.load_state_dict = _load_state_dict
    if not hasattr(cls, "_memmap_"):
        cls._memmap_ = _memmap_
    if not hasattr(cls, "share_memory_"):
        cls.share_memory_ = _share_memory_
    if not hasattr(cls, "update"):
        cls.update = _update
    if not hasattr(cls, "update_"):
        cls.update_ = _update_
    if not hasattr(cls, "update_at_"):
        cls.update_at_ = _update_at_
    for method_name in _METHOD_FROM_TD:
        if not hasattr(cls, method_name):
            setattr(cls, method_name, getattr(TensorDict, method_name))
    for method_name in _FALLBACK_METHOD_FROM_TD:
        if not hasattr(cls, method_name):
            setattr(cls, method_name, _wrap_td_method(method_name))
    for method_name in _FALLBACK_METHOD_FROM_TD_COPY:
        if not hasattr(cls, method_name):
            setattr(
                cls,
                method_name,
                _wrap_td_method(method_name, copy_non_tensor=True),
            )

    if not hasattr(cls, "_apply_nest"):
        cls._apply_nest = TensorDict._apply_nest

    if not hasattr(cls, "filter_non_tensor_data"):
        cls.filter_non_tensor_data = _filter_non_tensor_data
    cls.__enter__ = __enter__
    cls.__exit__ = __exit__

    # Memmap
    if not hasattr(cls, "memmap_like"):
        cls.memmap_like = TensorDictBase.memmap_like
    if not hasattr(cls, "memmap_"):
        cls.memmap_ = TensorDictBase.memmap_
    if not hasattr(cls, "memmap"):
        cls.memmap = TensorDictBase.memmap
    if not hasattr(cls, "load_memmap"):
        cls.load_memmap = TensorDictBase.load_memmap
    if not hasattr(cls, "_load_memmap"):
        cls._load_memmap = classmethod(_load_memmap)
    if not hasattr(cls, "from_dict"):
        cls.from_dict = classmethod(_from_dict)
    if not hasattr(cls, "from_dict_instance"):
        cls.from_dict_instance = _from_dict_instance

    for attr in TensorDict.__dict__.keys():
        func = getattr(TensorDict, attr)
        if inspect.ismethod(func) and attr not in cls.__dict__:
            tdcls = func.__self__
            if issubclass(tdcls, TensorDictBase):  # detects classmethods
                setattr(cls, attr, _wrap_classmethod(tdcls, cls, func))

    if not hasattr(cls, "to_tensordict"):
        cls.to_tensordict = _to_tensordict
    if not hasattr(cls, "device"):
        cls.device = property(_device, _device_setter)
    if not hasattr(cls, "batch_size"):
        cls.batch_size = property(_batch_size, _batch_size_setter)
    if not hasattr(cls, "names"):
        cls.names = property(_names, _names_setter)
    if not hasattr(cls, "to_dict"):
        cls.to_dict = _to_dict

    cls.__doc__ = f"{cls.__name__}{inspect.signature(cls)}"

    _register_tensor_class(cls)
    _register_td_node(cls)

    # faster than doing instance checks
    cls._is_non_tensor = _is_non_tensor
    cls._is_tensorclass = True

    from tensordict import _pytree

    _pytree._CONSTRUCTORS[cls] = _pytree._tensorclass_constructor
    return cls


def _filter_non_tensor_data(self):
    return super(type(self), self).__getattribute__("_tensordict")


def _arg_to_tensordict(arg):
    # if arg is a tensorclass or sequence of tensorclasses, extract the underlying
    # tensordicts and return those instead
    if is_tensorclass(arg):
        return arg._tensordict
    elif isinstance(arg, (tuple, list)) and all(is_tensorclass(item) for item in arg):
        return arg.__class__(item._tensordict for item in arg)
    return arg


def _from_tensordict_with_copy(tc, tensordict):
    # creates a new tensorclass with the same type as tc, and a copy of the
    # non_tensordict data
    return tc._from_tensordict(
        tensordict=tensordict, non_tensordict=copy(tc._non_tensordict)
    )


def _from_tensordict_with_none(tc, tensordict):
    # creates a new tensorclass with the same type as tc, and all non_tensordict entries
    # set to None
    return tc._from_tensordict(
        tensordict=tensordict,
        non_tensordict={key: None for key in tc._non_tensordict},
    )


def _init_wrapper(init: Callable) -> Callable:
    init_sig = inspect.signature(init)
    params = list(init_sig.parameters.values())
    # drop first entry of params which corresponds to self and isn't passed by the user
    required_params = [p.name for p in params[1:] if p.default is inspect._empty]

    @functools.wraps(init)
    def wrapper(
        self,
        *args: Any,
        batch_size: Sequence[int] | torch.Size | int = None,
        device: DeviceType | None = None,
        names: List[str] | None = None,
        **kwargs,
    ):

        for value, key in zip(args, self.__dataclass_fields__):
            if key in kwargs:
                raise ValueError(f"The key {key} is already set in kwargs")
            kwargs[key] = value
        if batch_size is None:
            batch_size = torch.Size([])
        for key, field in self.__dataclass_fields__.items():
            if field.default_factory is not dataclasses.MISSING:
                default = field.default_factory()
            else:
                default = field.default
            if default not in (None, dataclasses.MISSING):
                kwargs.setdefault(key, default)

        missing_params = [p for p in required_params if p not in kwargs]
        if missing_params:
            n_missing = len(missing_params)
            raise TypeError(
                f"{self.__class__.__name__}.__init__() missing {n_missing} "
                f"required positional argument{'' if n_missing == 1 else 's'}: "
                f"""{", ".join(f"'{name}'" for name in missing_params)}"""
            )

        self._tensordict = TensorDict(
            {},
            batch_size=torch.Size(batch_size),
            device=device,
            names=names,
            _run_checks=False,
        )
        self._non_tensordict = {}

        init(self, **kwargs)

    new_params = [
        inspect.Parameter("batch_size", inspect.Parameter.KEYWORD_ONLY),
        inspect.Parameter("device", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("names", inspect.Parameter.KEYWORD_ONLY, default=None),
    ]
    wrapper.__signature__ = init_sig.replace(parameters=params + new_params)

    return wrapper


_cast_funcs = KeyDependentDefaultDict(lambda cls: cls)
_cast_funcs[torch.Tensor] = torch.as_tensor
_cast_funcs[np.ndarray] = np.asarray


def _get_type_hints(cls, with_locals=False):
    #######
    # Set proper type annotations for autocasting to tensordict/tensorclass
    #
    # by updating locals, we can allow this to be used within a function
    # local-cross referencing will not work though
    # def foo():
    #     @tensorclass
    #     class MyOtherClass:
    #         x: torch.Tensor
    #     @tensorclass
    #     class MyClass:
    #         x: MyClass # works
    #         y: MyOtherClass # fails
    #
    # In this case, we will use the get_parent_local function to get the locals
    # from the parent frame and so recursively until we can find the class.

    if with_locals:
        # This function gets the parent frame recursively until we can find the current class.
        # Any exception leads to this to be None and auto-casting will be disabled
        localns = locals()
        localns = copy(localns)

        def get_parent_locals(cls, localns=localns):
            # Get the current frame
            frame = inspect.currentframe()
            try:
                parent_locs = localns
                while cls.__name__ not in parent_locs:
                    # Get the parent frame
                    parent_frame = frame.f_back
                    # Get the locals dictionary of the parent frame
                    parent_locs = parent_frame.f_locals
                    frame = parent_frame
            except Exception:
                localns.setdefault(cls.__name__, cls)
                return localns
            finally:
                # Clean up the frame reference
                del frame
            return copy(parent_locs)

        localns = get_parent_locals(cls)
    else:
        localns = None

    globalns = None

    try:
        cls._type_hints = get_type_hints(
            cls,
            localns=localns,
            # globalns=globals(),
        )
        cls._type_hints = {
            key: val if isinstance(val, type) else _AnyType
            for key, val in cls._type_hints.items()
        }
    except NameError:
        if not with_locals:
            return _get_type_hints(cls, with_locals=True)
        cls._set_dict_warn_msg = (
            "A NameError occurred while trying to retrieve a type annotation. "
            "This can occur when a tensorclass references another locally defined "
            "tensorclass. "
            f"As a result type hints cannot be read and {cls}.from_dict(...) "
            f"or `{cls}.set` will not attempt to map dictionaries to "
            "the relevant tensorclass. To resolve this issue, consider defining "
            "your tensorclass globally."
        )
        cls._type_hints = None
    except TypeError:
        # This is a rather common case where type annotation is like
        # class MyClass:
        #     x: int | str
        # in which case get_type_hints doesn't work (it does work
        # however with old-school Optional or Union...)
        # We simply differ the warning till _set() is called
        cls._set_dict_warn_msg = (
            "A TypeError occurred when trying to retrieve a type annotation. "
            "This may be caused by annotations that use plain `|` instead of typing.Union "
            "or typing.Optional which are supported. If you wish to use the feature "
            "of setting dict as attributes with automapping to tensordict/tensorclass "
            "(`my_obj.attr = dict(...)`), consider re-writing the tensorclass with "
            "traditional type annotations."
        )
        cls._type_hints = None


def _from_tensordict_wrapper(expected_keys):
    def wrapper(cls, tensordict, non_tensordict=None):  # noqa: D417
        """Tensor class wrapper to instantiate a new tensor class object.

        Args:
            tensordict (TensorDict): Dictionary of tensor types
            non_tensordict (dict): Dictionary with non-tensor and nested tensor class objects

        """
        if not isinstance(tensordict, TensorDictBase):
            raise RuntimeError(
                f"Expected a TensorDictBase instance but got {type(tensordict)}"
            )
        # Validating keys of tensordict
        for key in tensordict.keys():
            if key not in expected_keys:
                raise ValueError(
                    f"Keys from the tensordict ({set(tensordict.keys())}) must "
                    f"correspond to the class attributes ({expected_keys})."
                )

        # Validating non-tensor keys and for key clash
        tensor_keys = set(tensordict.keys())
        if non_tensordict is not None:
            for key in list(non_tensordict.keys()):
                if key not in expected_keys:
                    raise ValueError(
                        f"Keys from the non-tensor data ({set(non_tensordict.keys())}) must "
                        f"correspond to the class attributes ({expected_keys})."
                    )
                if key in tensor_keys:
                    if non_tensordict[key] is None:
                        del non_tensordict[key]
                        continue
                    raise KeyError(
                        f"{key} is present in both tensor and non-tensor dicts."
                    )
        # bypass initialisation. this means we don't incur any overhead creating an
        # empty tensordict and writing values to it. we can skip this because we already
        # have a tensordict to use as the underlying tensordict
        tc = cls.__new__(cls)
        tc.__dict__["_tensordict"] = tensordict

        tc.__dict__["_non_tensordict"] = (
            non_tensordict if non_tensordict is not None else {}
        )
        # since we aren't calling the dataclass init method, we need to manually check
        # whether a __post_init__ method has been defined and invoke it if so
        if hasattr(tc, "__post_init__"):
            tc.__post_init__()
        return tc

    return wrapper


def _memmap_(
    self,
    *,
    prefix: str | None = None,
    copy_existing: bool = False,
    executor=None,
    futures=None,
    inplace=True,
    like=False,
    memmaped: bool = False,
    share_non_tensor: bool = False,
):
    _non_tensordict = copy(self._non_tensordict)
    cls = self.__class__

    if not memmaped and prefix is not None:
        prefix = Path(prefix)
        if not prefix.exists():
            os.makedirs(prefix, exist_ok=True)

        def save_metadata(cls=cls, _non_tensordict=_non_tensordict, prefix=prefix):
            with open(prefix / "meta.json", "w") as f:
                metadata = {"_type": str(cls)}
                to_pickle = {}
                for key, value in _non_tensordict.items():
                    value = _from_shared_nontensor(value)
                    if _is_json_serializable(value):
                        metadata[key] = value
                    else:
                        to_pickle[key] = value
                json.dump(metadata, f)
                if to_pickle:
                    with open(prefix / "other.pickle", "wb") as pickle_file:
                        pickle.dump(to_pickle, pickle_file)

        if executor is None:
            save_metadata()
        else:
            futures.append(executor.submit(save_metadata))

        prefix = prefix / "_tensordict"

    td = self._tensordict._memmap_(
        prefix=prefix,
        executor=executor,
        futures=futures,
        inplace=inplace,
        like=like,
        copy_existing=copy_existing,
        share_non_tensor=share_non_tensor,
    )
    td._device = torch.device("cpu")
    if not inplace:
        result = cls._from_tensordict(td, _non_tensordict)
    else:
        result = self
    return result


def _share_memory_(self):
    self._tensordict.share_memory_()
    return self


def _load_memmap(cls, prefix: Path, metadata: dict, **kwargs):
    non_tensordict = copy(metadata)
    del non_tensordict["_type"]
    if os.path.exists(prefix / "other.pickle"):
        with open(prefix / "other.pickle", "rb") as pickle_file:
            non_tensordict.update(pickle.load(pickle_file))
    td = TensorDict.load_memmap(prefix / "_tensordict", **kwargs, non_blocking=False)
    return cls._from_tensordict(td, non_tensordict)


def __enter__(self, *args, **kwargs):
    return self._tensordict.__enter__(*args, **kwargs)


def __exit__(self, *args, **kwargs):
    return self._tensordict.__exit__(*args, **kwargs)


def _getstate(self) -> dict[str, Any]:
    """Returns a state dict which consists of tensor and non_tensor dicts for serialization.

    Returns:
        dictionary of state of tensor class

    """
    return {"tensordict": self._tensordict, "non_tensordict": self._non_tensordict}


def _setstate(self, state: dict[str, Any]) -> None:  # noqa: D417
    """Used to set the state of an object using state parameter.

    Args:
        state (dict): State parameter to set the object
    """
    self._tensordict = state.get("tensordict", None)
    self._non_tensordict = state.get("non_tensordict", None)


def _getattr(self, item: str) -> Any:
    # if not item.startswith("__"):
    __dict__ = self.__dict__
    _non_tensordict = __dict__.get("_non_tensordict")
    if _non_tensordict is not None:
        out = _non_tensordict.get(item, NO_DEFAULT)
        if out is not NO_DEFAULT:
            if (
                isinstance(self, NonTensorData)
                and item == "data"
                and (self._is_shared or self._is_memmap)
            ):
                return _from_shared_nontensor(out)
            return out
    _tensordict = __dict__.get("_tensordict")
    if _tensordict is not None:
        out = _tensordict._get_str(item, default=None)
        if out is not None:
            return out
        out = getattr(_tensordict, item, NO_DEFAULT)
        if out is not NO_DEFAULT:
            if not callable(out):
                return out
            return _wrap_method(self, item, out)
    raise AttributeError


SET_ATTRIBUTES = ("batch_size", "device", "_locked_tensordicts", "names")


def _setattr_wrapper(setattr_: Callable, expected_keys: set[str]) -> Callable:
    @functools.wraps(setattr_)
    def wrapper(self, key: str, value: Any) -> None:  # noqa: D417
        """Set the value of an attribute for the tensor class object.

        Args:
            key (str): the name of the attribute to set
            value (any): the value to set for the attribute

        """
        __dict__ = self.__dict__
        if (
            "_tensordict" not in __dict__
            or "_non_tensordict" not in __dict__
            or key in SET_ATTRIBUTES
        ):
            return setattr_(self, key, value)

        out = self.set(key, value)
        if out is not self:
            raise RuntimeError(
                "Cannot set attribute on a locked tensorclass, even if "
                "clone_on_set is set to True. Use my_obj.set(...) instead."
            )

    return wrapper


def _wrap_td_method(funcname, *, copy_non_tensor=False):
    def wrapped_func(self, *args, **kwargs):
        td = super(type(self), self).__getattribute__("_tensordict")
        result = getattr(td, funcname)(*args, **kwargs)

        def check_out(kwargs, result):
            out = kwargs.get("out")
            if out is result:
                # No need to transform output
                return True
            return False

        if isinstance(result, TensorDictBase) and not check_out(kwargs, result):
            if result is td:
                return self
            nontd = super(type(self), self).__getattribute__("_non_tensordict")
            if copy_non_tensor:
                # use tree_map to copy
                nontd = tree_map(lambda x: x, nontd)
            return super(type(self), self).__getattribute__("_from_tensordict")(
                result, nontd
            )
        return result

    return wrapped_func


def _wrap_method(self, attr, func):
    warnings.warn(
        f"The method {func} wasn't explicitly implemented for tensorclass. "
        f"This fallback will be deprecated in future releases because it is inefficient "
        f"and non-compilable. Please raise an issue in tensordict repo to support this method!"
    )

    @functools.wraps(func)
    def wrapped_func(*args, **kwargs):
        args = tuple(_arg_to_tensordict(arg) for arg in args)
        kwargs = {key: _arg_to_tensordict(value) for key, value in kwargs.items()}
        res = func(*args, **kwargs)
        if isinstance(res, TensorDictBase):
            if attr.endswith("_"):
                # in-place operation, return the current object
                return self
            elif attr in _CLEAR_METADATA:
                # this is an attribute where copying the metadata makes no sense, e.g.
                # .all or .any, so we replace all values with None
                return self._from_tensordict(
                    res, {k: None for k in self._non_tensordict}
                )
            # create a new tensorclass from res and copy the metadata from self
            return self._from_tensordict(res, copy(self._non_tensordict))
        return res

    return wrapped_func


def _update(
    self,
    input_dict_or_td: dict[str, CompatibleType] | T,
    clone: bool = False,
    inplace: bool = False,
    *,
    keys_to_update: Sequence[NestedKey] | None = None,
    non_blocking: bool = False,
):
    if isinstance(input_dict_or_td, dict):
        input_dict_or_td = self.from_dict(input_dict_or_td)

    if is_tensorclass(input_dict_or_td):
        self._tensordict.update(input_dict_or_td.__dict__["_tensordict"])
        self._non_tensordict.update(input_dict_or_td.__dict__["_non_tensordict"])
        return self

    non_tensordict = {}
    for key, value in input_dict_or_td.items():
        if is_non_tensor(value):
            non_tensordict[key] = value.data

    self._tensordict.update(
        input_dict_or_td.exclude(*non_tensordict.keys()),
        clone=clone,
        inplace=inplace,
        keys_to_update=keys_to_update,
        non_blocking=non_blocking,
    )
    self._non_tensordict.update(non_tensordict)
    return self


def _update_(
    self,
    input_dict_or_td: dict[str, CompatibleType] | T,
    clone: bool = False,
    inplace: bool = False,
    *,
    keys_to_update: Sequence[NestedKey] | None = None,
    non_blocking: bool = False,
):
    if isinstance(input_dict_or_td, dict):
        input_dict_or_td = self.from_dict(input_dict_or_td, batch_size=self.batch_size)

    if is_tensorclass(input_dict_or_td):
        self._tensordict.update_(input_dict_or_td._tensordict)
        self._non_tensordict.update(input_dict_or_td._non_tensordict)
        return self

    non_tensordict = {}
    for key, value in input_dict_or_td.items():
        if is_non_tensor(value):
            non_tensordict[key] = value.data

    self._tensordict.update_(
        input_dict_or_td.exclude(*non_tensordict.keys()),
        clone=clone,
        inplace=inplace,
        keys_to_update=keys_to_update,
        non_blocking=non_blocking,
    )
    self._non_tensordict.update(non_tensordict)
    return self


def _update_at_(
    self,
    input_dict_or_td: dict[str, CompatibleType] | T,
    index: IndexType,
    clone: bool = False,
    *,
    keys_to_update: Sequence[NestedKey] | None = None,
    non_blocking: bool = False,
):
    if isinstance(input_dict_or_td, dict):
        input_dict_or_td = self.from_dict(input_dict_or_td, batch_size=self.batch_size)

    if is_tensorclass(input_dict_or_td):
        self._tensordict.update(input_dict_or_td._tensordict)
        self._non_tensordict.update(input_dict_or_td._non_tensordict)
        return self

    non_tensordict = {}
    for key, value in input_dict_or_td.items():
        if is_non_tensor(value):
            non_tensordict[key] = value.data

    self._tensordict.update_at_(
        input_dict_or_td.exclude(*non_tensordict.keys()),
        index=index,
        clone=clone,
        keys_to_update=keys_to_update,
        non_blocking=non_blocking,
    )
    self._non_tensordict.update(non_tensordict)
    return self


def _wrap_classmethod(td_cls, cls, func):
    @functools.wraps(func)
    def wrapped_func(*args, **kwargs):
        res = func.__get__(td_cls)(*args, **kwargs)
        # res = func(*args, **kwargs)
        if isinstance(res, TensorDictBase):
            # create a new tensorclass from res and copy the metadata from self
            return cls._from_tensordict(res)
        return res

    return wrapped_func


def _getitem(self, item: NestedKey) -> Any:
    """Retrieve the class object at the given index. Indexing will happen for nested tensors as well.

    Args:
       item (int or any other valid index type): index of the object to retrieve

    Returns:
        Tensor class object at the given index

    """
    if isinstance(item, str) or (
        isinstance(item, tuple) and all(isinstance(_item, str) for _item in item)
    ):
        raise ValueError(f"Invalid indexing arguments: {item}.")
    tensor_res = self._tensordict[item]
    return _from_tensordict_with_copy(self, tensor_res)  # device=res.device)


def _setitem(self, item: NestedKey, value: Any) -> None:  # noqa: D417
    """Set the value of the Tensor class object at the given index. Note that there is no strict validation on non-tensor values.

    Args:
        item (int or any other valid index type): index of the object to set
        value (any): value to set for the item

    """
    if isinstance(item, str) or (
        isinstance(item, tuple) and all(isinstance(_item, str) for _item in item)
    ):
        raise ValueError(f"Invalid indexing arguments: {item}.")

    if not is_tensorclass(value) and not isinstance(
        value, (TensorDictBase, numbers.Number, Tensor)
    ):
        raise ValueError(
            f"__setitem__ only supports tensorclasses, tensordicts,"
            f" numeric scalars and tensors. Got {type(value)}"
        )

    if is_tensorclass(value):
        if not isinstance(value, self.__class__):
            self_keys = set().union(self._non_tensordict, self._tensordict.keys())
            value_keys = set().union(value._non_tensordict, value._tensordict.keys())
            if self_keys != value_keys:
                # if tensorclass but different class ensure that all keys are equal
                raise ValueError(
                    "__setitem__ is only allowed for same-class or "
                    "compatible class (i.e. same members) assignment"
                )

        # Validating the non-tensor data before setting the item
        for key, val in value._non_tensordict.items():
            # Raise a warning if non_tensor data doesn't match
            if (
                key in self._non_tensordict.keys()
                and val is not self._non_tensordict[key]
            ):
                warnings.warn(
                    f"Meta data at {repr(key)} may or may not be equal, "
                    f"this may result in undefined behaviours",
                    category=UserWarning,
                    stacklevel=2,
                )

        for key in value._tensordict.keys():
            # Making sure that the key-clashes won't happen, if the key is present
            # in tensor data in value we will honor that and remove the key-value
            # pair from non-tensor data
            if key in self._non_tensordict.keys():
                del self._non_tensordict[key]

        self._tensordict[item] = value._tensordict
    elif isinstance(value, TensorDictBase):  # it is one of accepted "broadcast" types
        # attempt broadcast on all tensordata and nested tensorclasses
        self._tensordict[item] = value.filter_non_tensor_data()
        self._non_tensordict.update(
            {
                key: val.data
                for key, val in value.items(is_leaf=is_non_tensor, leaves_only=True)
            }
        )
    else:
        # int, float etc.
        self._tensordict[item] = value


def _repr(self) -> str:
    """Return a string representation of Tensor class object."""
    fields = _all_td_fields_as_str(self._tensordict)
    field_str = [fields] if fields else []
    non_tensor_fields = _all_non_td_fields_as_str(self._non_tensordict)
    batch_size_str = indent(f"batch_size={self.batch_size}", 4 * " ")
    device_str = indent(f"device={self.device}", 4 * " ")
    is_shared_str = indent(f"is_shared={self.is_shared()}", 4 * " ")
    if len(non_tensor_fields) > 0:
        non_tensor_field_str = indent(
            ",\n".join(non_tensor_fields),
            4 * " ",
        )
        string = ",\n".join(
            field_str
            + [non_tensor_field_str, batch_size_str, device_str, is_shared_str]
        )
    else:
        string = ",\n".join(field_str + [batch_size_str, device_str, is_shared_str])
    return f"{self.__class__.__name__}(\n{string})"


def _len(self) -> int:
    """Returns the length of first dimension, if there is, otherwise 0."""
    return len(self._tensordict)


def _to_dict(self) -> dict:
    td_dict = self._tensordict.to_dict()
    td_dict.update(self._non_tensordict)
    return td_dict


def _from_dict(cls, input_dict, batch_size=None, device=None, batch_dims=None):
    # we pass through a tensordict because keys could be passed as NestedKeys
    # We can't assume all keys are strings, otherwise calling cls(**kwargs)
    # would work ok

    td = TensorDict.from_dict(
        input_dict, batch_size=batch_size, device=device, batch_dims=batch_dims
    )
    non_tensor = {}

    # Note: this won't deal with sub-tensordicts which may or may not be tensorclasses.
    # We don't want to enforce them to be tensorclasses so we can't do much about it...
    for key, value in list(td.items()):
        if is_non_tensor(value):
            non_tensor[key] = value.data
            del td[key]

    return cls.from_tensordict(tensordict=td, non_tensordict=non_tensor)


def _from_dict_instance(
    self, input_dict, batch_size=None, device=None, batch_dims=None
):
    if batch_dims is not None and batch_size is not None:
        raise ValueError("Cannot pass both batch_size and batch_dims to `from_dict`.")
    from tensordict import TensorDict

    batch_size_set = torch.Size(()) if batch_size is None else batch_size
    # TODO: this is a bit slow and will be a bottleneck every time td[idx] = dict(subtd)
    # is called when there are non tensor data in it
    if not _is_tensor_collection(type(input_dict)):
        input_tdict = TensorDict.from_dict(input_dict)
    else:
        input_tdict = input_dict
    trsf_dict = {}
    for key, value in list(input_tdict.items()):
        # cur_value = getattr(self, key, None)
        cur_value = self.get(key, None)
        if _is_tensor_collection(type(cur_value)):
            trsf_dict[key] = cur_value.from_dict_instance(
                value, batch_size=[], device=device, batch_dims=None
            )
        elif not isinstance(cur_value, torch.Tensor) and is_non_tensor(value):
            trsf_dict[key] = value.data
        elif cur_value is not None and not isinstance(cur_value, torch.Tensor):
            # This is slightly unsafe but will work with bool, float and int
            try:
                trsf_dict[key] = type(cur_value)(value)
            except Exception:
                trsf_dict[key] = input_dict[key]
        else:
            trsf_dict[key] = value
    out = type(self)(
        **trsf_dict,
        batch_size=batch_size_set,
        device=device,
    )
    # check that
    if batch_size is None:
        out._tensordict.auto_batch_size_()
    return out


def _to_tensordict(self) -> TensorDict:
    """Convert the tensorclass into a regular TensorDict.

    Makes a copy of all entries. Memmap and shared memory tensors are converted to
    regular tensors.

    Returns:
        A new TensorDict object containing the same values as the tensorclass.

    """
    td = self._tensordict.to_tensordict()
    for key, val in self._non_tensordict.items():
        td.set_non_tensor(key, val)
    return td


def _device(self) -> torch.device:
    """Retrieves the device type of tensor class."""
    return self._tensordict.device


def _device_setter(self, value: DeviceType) -> None:
    raise RuntimeError(
        "device cannot be set using tensorclass.device = device, "
        "because device cannot be updated in-place. To update device, use "
        "tensorclass.to(new_device), which will return a new tensorclass "
        "on the new device."
    )


def _set(
    self, key: NestedKey, value: Any, inplace: bool = False, non_blocking: bool = False
):
    """Sets a new key-value pair.

    Args:
        key (str, tuple of str): name of the key to be set.
           If tuple of str it is equivalent to chained calls of getattr
           followed by a final setattr.
        value (Any): value to be stored in the tensorclass
        inplace (bool, optional): if ``True``, set will tentatively try to
            update the value in-place. If ``False`` or if the key isn't present,
            the value will be simply written at its destination.

    Returns:
        self

    """
    if isinstance(key, str):
        cls = type(self)
        __dict__ = self.__dict__
        if __dict__["_tensordict"].is_locked:
            raise RuntimeError(_LOCK_ERROR)
        if key in ("batch_size", "names", "device"):
            # handled by setattr
            return
        expected_keys = cls.__dataclass_fields__
        if key not in expected_keys:
            raise AttributeError(
                f"Cannot set the attribute '{key}', expected attributes are {expected_keys}."
            )

        def set_tensor(
            key=key, value=value, inplace=inplace, non_blocking=non_blocking
        ):
            # Avoiding key clash, honoring the user input to assign tensor type data to the key
            if key in self._non_tensordict.keys():
                if inplace:
                    raise RuntimeError(
                        f"Cannot update an existing entry of type {type(self._non_tensordict.get(key))} with a value of type {type(value)}."
                    )
                del self._non_tensordict[key]
            self._tensordict.set(key, value, inplace=inplace, non_blocking=non_blocking)
            return self

        def _is_castable(datatype):
            return issubclass(datatype, (int, float, np.ndarray))

        if cls.autocast:
            type_hints = cls._type_hints
            if type_hints is not None:
                target_cls = type_hints.get(key, _AnyType)
            else:
                warnings.warn("type_hints are none, cannot perform auto-casting")
                target_cls = _AnyType

            if isinstance(value, dict):
                if _is_tensor_collection(target_cls):
                    value = target_cls.from_dict(value)
                    self._tensordict.set(
                        key, value, inplace=inplace, non_blocking=non_blocking
                    )
                    return self
                elif type_hints is None:
                    warnings.warn(type(self)._set_dict_warn_msg)
            elif value is not None and issubclass(
                target_cls, tuple(tensordict_lib.base._ACCEPTED_CLASSES)
            ):
                try:
                    if not issubclass(type(value), target_cls):
                        if issubclass(target_cls, torch.Tensor):
                            # first convert to tensor to make sure that the dtype is preserved
                            value = torch.as_tensor(value)
                        cast_val = _cast_funcs[target_cls](value)
                    else:
                        cast_val = value
                except TypeError:
                    raise TypeError(
                        f"Failed to cast the value {key} to the type annotation {target_cls}."
                    )
                return set_tensor(value=cast_val)
            elif value is not None and target_cls is not _AnyType:
                value = _cast_funcs[target_cls](value)
            elif target_cls is _AnyType and _is_castable(type(value)):
                return set_tensor()
        else:
            if isinstance(value, tuple(tensordict_lib.base._ACCEPTED_CLASSES)):
                return set_tensor()

        # Avoiding key clash, honoring the user input to assign non-tensor data to the key
        if key in self._tensordict.keys():
            if inplace:
                raise RuntimeError(
                    f"Cannot update an existing entry of type {type(self._tensordict.get(key))} with a value of type {type(value)}."
                )
            self._tensordict.del_(key)
        # Saving all non-tensor attributes
        self._non_tensordict[key] = value
        return self

    if isinstance(key, tuple) and len(key):
        key = _unravel_key_to_tuple(key)
        if len(key) > 1:
            return self.set(key[0], getattr(self, key[0]).set(key[1:], value))
        out = self.set(key[0], value)
        return out
    raise ValueError(
        f"Supported type for key are str and tuple, got {key} of type {type(key)}"
    )


def _del_(self, key):
    key = _unravel_key_to_tuple(key)
    if len(key) > 1:
        td = self.get(key[0])
        td.del_(key[1:])
        return
    if key[0] in self._tensordict.keys():
        self._tensordict.del_(key[0])
        # self.set(key[0], None)
    elif key[0] in self._non_tensordict.keys():
        self._non_tensordict[key[0]] = None
    else:
        raise KeyError(f"Key {key} could not be found in tensorclass {self}.")
    return


def _set_at_(
    self, key: NestedKey, value: Any, idx: IndexType, non_blocking: bool = False
):
    if key in self._non_tensordict:
        del self._non_tensordict[key]
    return self._tensordict.set_at_(key, value, idx, non_blocking=non_blocking)


def _get(self, key: NestedKey, default: Any = NO_DEFAULT):
    """Gets the value stored with the input key.

    Args:
        key (str, tuple of str): key to be queried. If tuple of str it is
            equivalent to chained calls of getattr.
        default: default value if the key is not found in the tensorclass.

    Returns:
        value stored with the input key

    """
    if isinstance(key, str):
        key = (key,)

    if isinstance(key, tuple):
        try:
            if len(key) > 1:
                return getattr(self, key[0]).get(key[1:])
            return getattr(self, key[0])
        except AttributeError:
            if default is NO_DEFAULT:
                raise
            return default
    raise ValueError(f"Supported type for key are str and tuple, got {type(key)}")


def _get_at(self, key: NestedKey, idx, default: Any = NO_DEFAULT):
    try:
        return self.get(key, NO_DEFAULT)[idx]
    except AttributeError:
        if default is NO_DEFAULT:
            raise
        return default


def _batch_size(self) -> torch.Size:
    """Retrieves the batch size for the tensor class.

    Returns:
        batch size (torch.Size)

    """
    return self._tensordict.batch_size


def _batch_size_setter(self, new_size: torch.Size) -> None:  # noqa: D417
    """Set the value of batch_size.

    Args:
        new_size (torch.Size): new_batch size to be set

    """
    self._tensordict._batch_size_setter(new_size)


def _names(self) -> torch.Size:
    """Retrieves the dim names for the tensor class.

    Returns:
        names (list of str)

    """
    return self._tensordict.names


def _names_setter(self, names: str) -> None:  # noqa: D417
    """Set the value of ``tensorclass.names``.

    Args:
        names (sequence of str)

    """
    self._tensordict.names = names


def _state_dict(
    self, destination=None, prefix="", keep_vars=False, flatten=False
) -> dict[str, Any]:
    """Returns a state_dict dictionary that can be used to save and load data from a tensorclass."""
    state_dict = {
        "_tensordict": super(type(self), self)
        .__getattribute__("_tensordict")
        .state_dict(
            destination=destination, prefix=prefix, keep_vars=keep_vars, flatten=flatten
        )
    }
    state_dict["_non_tensordict"] = copy(self._non_tensordict)
    return state_dict


def _load_state_dict(
    self, state_dict: dict[str, Any], strict=True, assign=False, from_flatten=False
):
    """Loads a state_dict attemptedly in-place on the destination tensorclass."""
    for key, item in state_dict.items():
        # keys will never be nested which facilitates everything, but let's
        # double check in case someone does something nasty
        if not isinstance(key, str):
            raise TypeError("Only str keys are allowed when calling load_state_dict.")
        if key == "_non_tensordict":
            for sub_key, sub_item in item.items():
                # sub_item is the state dict of a tensorclass
                if isinstance(sub_item, dict) and "_non_tensordict" in sub_item:
                    raise RuntimeError(
                        "Loading a saved tensorclass on a uninitialized tensorclass is not allowed"
                    )
                else:
                    # check that sub_key is part of the tensorclass
                    if sub_key not in self.__class__.__dataclass_fields__:
                        raise KeyError(
                            f"Key '{sub_key}' wasn't expected in the state-dict."
                        )
                    super(type(self), self).__getattribute__("_non_tensordict")[
                        sub_key
                    ] = sub_item
        elif key == "_tensordict":
            for sub_key in item.keys():
                if (
                    sub_key not in self.__class__.__dataclass_fields__
                    and sub_key not in ("__batch_size", "__device")
                ):
                    raise KeyError(
                        f"Key '{sub_key}' wasn't expected in the state-dict."
                    )
            super(type(self), self).__getattribute__("_tensordict").load_state_dict(
                item, strict=strict, assign=assign, from_flatten=from_flatten
            )
        else:
            raise KeyError(f"Key '{key}' wasn't expected in the state-dict.")

    return self


def _eq(self, other: object) -> bool:
    """Compares the Tensor class object to another object for equality. However, the equality check for non-tensor data is not performed.

    Args:
        other: object to compare to this object. Can be a tensorclass, a
            tensordict or any compatible type (int, float or tensor), in
            which case the equality check will be propagated to the leaves.

    Returns:
        False if the objects are of different class types, Tensorclass of boolean
        values for tensor attributes and None for non-tensor attributes

    Examples:
        >>> @tensorclass
        ... class MyClass:
        ...     x: Tensor
        ...     y: "MyClass"
        ...     z: str
        ...
        >>> c1 = MyClass(
        ...     x=torch.randn(3, 4),
        ...     y=MyClass(
        ...         x=torch.randn(3, 4, 1),
        ...         y=None,
        ...         z="bar",
        ...         batch_size=[3, 4, 1],
        ...     ),
        ...     z="foo",
        ...     batch_size=[3, 4],
        ... )
        >>> c2 = c1.clone()
        >>> print(c1 == c2)
        MyClass(
            x=Tensor(shape=torch.Size([3, 4]), device=cpu, dtype=torch.bool, is_shared=False),
            y=MyClass(
                x=Tensor(shape=torch.Size([3, 4, 1]), device=cpu, dtype=torch.bool, is_shared=False),
                y=None,
                z=None,
                batch_size=torch.Size([3, 4, 1]),
                device=None,
                is_shared=False),
            z=None,
            batch_size=torch.Size([3, 4]),
            device=None,
            is_shared=False)
        >>> assert (c1 == c2).all()
        >>> assert (c1[:2] == c2[:2]).all()
        >>> assert not (c1 == c2.apply(lambda x: x+1)).all()

    """
    if not is_tensor_collection(other) and not isinstance(
        other, (dict, numbers.Number, Tensor)
    ):
        return False
    if is_tensorclass(other):
        tensor = self._tensordict == other._tensordict
    elif _is_tensor_collection(type(other)):
        # other can be a tensordict reconstruction of self, in which case we discard
        # the non-tensor data
        tensor = self.filter_non_tensor_data() == other.filter_non_tensor_data()
    else:
        tensor = self._tensordict == other
    return _from_tensordict_with_none(self, tensor)


def _ne(self, other: object) -> bool:
    """Compare the Tensor class object to another object for inequality. However, the equality check for non-tensor data is not performed.

    Args:
        other: object to compare to this object

    Returns:
        False if the objects are of different class types, Tensorclass of boolean values for tensor attributes and None for non-tensor attributes

    Examples:
        >>> @tensorclass
        ... class MyClass:
        ...     x: Tensor
        ...     y: "MyClass"
        ...     z: str
        ...
        >>> c1 = MyClass(
        ...     x=torch.randn(3, 4),
        ...     y=MyClass(
        ...         x=torch.randn(3, 4, 1),
        ...         y=None,
        ...         z="bar",
        ...         batch_size=[3, 4, 1],
        ...     ),
        ...     z="foo",
        ...     batch_size=[3, 4],
        ... )
        >>> c2 = c1.clone()
        >>> print(c1 != c2)
        MyClass(
            x=Tensor(shape=torch.Size([3, 4]), device=cpu, dtype=torch.bool, is_shared=False),
            y=MyClass(
                x=Tensor(shape=torch.Size([3, 4, 1]), device=cpu, dtype=torch.bool, is_shared=False),
                y=None,
                z=None,
                batch_size=torch.Size([3, 4, 1]),
                device=None,
                is_shared=False),
            z=None,
            batch_size=torch.Size([3, 4]),
            device=None,
            is_shared=False)
        >>> c2 = c2.apply(lambda x: x+1)
        >>> assert (c1 != c2).all()

    """
    if not is_tensor_collection(other) and not isinstance(
        other, (dict, numbers.Number, Tensor)
    ):
        return True
    if is_tensorclass(other):
        tensor = self._tensordict != other._tensordict
    elif _is_tensor_collection(type(other)):
        # other can be a tensordict reconstruction of self, in which case we discard
        # the non-tensor data
        tensor = self._tensordict != other.exclude(*self._non_tensordict.keys())
    else:
        tensor = self._tensordict != other
    return _from_tensordict_with_none(self, tensor)


def _or(self, other: object) -> bool:
    """Compares the Tensor class object to another object for logical OR. However, the logical OR check for non-tensor data is not performed.

    Args:
        other: object to compare to this object. Can be a tensorclass, a
            tensordict or any compatible type (int, float or tensor), in
            which case the equality check will be propagated to the leaves.

    Returns:
        False if the objects are of different class types, Tensorclass of boolean
        values for tensor attributes and None for non-tensor attributes

    """
    if not is_tensor_collection(other) and not isinstance(
        other, (dict, numbers.Number, Tensor)
    ):
        return False
    if is_tensorclass(other):
        tensor = self._tensordict | other._tensordict
    elif _is_tensor_collection(type(other)):
        # other can be a tensordict reconstruction of self, in which case we discard
        # the non-tensor data
        tensor = self._tensordict | other.exclude(*self._non_tensordict.keys())
    else:
        tensor = self._tensordict | other
    return _from_tensordict_with_none(self, tensor)


def _xor(self, other: object) -> bool:
    """Compares the Tensor class object to another object for exclusive OR. However, the exclusive OR check for non-tensor data is not performed.

    Args:
        other: object to compare to this object. Can be a tensorclass, a
            tensordict or any compatible type (int, float or tensor), in
            which case the equality check will be propagated to the leaves.

    Returns:
        False if the objects are of different class types, Tensorclass of boolean
        values for tensor attributes and None for non-tensor attributes

    """
    if not is_tensor_collection(other) and not isinstance(
        other, (dict, numbers.Number, Tensor)
    ):
        return False
    if is_tensorclass(other):
        tensor = self._tensordict ^ other._tensordict
    elif _is_tensor_collection(type(other)):
        # other can be a tensordict reconstruction of self, in which case we discard
        # the non-tensor data
        tensor = self._tensordict ^ other.exclude(*self._non_tensordict.keys())
    else:
        tensor = self._tensordict ^ other
    return _from_tensordict_with_none(self, tensor)


def _non_tensor_items(self, include_nested=False):
    if include_nested:
        return self.non_tensor_items() + self._tensordict.non_tensor_items(
            include_nested=True
        )
    else:
        return list(self._non_tensordict.items())


def _bool(self):
    raise RuntimeError("Converting a tensorclass to boolean value is not permitted")


def _single_td_field_as_str(key, item, tensordict):
    """Returns a string as a  key-value pair of tensordict.

    Args:
        key (str): key of tensor dict item
        item (tensor type): value to be returned for key
        tensordict (Tensordict): Tensordict object

    Returns:
        String representation of a key-value pair

    """
    if is_tensor_collection(type(item)):
        return f"{key}={repr(tensordict[key])}"
    return f"{key}={_get_repr(item)}"


def _all_td_fields_as_str(td: TensorDictBase) -> str:
    """Returns indented representation of tensor dict values as a key-value pairs.

    Args:
        td (TensorDict) : Tensordict object

    Returns:
        String representation of all tensor data

    """
    return indent(
        ",\n".join(
            sorted([_single_td_field_as_str(key, item, td) for key, item in td.items()])
        ),
        4 * " ",
    )


def _all_non_td_fields_as_str(src_dict) -> list:
    """Returns a list of string representation of non-tensor key-value pairs.

    Args:
        src_dict (dict): non_tensor_dict

    Returns:
        result (list): list of strings with key-value representation

    """
    result = []
    for key, val in src_dict.items():
        if not is_tensor_collection(val):
            result.append(f"{key}={repr(val)}")

    return result


def _unbind(self, dim: int):
    """Returns a tuple of indexed tensorclass instances unbound along the indicated dimension.

    Resulting tensorclass instances will share the storage of the initial tensorclass instance.

    """
    return tuple(
        self._from_tensordict(td, non_tensordict=copy(self._non_tensordict))
        for td in self._tensordict.unbind(dim)
    )


################
# Custom classes
# --------------

NONTENSOR_HANDLED_FUNCTIONS = []

_MP_MANAGER = None


def _mp_manager():
    global _MP_MANAGER
    if _MP_MANAGER is None:
        _MP_MANAGER = Manager()
    return _MP_MANAGER


@tensorclass
class NonTensorData:
    """A carrier for non-tensordict data.

    This class can be used whenever non-tensor data needs to be carrier at
    any level of a tensordict instance.

    :class:`~tensordict.tensorclass.NonTensorData` instances can be created
    explicitely or using :meth:`~tensordict.TensorDictBase.set_non_tensor`.

    This class is serializable using :meth:`tensordict.TensorDictBase.memmap`
    and related methods, and can be loaded through :meth:`~tensordict.TensorDictBase.load_memmap`.
    If the content of the object is JSON-serializable, it will be serializsed in
    the `meta.json` file in the directory pointed by the parent key of the `NoneTensorData`
    object. If it isn't, serialization will fall back on pickle. This implies
    that we assume that the content of this class is either json-serializable or
    pickable, and it is the user responsibility to make sure that one of these
    holds. We try to avoid pickling/unpickling objects for performance and security
    reasons (as pickle can execute arbitrary code during loading).

    .. note:: if the data passed to :class:`NonTensorData` is a :class:`NonTensorData`
        itself, the data from the nested object will be gathered.

        >>> non_tensor = NonTensorData("a string!")
        >>> non_tensor = NonTensorData(non_tensor)
        >>> assert non_tensor.data == "a string!"

    .. note:: To faciliate ``NonTensorData`` integration in tensordict, the
        :meth:`~tensordict.TensorDictBase.__getitem__` and :meth:`~tensordict.TensorDictBase.__setitem__`
        are overloaded to set non-tensor data appropriately (unlike :meth:`~tensordict.TensorDictBase.set`
        and :meth:`~tensordict.TensorDictBase.get` which are reserved for tensor-like
        objects):

        >>> td = TensorDict({"a": torch.zeros(3)}, batch_size=[3])
        >>> td["a"]  # gets a tensor
        >>> td["b"] = "a string!"
        >>> assert td["b"] == "a string!"
        >>> # indexing preserves the meta-data
        >>> assert td[0]["b"] == "a string!"
        >>> td.get("b")  # returns the NonTensorData

    .. note:: Unlike other tensorclass classes, :class:`NonTensorData` supports
        comparisons of two non-tensor data through :meth:`~.__eq__`, :meth:`~.__ne__`,
        :meth:`~.__xor__` or :meth:`~.__or__`. These operations return a tensor
        of shape `batch_size`. For compatibility with `<a tensordict> == <float_number>`,
        comparison with non-:class:`NonTensorData` will always return an empty
        :class:`NonTensorData`.

        >>> a = NonTensorData(True, batch_size=[])
        >>> b = NonTensorData(True, batch_size=[])
        >>> assert a == b
        >>> assert not (a != b)
        >>> assert not (a ^ b)
        >>> assert a | b
        >>> # The output is a tensor of shape batch-size
        >>> a = NonTensorData(True, batch_size=[3])
        >>> b = NonTensorData(True, batch_size=[3])
        >>> print(a == b)
        tensor([True, True, True])

    .. note:: Stacking :class:`NonTensorData` instances results in either
        a single :class:`NonTensorData` instance if all shapes match, or a
        :class:`~tensordict.LazyStackedTensorDict` object if the content
        mismatch. To get to this result, the content of the :class:`NonTensorData`
        instances must be compared, which can be computationally intensive
        depending on what this content is.

        >>> data = torch.stack([NonTensorData(1, batch_size=[]) for _ in range(10)])
        >>> data
        NonTensorData(
            data=1,
            batch_size=torch.Size([10]),
            device=None,
            is_shared=False)
        >>> data = torch.stack([NonTensorData(i, batch_size=[3,]) for i in range(10)], 1)
        >>> data[:, 0]
        NonTensorData(
            data=0,
            batch_size=torch.Size([3]),
            device=None,
            is_shared=False)

    .. note:: Non-tensor data can be filtered out from a tensordict using
        :meth:`~tensordict.TensorDictBase.filter_non_tensor`.

    Examples:
        >>> # create an instance explicitly
        >>> non_tensor = NonTensorData("a string!", batch_size=[]) # batch-size can be anything
        >>> data = TensorDict({}, batch_size=[3])
        >>> data.set_non_tensor(("nested", "key"), "a string!")
        >>> assert isinstance(data.get(("nested", "key")), NonTensorData)
        >>> assert data.get_non_tensor(("nested", "key")) == "a string!"
        >>> # serialization
        >>> class MyPickableClass:
        ...     value = 10
        >>> data.set_non_tensor("pickable", MyPickableClass())
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmpdir:
        ...     data.memmap(tmpdir)
        ...     loaded = TensorDict.load_memmap(tmpdir)
        ...     # print directory path
        ...     print_directory_tree(tmpdir)
        Directory size: 511.00 B
        tmp2cso9og_/
            pickable/
                _tensordict/
                    meta.json
                other.pickle
                meta.json
            nested/
                key/
                    _tensordict/
                        meta.json
                    meta.json
                meta.json
            meta.json
        >>> assert loaded.get_non_tensor("pickable").value == 10

    .. note:: __Preallocation__ is also possible with ``NonTensorData``.
      This class can handle conversion from ``NonTensorData`` to
      ``NonTensorStack`` where appropriate, as the following example
      demonstrates:

        >>> td = TensorDict({"val": NonTensorData(data=0, batch_size=[10])}, [10])
        >>> print(td)
        TensorDict(
            fields={
                val: NonTensorData(
                    data=0,
                    _metadata=None,
                    _is_non_tensor=True,
                    batch_size=torch.Size([10]),
                    device=None,
                    is_shared=False)},
            batch_size=torch.Size([10]),
            device=None,
            is_shared=False)
        >>> print(td["val"])
        0
        >>> newdata = TensorDict({"val": NonTensorData(data=1, batch_size=[5])}, [5])
        >>> td[1::2] = newdata
        >>> print(td)
        TensorDict(
            fields={
                val: NonTensorStack(
                    [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
                    batch_size=torch.Size([10]),
                    device=None)},
            batch_size=torch.Size([10]),
            device=None,
            is_shared=False)
        >>> print(td["val"])  # the stack is automatically converted to a list
        [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]

      If the value is unique, the ``NonTensorData`` container is kept and
      retrieving the value only returns this value. If a ``NonTensorStack``
      is used, ``__getitem__`` will return the list of values instead.
      This makes the two operations not exactly interchangeable. The reason
      for this inconsistency is that a single ``NonTensorData`` with a non-empty
      batch-size is intended to be used as a metadata carrier for bigger
      tensordicts, whereas ``NonTensorStack`` usage is aimed at allocating
      one metadata atom to each corresponding batch element.

    .. note::
      ``NonTensorData`` can be shared between processes. In fact, both
      :meth:`~tensordict.TensorDict.memmap_` (and the likes) and
      :meth:`~tensordict.TensorDict.share_memory_` will produce sharable
      instances.

      Valid methods to write data are :meth:`~tensordict.TensorDictBase.update`
      with the `inplace=True` flag and :meth:`~tensordict.TensorDictBase.update_`
      or :meth:`~tensordict.TensorDictBase.update_at_`.

        >>> if __name__ == "__main__":
        ...     td = TensorDict({"val": NonTensorData(data=0, batch_size=[])}, [])
        ...     td.share_memory_()
        ...     td.update_(TensorDict({"val": NonTensorData(data=1, batch_size=[])}, []))  # works
        ...     td.update(TensorDict({"val": NonTensorData(data=1, batch_size=[])}, []), inplace=True)  # works
        ...     td["val"] = 1  # breaks

      A shared ``NonTensorData`` is writable whenever its content is a ``str``,
      ``int``, ``float``, ``bool``, ``dict`` or ``list`` instance. Other types
      (e.g., dataclasses) will not raise an exception during the call to
      ``memmap_`` or ``share_memory_`` but they will cause the code to break
      when the data is overwritten.

        >>> @dataclass
        ... class MyClass:
        ...     string: str
        ...
        >>> if __name__ == "__main__":
        ...     td = TensorDict({"val": MyClass("a string!")}, [])
        ...     td.share_memory_()  # works and can be shared between processes
        ...     td.update_(TensorDict({"val": MyClass("another string!")}, []))  # breaks!

      :class:`~tensordict.tensorclass.TensorStack` instances are also sharable
      in a similar way. Crucially, preallocation must be properly handled for
      this to work.

        >>> td = TensorDict({"val": NonTensorData(data=0, batch_size=[10])}, [10])
        >>> newdata = TensorDict({"val": NonTensorData(data=1, batch_size=[5])}, [5])
        >>> td[1::2] = newdata
        >>> # If TD is properly preallocated, we can share it and change its content
        >>> td.share_memory_()
        >>> newdata = TensorDict({"val": NonTensorData(data=2, batch_size=[5])}, [5])
        >>> td[1::2] = newdata  # Works!
        >>> # In contrast, not preallocating the tensordict properly will break when assigning values
        >>> td = TensorDict({"val": NonTensorData(data=0, batch_size=[10])}, [10])
        >>> td.share_memory_()
        >>> newdata = TensorDict({"val": NonTensorData(data=2, batch_size=[5])}, [5])
        >>> td[1::2] = newdata  # breaks!

      Writable memmapped-``NonTensorData`` instances will update the underlying
      metadata if required. This involves writing in a JSON file, which can
      introduce some overhead. We advise against this usage whenever one seeks
      performance and long-lasting data sharing isn't required (``share_memory_``
      should be preferred in these cases).

        >>> if __name__ == "__main__":
        ...     td = TensorDict({"val": NonTensorData(data=0, batch_size=[])}, [])
        ...     td.memmap_(dest_folder)
        ...     td.update_(TensorDict({"val": NonTensorData(data=1, batch_size=[])}, []))
        ...     # The underlying metadata on disk is updated during calls to update_
        ...     td_load = TensorDict.load_memmap(dest_folder)
        ...     assert (td == td_load).all()

    """

    # Used to carry non-tensor data in a tensordict.
    # The advantage of storing this in a tensorclass is that we don't need
    # to patch tensordict with additional checks that will encur unwanted overhead
    # and all the overhead falls back on this class.
    data: Any
    _metadata: dict | None = None

    _is_non_tensor: bool = True

    def __post_init__(self):
        if is_non_tensor(self.data):
            data = getattr(self.data, "data", None)
            if data is None:
                data = self.data.tolist()
            del self._tensordict["data"]
            self._non_tensordict["data"] = data
        assert self._tensordict.is_empty(), self._tensordict

        def __repr__(self):
            data_str = str(self.data)
            if len(data_str) > 200:
                data_str = data_str[:20] + "  ...  " + data_str[-20:]
            return f"{type(self).__name__}(data={data_str}, batch_size={self.batch_size}, device={self.device})"

        self.__class__.__repr__ = __repr__

        old_eq = self.__class__.__eq__
        if old_eq is _eq:
            global NONTENSOR_HANDLED_FUNCTIONS
            NONTENSOR_HANDLED_FUNCTIONS.extend(TD_HANDLED_FUNCTIONS)

            # Patch only the first time a class is created

            @functools.wraps(_eq)
            def __eq__(self, other):
                if isinstance(other, NonTensorData):
                    return torch.full(
                        self.batch_size, self.data == other.data, device=self.device
                    )
                return old_eq(self, other)

            self.__class__.__eq__ = __eq__

            _ne = self.__class__.__ne__

            @functools.wraps(_ne)
            def __ne__(self, other):
                if isinstance(other, NonTensorData):
                    return torch.full(
                        self.batch_size, self.data != other.data, device=self.device
                    )
                return _ne(self, other)

            self.__class__.__ne__ = __ne__

            _xor = self.__class__.__xor__

            @functools.wraps(_xor)
            def __xor__(self, other):
                if isinstance(other, NonTensorData):
                    return torch.full(
                        self.batch_size, self.data ^ other.data, device=self.device
                    )
                return _xor(self, other)

            self.__class__.__xor__ = __xor__

            _or = self.__class__.__or__

            @functools.wraps(_or)
            def __or__(self, other):
                if isinstance(other, NonTensorData):
                    return torch.full(
                        self.batch_size, self.data | other.data, device=self.device
                    )
                return _or(self, other)

            self.__class__.__or__ = __or__

    def update(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        inplace: bool = False,
        *,
        non_blocking: bool = False,
        keys_to_update: Sequence[NestedKey] | None = None,
    ) -> T:
        return self._update(
            input_dict_or_td=input_dict_or_td,
            clone=clone,
            inplace=inplace,
            keys_to_update=keys_to_update,
        )

    def _update(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        inplace: bool = False,
        *,
        keys_to_update: Sequence[NestedKey] | None = None,
        break_on_memmap: bool = None,
    ) -> T:
        if isinstance(input_dict_or_td, NonTensorData):
            data = input_dict_or_td.data
            if inplace and self._tensordict._is_shared:
                _update_shared_nontensor(self._non_tensordict["data"], data)
                return self
            elif inplace and self._is_memmap:
                _is_memmaped_from_above = self._is_memmaped_from_above()
                if break_on_memmap is None:
                    global _BREAK_ON_MEMMAP
                    break_on_memmap = _BREAK_ON_MEMMAP
                if _is_memmaped_from_above and break_on_memmap:
                    raise RuntimeError(
                        "Cannot update a leaf NonTensorData from a memmaped parent NonTensorStack. "
                        "To update this leaf node, please update the NonTensorStack with the proper index."
                    )
                share_non_tensor = self._metadata["_share_non_tensor"]
                if share_non_tensor:
                    _update_shared_nontensor(self._non_tensordict["data"], data)
                else:
                    self._non_tensordict["data"] = data
                # Force json update by setting is memmap to False
                if not _is_memmaped_from_above and "memmap_prefix" in self._metadata:
                    self._tensordict._is_memmap = False
                    self._memmap_(
                        prefix=self._metadata["memmap_prefix"],
                        copy_existing=False,
                        executor=None,
                        futures=None,
                        inplace=True,
                        like=False,
                        share_non_tensor=share_non_tensor,
                    )
                return self
            elif not inplace and self.is_locked:
                raise RuntimeError(_LOCK_ERROR)
            if clone:
                data = deepcopy(data)
            self.data = data
        elif isinstance(input_dict_or_td, NonTensorStack):
            raise ValueError(
                "Cannot update a NonTensorData object with a NonTensorStack. Call `non_tensor_data.maybe_to_stack()` "
                "before calling update()."
            )
        elif not input_dict_or_td.is_empty():
            raise RuntimeError(f"Unexpected type {type(input_dict_or_td)}")
        return self

    def maybe_to_stack(self):
        """Converts the NonTensorData object to a NonTensorStack object if it has a non-empty batch-size."""
        datalist = self.data
        if not self.batch_size:
            return self
        for i in reversed(self.batch_size):
            datalist = [datalist] * i
        return NonTensorStack._from_list(datalist, device=self.device)

    def update_(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        *,
        non_blocking: bool = False,
        keys_to_update: Sequence[NestedKey] | None = None,
    ) -> T:
        return self._update_(
            input_dict_or_td=input_dict_or_td,
            clone=clone,
            keys_to_update=keys_to_update,
        )

    def _update_(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        *,
        keys_to_update: Sequence[NestedKey] | None = None,
        break_on_memmap: bool = None,
    ) -> T:

        if isinstance(input_dict_or_td, NonTensorStack):
            raise RuntimeError(
                "Cannot update a NonTensorData with a NonTensorStack object."
            )
        if not isinstance(input_dict_or_td, NonTensorData):
            raise RuntimeError(
                "NonTensorData.copy_ / update_ requires the source to be a NonTensorData object."
            )
        return self._update(
            input_dict_or_td,
            inplace=True,
            clone=clone,
            keys_to_update=keys_to_update,
            break_on_memmap=break_on_memmap,
        )

    def update_at_(
        self,
        input_dict_or_td: dict[str, CompatibleType] | TensorDictBase,
        index: IndexType,
        clone: bool = False,
        *,
        non_blocking: bool = False,
    ) -> NonTensorData:
        if index != () and index != slice(None):
            raise RuntimeError("Cannot update a part of a NonTensorData.")
        return self.update_(
            input_dict_or_td=input_dict_or_td, clone=clone, non_blocking=non_blocking
        )

    def empty(self, recurse=False, *, device=NO_DEFAULT, batch_size=None, names=None):
        if batch_size is not None and names is None:
            names = None
        elif names is None and self._has_names():
            names = self.names
        else:
            names = None
        return NonTensorData(
            data=self.data,
            batch_size=self.batch_size if batch_size is None else batch_size,
            names=names,
            device=self.device if device is NO_DEFAULT else device,
        )

    def _apply_nest(self, *args, out=None, **kwargs):
        # kwargs["filter_empty"] = False
        if out is not None:
            return out
        return self.empty(
            batch_size=kwargs.get("batch_size"),
            device=kwargs.get("device", NO_DEFAULT),
            names=kwargs.get("names"),
        )

    def to_dict(self):
        # override to_dict to return just the data
        return self.data

    def to_tensordict(self):
        return self

    @classmethod
    def _stack_non_tensor(cls, list_of_non_tensor, dim=0):
        # checks have been performed previously, so we're sure the list is non-empty
        first = list_of_non_tensor[0]

        def _check_equal(a, b):
            if isinstance(a, _ACCEPTED_CLASSES) or isinstance(b, _ACCEPTED_CLASSES):
                return (a == b).all()
            if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
                return (a == b).all()
            try:
                iseq = a == b
            except Exception:
                iseq = False
            return iseq

        if all(isinstance(data, NonTensorData) for data in list_of_non_tensor) and all(
            _check_equal(data.data, first.data) for data in list_of_non_tensor[1:]
        ):
            batch_size = list(first.batch_size)
            batch_size.insert(dim, len(list_of_non_tensor))
            return NonTensorData(
                data=first.data,
                batch_size=batch_size,
                names=first.names if first._has_names() else None,
                device=first.device,
            )

        return NonTensorStack(*list_of_non_tensor, stack_dim=dim)

    @classmethod
    def __torch_function__(
        cls,
        func: Callable,
        types: tuple[type, ...],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Callable:
        # A modified version of __torch_function__ to account for the different behaviour
        # of stack, which should return lazy stacks of data of data does not match.
        if func not in _TD_PASS_THROUGH or not all(
            issubclass(t, (Tensor, cls)) for t in types
        ):
            return NotImplemented

        escape_conversion = func in (torch.stack,)

        if kwargs is None:
            kwargs = {}

        # get the output type from the arguments / keyword arguments
        if len(args) > 0:
            tensorclass_instance = args[0]
        else:
            tensorclass_instance = kwargs.get("input", kwargs["tensors"])
        if isinstance(tensorclass_instance, (tuple, list)):
            tensorclass_instance = tensorclass_instance[0]
        if not escape_conversion:
            args = tuple(_arg_to_tensordict(arg) for arg in args)
            kwargs = {key: _arg_to_tensordict(value) for key, value in kwargs.items()}

        result = TD_HANDLED_FUNCTIONS[func](*args, **kwargs)
        if isinstance(result, (list, tuple)):
            return result.__class__(
                _from_tensordict_with_copy(tensorclass_instance, tensordict_result)
                for tensordict_result in result
            )
        if not escape_conversion:
            return _from_tensordict_with_copy(tensorclass_instance, result)
        return result

    def _fast_apply(self, *args, **kwargs):
        kwargs["filter_empty"] = False
        return _wrap_method(self, "_fast_apply", self._tensordict._fast_apply)(
            *args, **kwargs
        )

    def tolist(self):
        """Converts the data in a list if the batch-size is non-empty.

        If the batch-size is empty, returns the data.

        """
        if not self.batch_size:
            return self.data
        return [ntd.tolist() for ntd in self.unbind(0)]

    def copy_(self, src: NonTensorData | NonTensorStack, non_blocking: bool = False):
        return self.update_(src, non_blocking=non_blocking)

    def clone(self, recurse: bool = True):
        if recurse:
            return type(self)(
                data=deepcopy(self.data),
                batch_size=self.batch_size,
                device=self.device,
                names=self.names,
            )
        return type(self)(
            data=self.data,
            batch_size=self.batch_size,
            device=self.device,
            names=self.names,
        )

    def share_memory_(self):
        if self._tensordict._is_shared:
            return self
        with self.unlock_():
            self._non_tensordict["data"] = _share_memory_nontensor(
                self.data, manager=_mp_manager()
            )
        self._tensordict.share_memory_()
        return self

    def _memmap_(
        self,
        *,
        prefix: str | None = None,
        copy_existing: bool = False,
        executor=None,
        futures=None,
        inplace=True,
        like=False,
        memmaped: bool = False,
        share_non_tensor: bool = False,
    ):
        if self._tensordict._is_memmap:
            return self

        _metadata = {}
        if prefix is not None:
            _metadata = copy(self._metadata)
            if _metadata is None:
                _metadata = {}
            _metadata["memmap_prefix"] = prefix
            _metadata["memmaped"] = memmaped

        out = _memmap_(
            self,
            prefix=prefix,
            copy_existing=copy_existing,
            executor=executor,
            futures=futures,
            inplace=inplace,
            like=like,
            memmaped=memmaped,
            share_non_tensor=share_non_tensor,
        )
        _metadata["_share_non_tensor"] = share_non_tensor
        out._non_tensordict["_metadata"] = _metadata
        if share_non_tensor:
            out._non_tensordict["data"] = _share_memory_nontensor(
                out.data, manager=_mp_manager()
            )
        return out

    def _is_memmaped_from_above(self):
        _metadata = self._metadata
        if _metadata is None:
            return False
        return _metadata.get("memmaped", False)


# For __setitem__ and _update_at_ we don't pass a kwarg but use a global variable instead
_BREAK_ON_MEMMAP = True


class NonTensorStack(LazyStackedTensorDict):
    """A thin wrapper around LazyStackedTensorDict to make stack on non-tensor data easily recognizable.

    A ``NonTensorStack`` is returned whenever :func:`~torch.stack` is called on
    a list of :class:`~tensordict.NonTensorData` or ``NonTensorStack``.

    Examples:
        >>> from tensordict import NonTensorData
        >>> import torch
        >>> data = torch.stack([
        ...     torch.stack([NonTensorData(data=(i, j), batch_size=[]) for i in range(2)])
        ...    for j in range(3)])
        >>> print(data)
        NonTensorStack(
            [[(0, 0), (1, 0)], [(0, 1), (1, 1)], [(0, 2), (1, ...,
            batch_size=torch.Size([3, 2]),
            device=None)

    To obtain the values stored in a ``NonTensorStack``, call :class:`~.tolist`.

    """

    _is_non_tensor: bool = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not all(is_non_tensor(item) for item in self.tensordicts):
            raise RuntimeError("All tensordicts must be non-tensors.")

    def tolist(self):
        """Extracts the content of a :class:`tensordict.tensorclass.NonTensorStack` in a nested list.

        Examples:
            >>> from tensordict import NonTensorData
            >>> import torch
            >>> data = torch.stack([
            ...     torch.stack([NonTensorData(data=(i, j), batch_size=[]) for i in range(2)])
            ...    for j in range(3)])
            >>> data.tolist()
            [[(0, 0), (1, 0)], [(0, 1), (1, 1)], [(0, 2), (1, 2)]]

        """
        iterator = self.tensordicts if self.stack_dim == 0 else self.unbind(0)
        return [td.tolist() for td in iterator]

    @classmethod
    def from_nontensordata(cls, non_tensor: NonTensorData):
        data = non_tensor.data
        prev = NonTensorData(data, batch_size=[], device=non_tensor.device)
        for dim in reversed(non_tensor.shape):
            prev = cls(*[prev.clone(False) for _ in range(dim)], stack_dim=0)
        return prev

    def __repr__(self):
        selfrepr = str(self.tolist())
        if len(selfrepr) > 50:
            selfrepr = f"{selfrepr[:50]}..."
        selfrepr = indent(selfrepr, prefix=4 * " ")
        batch_size = indent(f"batch_size={self.batch_size}", prefix=4 * " ")
        device = indent(f"device={self.device}", prefix=4 * " ")
        return f"NonTensorStack(\n{selfrepr}," f"\n{batch_size}," f"\n{device})"

    @classmethod
    def lazy_stack(
        cls,
        items: Sequence[TensorDictBase],
        dim: int = 0,
        *,
        device: DeviceType | None = None,
        out: T | None = None,
        stack_dim_name: str | None = None,
    ) -> T:
        result = super().lazy_stack(
            items=items, dim=dim, out=out, stack_dim_name=stack_dim_name, device=device
        )
        if not isinstance(result, cls):
            raise RuntimeError(
                f"Unexpected result type: {type(result)} - expected one of {cls}."
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        return self.tolist()

    def to_tensordict(self):
        return self

    def _memmap_(
        self,
        *,
        prefix: str | None = None,
        copy_existing: bool = False,
        executor=None,
        futures=None,
        inplace=True,
        like=False,
        memmaped: bool = False,
        share_non_tensor: bool = False,
    ) -> T:

        memmaped_leaves = memmaped
        if not memmaped and prefix is not None:
            memmaped_leaves = True

            def save_metadata(prefix=prefix, self=self):
                data = self.tolist()
                device = str(self.device) if self.device is not None else None
                if not prefix.exists():
                    os.makedirs(prefix, exist_ok=True)
                jsondict = {
                    "_type": str(self.__class__),
                    "stack_dim": self.stack_dim,
                    "device": device,
                }
                if _is_json_serializable(data):
                    jsondict["data"] = data
                else:
                    jsondict["data"] = "pickle.pkl"
                    with open(prefix / "pickle.pkl", "wb") as f:
                        pickle.dump(data, f)
                with open(prefix / "meta.json", "w") as f:
                    json.dump(jsondict, f)

            if executor is None:
                save_metadata()
            else:
                futures.append(executor.submit(save_metadata))
        # The leaves are all non-tensor or non-tensor stacks, and we already saved this on disk
        # The only thing remaining to do is share the data between processes
        results = []
        for i, td in enumerate(self.tensordicts):
            results.append(
                td._memmap_(
                    prefix=(prefix / str(i)) if prefix is not None else None,
                    copy_existing=copy_existing,
                    executor=executor,
                    futures=futures,
                    inplace=inplace,
                    like=like,
                    # tell the nested stack / nontensor that
                    # no memmapping should be executed
                    memmaped=memmaped_leaves,
                    share_non_tensor=share_non_tensor,
                )
            )
        if not inplace:
            results = self.lazy_stack(results, dim=self.stack_dim)
        else:
            results = self
        if not memmaped and prefix is not None:
            results.__dict__["_path_to_memmap"] = prefix
        return results

    @classmethod
    def _load_memmap(
        cls, prefix: str, metadata: dict, *, out=None, **kwargs
    ) -> LazyStackedTensorDict:
        data = metadata.get("data", None)
        if data is not None:
            if isinstance(data, str):
                with open(prefix / data, "rb") as file:
                    data = pickle.load(file)
            device = metadata["device"]
            if device is not None:
                device = torch.device(device)
            return cls._from_list(data, device=device)
        return super()._load_memmap(prefix=prefix, metadata=metadata, **kwargs)

    @classmethod
    def _from_list(cls, datalist: List, device: torch.device):
        if all(isinstance(item, list) for item in datalist) and all(
            len(item) == len(datalist[0]) for item in datalist
        ):
            return NonTensorStack(
                *(cls._from_list(item, device=device) for item in datalist), stack_dim=0
            )
        return NonTensorStack(
            *(
                NonTensorData(data=item, device=device, batch_size=torch.Size([]))
                for item in datalist
            ),
            stack_dim=0,
        )

    def update(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        inplace: bool = False,
        *,
        non_blocking: bool = False,
        keys_to_update: Sequence[NestedKey] | None = None,
    ) -> T:
        return self._update(
            input_dict_or_td=input_dict_or_td,
            clone=clone,
            inplace=inplace,
            keys_to_update=keys_to_update,
        )

    def update_(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        *,
        non_blocking: bool = False,
        keys_to_update: Sequence[NestedKey] | None = None,
    ) -> T:
        return self._update(
            input_dict_or_td=input_dict_or_td,
            clone=clone,
            inplace=True,
            keys_to_update=keys_to_update,
        )

    def _update(
        self,
        input_dict_or_td: dict[str, CompatibleType] | T,
        clone: bool = False,
        inplace: bool = False,
        *,
        keys_to_update: Sequence[NestedKey] | None = None,
        break_on_memmap: bool = None,
        non_blocking: bool = False,
    ) -> T:
        if inplace and self.is_locked and not (self._is_shared or self._is_memmap):
            raise RuntimeError(_LOCK_ERROR)

        if isinstance(input_dict_or_td, NonTensorData):
            datalist = input_dict_or_td.data
            for d in reversed(self.batch_size):
                datalist = [datalist] * d
            reconstructed = self._from_list(datalist, device=self.device)
            return self.update(
                reconstructed,
                clone=clone,
                inplace=inplace,
                keys_to_update=keys_to_update,
            )

        memmap = False
        if self._is_memmap and hasattr(self, "_path_to_memmap"):
            if break_on_memmap is None:
                global _BREAK_ON_MEMMAP
                break_on_memmap = _BREAK_ON_MEMMAP
            if not break_on_memmap:
                raise RuntimeError(
                    "Calling _update with break_on_memmap=False is not permitted if the stack has a path."
                )
            # this is the only way break_on_memmap is False
            break_on_memmap = False
            # remove memmap
            if self._path_to_memmap.exists():
                shutil.rmtree(self._path_to_memmap)
            memmap = True

        # update content
        if isinstance(input_dict_or_td, NonTensorStack):
            for leaf_dest, leaf_src in zip(
                self.tensordicts, input_dict_or_td.unbind(self.stack_dim)
            ):
                leaf_dest._update(
                    leaf_src,
                    clone=clone,
                    inplace=inplace,
                    keys_to_update=keys_to_update,
                    break_on_memmap=break_on_memmap,
                )
            if memmap:
                self._memmap_(prefix=self._path_to_memmap, inplace=True)
        else:
            raise NotImplementedError(
                f"The data type {type(input_dict_or_td)} is not supported within {type(self).__name__}.update"
            )
        return self

    def __setitem__(self, index, value):
        memmap = False
        if self._is_memmap and hasattr(self, "_path_to_memmap"):
            global _BREAK_ON_MEMMAP
            _BREAK_ON_MEMMAP = False
            memmap = True
        try:
            super().__setitem__(index, value)
            if memmap:
                self._memmap_(prefix=self._path_to_memmap, inplace=True)
        finally:
            _BREAK_ON_MEMMAP = True

    def update_at_(
        self,
        input_dict_or_td: dict[str, CompatibleType] | TensorDictBase,
        index: IndexType,
        clone: bool = False,
        *,
        non_blocking: bool = False,
    ) -> T:
        memmap = False
        if self._is_memmap and hasattr(self, "_path_to_memmap"):
            global _BREAK_ON_MEMMAP
            _BREAK_ON_MEMMAP = False
            memmap = True
        try:
            super().update_at_(
                input_dict_or_td, index, clone=clone, non_blocking=non_blocking
            )
            if memmap:
                self._memmap_(prefix=self._path_to_memmap, inplace=True)
        finally:
            _BREAK_ON_MEMMAP = True
        return self

    @property
    def data(self):
        raise AttributeError


_register_tensor_class(NonTensorStack)


def _share_memory_nontensor(data, manager: Manager):
    if isinstance(data, int):
        return mp.Value(ctypes.c_int, data)
    if isinstance(data, float):
        return mp.Value(ctypes.c_double, data)
    if isinstance(data, bool):
        return mp.Value(ctypes.c_bool, data)
    if isinstance(data, bytes):
        return mp.Value(ctypes.c_byte, data)
    if isinstance(data, dict):
        result = manager.dict()
        result.update(data)
        return result
    if isinstance(data, str):
        result = mp.Array(ctypes.c_char, 100)
        data = data.encode("utf-8")
        result[: len(data)] = data
        return result
    if isinstance(data, list):
        result = manager.list()
        result.extend(data)
        return result
    # In all other cases, we just return the tensor. It's ok because the content
    # will be passed to the remote process using regular serialization. We will
    # lock the update in _update_shared_nontensor though.
    return data


def _from_shared_nontensor(nontensor):
    if isinstance(nontensor, multiprocessing.managers.ListProxy):
        return list(nontensor)
    if isinstance(nontensor, multiprocessing.managers.DictProxy):
        return dict(nontensor)
    if isinstance(nontensor, multiprocessing.sharedctypes.Synchronized):
        return nontensor.value
    if isinstance(nontensor, multiprocessing.sharedctypes.SynchronizedArray):
        byte_list = []
        for byte in nontensor:
            if byte == b"\x00":
                break
            byte_list.append(byte)
        return b"".join(byte_list).decode("utf-8")
    return nontensor


def _update_shared_nontensor(nontensor, val):
    if isinstance(nontensor, multiprocessing.managers.ListProxy):
        nontensor[:] = []
        nontensor.extend(val)
    elif isinstance(nontensor, multiprocessing.managers.DictProxy):
        nontensor.clear()
        nontensor.update(val)
    elif isinstance(nontensor, multiprocessing.sharedctypes.Synchronized):
        nontensor.value = val
    elif isinstance(nontensor, multiprocessing.sharedctypes.SynchronizedArray):
        val = val.encode("utf-8")
        for i, byte in enumerate(nontensor):
            if i < len(val):
                v = val[i]
                nontensor[i] = v
            elif byte == b"\x00":
                break
            else:
                nontensor[i] = b"\x00"
        # nontensor[0] = val.encode("utf-8")
    else:
        raise NotImplementedError(
            f"Updating {type(nontensor).__name__} within a shared/memmaped structure is not supported."
        )
