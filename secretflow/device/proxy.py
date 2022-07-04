# Copyright 2022 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import threading
from functools import wraps
from typing import Dict, Type

import jax
import ray

from . import link
from .device import PYU, Device, DeviceObject, PYUObject

_WRAPPABLE_DEVICE_OBJ: Dict[Type[DeviceObject], Type[Device]] = {PYUObject: PYU}

thread_local = threading.local()


def _method_wrapper(device_object_type, name, num_returns):
    def wrapper(self, *args, **kwargs):
        # device object type check and unwrap
        value_flat, value_tree = jax.tree_util.tree_flatten((args, kwargs))
        for i, value in enumerate(value_flat):
            if isinstance(value, DeviceObject):
                assert (
                    value.device == self.device
                ), f'unexpected device object {value.device} self {self.device}'
                value_flat[i] = value.data
        args, kwargs = jax.tree_util.tree_unflatten(value_tree, value_flat)

        # get ray actor handle
        handle = getattr(self.data, name)
        # TODO @raofei: 支持public_reveal装饰器
        res = handle.options(num_returns=num_returns).remote(*args, **kwargs)

        if num_returns == 1:
            return device_object_type(self.device, res)
        else:
            return [device_object_type(self.device, x) for x in res]

    return wrapper


def proxy(device_object_type: Type[DeviceObject], max_concurrency=None):
    """Define a device class which should accept DeviceObject as method parameters and return DeviceObject.

    This proxy function mainly does the following work:
    1. Add an additional parameter `device: Device` to init method `__init__`.
    2. Wrap class methods, allow passing DeviceObject as parameters, which
    must be on the same device as the class instance.
    3. According to the `return annotation` of class methods, return the
    corresponding number of DeviceObject.

    .. code:: python

        @proxy(PYUObject)
        class Model:
            def __init__(self, builder):
                self.weights = builder()

            def build_dataset(self, x, y):
                self.dataset_x = x
                self.dataset_y = y

            def get_weights(self) -> np.ndarray:
                return self.weights

            def train_step(self, step) -> Tuple[np.ndarray, int]:
                return self.weights, 100

        alice = PYU('alice')
        model = Model(builder, device=alice)
        x, y = alice(load_data)()
        model.build_dataset(x, y)
        w = model.get_weights()
        w, n = model.train_step(10)

    Args:
        device_object_type (Type[DeviceObject]): DeviceObject type, eg. PYUObject.
        max_concurrency (int): Actor threadpool size.

    Returns:
        Callable: Wrapper function.
    """
    assert (
        device_object_type in _WRAPPABLE_DEVICE_OBJ
    ), f'{device_object_type} is not allowed to be proxy'

    def make_proxy(cls):
        ActorClass = ray.remote(cls)

        class ActorProxy(device_object_type):
            def __init__(self, *args, **kwargs):
                assert 'device' in kwargs, (
                    f'missing device argument, please specify it with '
                    f'{cls.__name__}(*args, device=d, **kwargs)'
                )
                device = kwargs['device']
                expected_device_type = _WRAPPABLE_DEVICE_OBJ[device_object_type]
                assert isinstance(device, expected_device_type), (
                    f'unexpected device type, expected: '
                    f'{expected_device_type}, got {type(device)}'
                )

                if not issubclass(cls, link.Link):
                    del kwargs['device']

                data = ActorClass.options(
                    max_concurrency=max_concurrency, resources={device.party: 1}
                ).remote(*args, **kwargs)
                super().__init__(device, data)

        methods = inspect.getmembers(cls, inspect.isfunction)
        for name, method in methods:
            if name == '__init__':
                continue
            sig = inspect.signature(method)
            if sig.return_annotation is None or sig.return_annotation == sig.empty:
                num_returns = 1
            else:
                if (
                    hasattr(sig.return_annotation, '_name')
                    and sig.return_annotation._name == 'Tuple'
                ):
                    num_returns = len(sig.return_annotation.__args__)
                elif isinstance(sig.return_annotation, tuple):
                    num_returns = len(sig.return_annotation)
                else:
                    num_returns = 1
            wrapped_method = wraps(method)(
                _method_wrapper(device_object_type, name, num_returns)
            )
            setattr(ActorProxy, name, wrapped_method)

        name = f"ActorProxy({cls.__name__})"
        ActorProxy.__module__ = cls.__module__
        ActorProxy.__name__ = name
        ActorProxy.__qualname__ = name

        return ActorProxy

    return make_proxy