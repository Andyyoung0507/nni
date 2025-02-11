# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""
Operations that support weight sharing at a fine-grained level,
which is commonly known as super-kernel (as in channel search), or weight entanglement.
"""

import inspect
import itertools
from typing import Union, Tuple, Dict, List, Any, Type, Optional, TypeVar

import torch
import torch.nn as nn
import torch.nn.functional as F

import nni.retiarii.nn.pytorch as retiarii_nn
from nni.common.hpo_utils import ParameterSpec
from nni.common.serializer import is_traceable
from nni.retiarii.nn.pytorch.api import ValueChoiceX

from .base import BaseSuperNetModule
from ._valuechoice_utils import traverse_all_options, dedup_inner_choices
from ._operation_utils import Slicable as _S, MaybeWeighted as _W, int_or_int_dict, scalar_or_scalar_dict

T = TypeVar('T')


class MixedOperationSamplingPolicy:
    """
    Algo-related part for mixed Operation.

    :class:`MixedOperation` delegates its resample and export to this policy (or its subclass),
    so that one Operation can be easily combined with different kinds of sampling.

    One SamplingStrategy corresponds to one mixed operation.
    """

    def __init__(self, operation: 'MixedOperation', memo: Dict[str, Any], mutate_kwargs: Dict[str, Any]) -> None:
        """At init, the sampling policy can prepare basic parameters,
        and store them in operation if they need back propagation.

        This init is called in :meth:`BaseSuperNetModule.mutate`, after the mixed operation is created.
        So similar to :meth:`BaseSuperNetModule.mutate`,
        memo should also be managed (read and written) by the policy itself.
        """
        pass

    def resample(self, operation: 'MixedOperation', memo: Dict[str, Any] = None) -> Dict[str, Any]:
        """The handler of :meth:`MixedOperation.resample`."""
        raise NotImplementedError()

    def export(self, operation: 'MixedOperation', memo: Dict[str, Any] = None) -> Dict[str, Any]:
        """The handler of :meth:`MixedOperation.export`."""
        raise NotImplementedError()

    def forward_argument(self, operation: 'MixedOperation', name: str) -> Any:
        """Computing the argument with ``name`` used in operation's forward.
        Usually a value, or a distribution of value.
        """
        raise NotImplementedError()


class MixedOperation(BaseSuperNetModule):
    """This is the base class for all mixed operations.

    It contains commonly used utilities that will ease the effort to write customized mixed oeprations,
    i.e., operations with ValueChoice in its arguments.

    By design, for a mixed operation to work in a specific algorithm,
    at least two classes are needed.

    1. One class needs to inherit this class, to control operation-related behavior,
       such as how to initialize the operation such that the sampled operation can be its sub-operation.
    2. The other one needs to inherit :class:`MixedOperationSamplingPolicy`,
       which controls algo-related behavior, such as sampling.

    The two classes are linked with ``sampling_policy`` attribute in :class:`MixedOperation`,
    whose type is set via ``mixed_op_sampling`` in ``mutate_kwargs`` when
    :meth:`MixedOperation.mutate` is called.

    With this design, one mixed-operation (e.g., MixedConv2d) can work in multiple algorithms
    (e.g., both DARTS and ENAS), saving the engineering effort to rewrite all operations for
    each specific algo.

    This class should also define a ``bound_type``, to control the matching type in mutate,
    an ``argument_list``, to control which arguments can be dynamically used in ``forward``.
    This list will also be used in mutate for sanity check.
    """

    bound_type: Type[nn.Module]                 # defined in subclass
    argument_list: List[str]                    # defined in subclass

    sampling_policy: MixedOperationSamplingPolicy

    def super_init_argument(self, name: str, value_choice: ValueChoiceX) -> Any:
        """Get the initialization argument when constructing super-kernel, i.e., calling ``super().__init__()``.
        This is often related to specific operator, rather than algo.

        For example::

            def super_init_argument(self, name, value_choice):
                return max(value_choice.candidates)
        """
        raise NotImplementedError()

    def __post_init__(self) -> None:
        """Can be used to validate, or to do extra processing after calling ``__init__``."""
        pass

    def forward_with_args(self, *args, **kwargs):
        """To control real fprop. The accepted arguments are ``argument_list``,
        appended by forward arguments in the ``bound_type``."""
        raise NotImplementedError()

    def __init__(self, module_kwargs: Dict[str, Any]) -> None:
        # Concerned arguments
        self.mutable_arguments: Dict[str, ValueChoiceX] = {}
        # Useful when retrieving arguments without ValueChoice
        self.init_arguments: Dict[str, Any] = {**module_kwargs}
        self._fill_missing_init_arguments()

        # get init default
        super_init_kwargs = {}

        for key, value in module_kwargs.items():
            if isinstance(value, ValueChoiceX):
                if key not in self.argument_list:
                    raise TypeError(f'Unsupported value choice on argument of {self.bound_type}: {key}')
                super_init_kwargs[key] = self.super_init_argument(key, value)
                self.mutable_arguments[key] = value
            else:
                super_init_kwargs[key] = value

        # get all inner leaf value choices
        self._space_spec: Dict[str, ParameterSpec] = dedup_inner_choices(self.mutable_arguments.values())

        super().__init__(**super_init_kwargs)

        self.__post_init__()

    def resample(self, memo):
        """Delegates to :meth:`MixedOperationSamplingPolicy.resample`."""
        return self.sampling_policy.resample(self, memo)

    def export(self, memo):
        """Delegates to :meth:`MixedOperationSamplingPolicy.export`."""
        return self.sampling_policy.export(self, memo)

    def search_space_spec(self):
        return self._space_spec

    @classmethod
    def mutate(cls, module, name, memo, mutate_kwargs):
        """Find value choice in module's arguments and replace the whole module"""
        has_valuechoice = False
        if isinstance(module, cls.bound_type) and is_traceable(module):
            for arg in itertools.chain(module.trace_args, module.trace_kwargs.values()):
                if isinstance(arg, ValueChoiceX):
                    has_valuechoice = True

        if has_valuechoice:
            if module.trace_args:
                raise ValueError('ValueChoice on class arguments cannot appear together with ``trace_args``. '
                                    'Please enable ``kw_only`` on nni.trace.')

            # save type and kwargs
            mixed_op = cls(module.trace_kwargs)

            if 'mixed_op_sampling' not in mutate_kwargs:
                raise ValueError('Need to sampling policy of mixed op, but not found in `mutate_kwargs`.')
            policy_cls: Type[MixedOperationSamplingPolicy] = mutate_kwargs['mixed_op_sampling']
            # initialize policy class
            # this is put in mutate because we need to access memo
            mixed_op.sampling_policy = policy_cls(mixed_op, memo, mutate_kwargs)

            return mixed_op

    def forward_argument(self, name: str) -> Any:
        """Get the argument used in forward.
        This if often related to algo. We redirect this to sampling policy.
        """
        return self.sampling_policy.forward_argument(self, name)

    def forward(self, *args, **kwargs):
        """First get sampled arguments, then forward with the sampled arguments (by calling ``forward_with_args``)."""
        sampled_args = [self.forward_argument(name) for name in self.argument_list]
        return self.forward_with_args(*sampled_args, *args, **kwargs)

    def _fill_missing_init_arguments(self) -> None:
        """Set the unspecified init arguments in ``self.init_arguments``.
        For example, in the case of Conv2d, when user didn't specify argument ``stride``,
        this method adds ``stride = 1`` in ``self.init_arguments``.

        This is implemented by inspecting the init signature of ``bound_type``.
        Arguments in complex cases like ``__new__`` or in super-class is not supported.
        """

        def unwrap(cls):
            if not hasattr(cls, '__wrapped__'):
                return cls
            return unwrap(cls.__wrapped__)

        for param in inspect.signature(unwrap(self.bound_type).__init__).parameters.values():
            if param.default is not param.empty and param.name not in self.init_arguments:
                self.init_arguments[param.name] = param.default


class MixedLinear(MixedOperation, nn.Linear):
    """Mixed linear operation.

    Supported arguments are:

    - ``in_features``
    - ``out_features``

    Prefix of weight and bias will be sliced.
    """

    bound_type = retiarii_nn.Linear
    argument_list = ['in_features', 'out_features']

    def super_init_argument(self, name: str, value_choice: ValueChoiceX):
        return max(traverse_all_options(value_choice))

    def forward_with_args(self,
                          in_features: int_or_int_dict,
                          out_features: int_or_int_dict,
                          inputs: torch.Tensor) -> torch.Tensor:

        in_features = _W(in_features)
        out_features = _W(out_features)

        weight = _S(self.weight)[:out_features]
        weight = _S(weight)[:, :in_features]
        if self.bias is None:
            bias = self.bias
        else:
            bias = _S(self.bias)[:out_features]

        return F.linear(inputs, weight, bias)


_int_or_tuple = Union[int, Tuple[int, int]]


class MixedConv2d(MixedOperation, nn.Conv2d):
    """Mixed conv2d op.

    Supported arguments are:

    - ``in_channels``
    - ``out_channels``
    - ``groups`` (only supported in path sampling)
    - ``stride`` (only supported in path sampling)
    - ``kernel_size``
    - ``padding`` (only supported in path sampling)
    - ``dilation`` (only supported in path sampling)

    ``padding`` will be the "max" padding in differentiable mode.

    For channels, prefix will be sliced.
    For kernels, we take the small kernel from the center and round it to floor (left top). For example ::

        max_kernel = 5*5, sampled_kernel = 3*3, then we take [1: 4]
        max_kernel = 5*5, sampled_kernel = 2*2, then we take [1: 3]
        □ □ □ □ □   □ □ □ □ □
        □ ■ ■ ■ □   □ ■ ■ □ □
        □ ■ ■ ■ □   □ ■ ■ □ □
        □ ■ ■ ■ □   □ □ □ □ □
        □ □ □ □ □   □ □ □ □ □
    """

    bound_type = retiarii_nn.Conv2d
    argument_list = [
        'in_channels', 'out_channels', 'kernel_size', 'stride', 'padding', 'dilation', 'groups'
    ]

    @staticmethod
    def _to_tuple(value: scalar_or_scalar_dict[T]) -> Tuple[T, T]:
        if not isinstance(value, tuple):
            return (value, value)
        return value

    def super_init_argument(self, name: str, value_choice: ValueChoiceX):
        if name not in ['in_channels', 'out_channels', 'groups', 'stride', 'kernel_size', 'padding', 'dilation']:
            raise NotImplementedError(f'Unsupported value choice on argument: {name}')

        if name == ['kernel_size', 'padding']:
            all_sizes = set(traverse_all_options(value_choice))
            if any(isinstance(sz, tuple) for sz in all_sizes):
                # maximum kernel should be calculated on every dimension
                return (
                    max(self._to_tuple(sz)[0] for sz in all_sizes),
                    max(self._to_tuple(sz)[1] for sz in all_sizes)
                )
            else:
                return max(all_sizes)

        elif name == 'groups':
            # minimum groups, maximum kernel
            return min(traverse_all_options(value_choice))

        else:
            return max(traverse_all_options(value_choice))

    def forward_with_args(self,
                          in_channels: int_or_int_dict,
                          out_channels: int_or_int_dict,
                          kernel_size: scalar_or_scalar_dict[_int_or_tuple],
                          stride: _int_or_tuple,
                          padding: scalar_or_scalar_dict[_int_or_tuple],
                          dilation: int,
                          groups: int,
                          inputs: torch.Tensor) -> torch.Tensor:

        if any(isinstance(arg, dict) for arg in [stride, dilation, groups]):
            raise ValueError('stride, dilation, groups does not support weighted sampling.')

        in_channels = _W(in_channels)
        out_channels = _W(out_channels)

        # slice prefix
        # For groups > 1, we use groups to slice input weights
        weight = _S(self.weight)[:out_channels]
        weight = _S(weight)[:, :in_channels // groups]

        # slice center
        if isinstance(kernel_size, dict):
            padding = self.padding  # max padding, must be a tuple
        kernel_a, kernel_b = self._to_tuple(kernel_size)
        kernel_a, kernel_b = _W(kernel_a), _W(kernel_b)
        max_kernel_a, max_kernel_b = self.kernel_size  # self.kernel_size must be a tuple
        kernel_a_left, kernel_b_top = (max_kernel_a - kernel_a) // 2, (max_kernel_b - kernel_b) // 2
        weight = _S(weight)[:, :, kernel_a_left:kernel_a_left + kernel_a, kernel_b_top:kernel_b_top + kernel_b]

        bias = _S(self.bias)[:out_channels] if self.bias is not None else None

        # The rest parameters only need to be converted to tuple
        stride = self._to_tuple(stride)
        dilation = self._to_tuple(dilation)

        if self.padding_mode != 'zeros':
            return F.conv2d(F.pad(inputs, self._reversed_padding_repeated_twice, mode=self.padding_mode),
                            weight, bias, stride, (0, 0), dilation, groups)
        return F.conv2d(inputs, weight, bias, stride, padding, dilation, groups)


class MixedBatchNorm2d(MixedOperation, nn.BatchNorm2d):
    """
    Mixed BatchNorm2d operation.

    Supported arguments are:

    - ``num_features``
    - ``eps`` (only supported in path sampling)
    - ``momentum`` (only supported in path sampling)

    For path-sampling, prefix of ``weight``, ``bias``, ``running_mean`` and ``running_var``
    are sliced. For weighted cases, the maximum ``num_features`` is used directly.

    Momentum is required to be float.
    PyTorch BatchNorm supports a case where momentum can be none, which is not supported here.
    """

    bound_type = retiarii_nn.BatchNorm2d
    argument_list = ['num_features', 'eps', 'momentum']

    def super_init_argument(self, name: str, value_choice: ValueChoiceX):
        return max(traverse_all_options(value_choice))

    def forward_with_args(self,
                          num_features: int_or_int_dict,
                          eps: float,
                          momentum: float,
                          inputs: torch.Tensor) -> torch.Tensor:

        if any(isinstance(arg, dict) for arg in [eps, momentum]):
            raise ValueError('eps, momentum do not support weighted sampling')

        if isinstance(num_features, dict):
            num_features = self.num_features

        weight, bias = self.weight, self.bias
        running_mean, running_var = self.running_mean, self.running_var

        if num_features < self.num_features:
            weight = weight[:num_features]
            bias = bias[:num_features]
            running_mean = running_mean[:num_features]
            running_var = running_var[:num_features]

        if self.training:
            bn_training = True
        else:
            bn_training = (self.running_mean is None) and (self.running_var is None)

        return F.batch_norm(
            inputs,
            # If buffers are not to be tracked, ensure that they won't be updated
            running_mean if not self.training or self.track_running_stats else None,
            running_var if not self.training or self.track_running_stats else None,
            weight,
            bias,
            bn_training,
            momentum,  # originally exponential_average_factor in pytorch code
            eps,
        )


class MixedMultiHeadAttention(MixedOperation, nn.MultiheadAttention):
    """
    Mixed multi-head attention.

    Supported arguments are:

    - ``embed_dim``
    - ``num_heads`` (only supported in path sampling)
    - ``kdim``
    - ``vdim``
    - ``dropout`` (only supported in path sampling)

    At init, it constructs the largest possible Q, K, V dimension.
    At forward, it slices the prefix to weight matrices according to the sampled value.
    For ``in_proj_bias`` and ``in_proj_weight``, three parts will be sliced and concatenated together:
    ``[0, embed_dim)``, ``[max_embed_dim, max_embed_dim + embed_dim)``,
    ``[max_embed_dim * 2, max_embed_dim * 2 + embed_dim)``.

    Warnings
    ----------
    All candidates of ``embed_dim`` should be divisible by all candidates of ``num_heads``.
    """

    bound_type = retiarii_nn.MultiheadAttention
    argument_list = ['embed_dim', 'num_heads', 'kdim', 'vdim', 'dropout']

    def __post_init__(self):
        # sometimes super-class believes qkv have the same embed_dim.
        # but actually they do not, because we can have dynamic (mutable) kdim/vdim.

        _qkv_same_embed_dim = True

        for dimension in ['kdim', 'vdim']:
            if self.init_arguments[dimension] is None:
                # must follow embed_dim is this case
                continue

            if getattr(self, dimension) == self.embed_dim and \
                    (dimension in self.mutable_arguments or 'embed_dim' in self.mutable_arguments):
                _qkv_same_embed_dim = False

        if self._qkv_same_embed_dim and not _qkv_same_embed_dim:
            self._qkv_same_embed_dim = _qkv_same_embed_dim

            # adding back missing parameters
            # factory_kwargs could be empty for legacy pytorch versions
            factory_kwargs = {}
            if 'device' in self.init_arguments:
                factory_kwargs['device'] = self.init_arguments['device']
            if 'dtype' in self.init_arguments:
                factory_kwargs['dtype'] = self.init_arguments['dtype']
            self.q_proj_weight = nn.Parameter(torch.empty((self.embed_dim, self.embed_dim), **factory_kwargs))
            self.k_proj_weight = nn.Parameter(torch.empty((self.embed_dim, self.kdim), **factory_kwargs))
            self.v_proj_weight = nn.Parameter(torch.empty((self.embed_dim, self.vdim), **factory_kwargs))
            self.register_parameter('in_proj_weight', None)

            # reset parameters
            nn.init.xavier_uniform_(self.q_proj_weight)
            nn.init.xavier_uniform_(self.k_proj_weight)
            nn.init.xavier_uniform_(self.v_proj_weight)

    def super_init_argument(self, name: str, value_choice: ValueChoiceX):
        return max(traverse_all_options(value_choice))

    def _to_proj_slice(self, embed_dim: _W) -> List[slice]:
        # slice three parts, corresponding to q, k, v respectively
        return [
            slice(embed_dim),
            slice(self.embed_dim, self.embed_dim + embed_dim),
            slice(self.embed_dim * 2, self.embed_dim * 2 + embed_dim)
        ]

    def forward_with_args(
        self,
        embed_dim: int_or_int_dict, num_heads: int,
        kdim: Optional[int_or_int_dict], vdim: Optional[int_or_int_dict],
        dropout: float,
        query: torch.Tensor, key: torch.Tensor, value: torch.Tensor,
        key_padding_mask: Optional[torch.Tensor] = None,
        need_weights: bool = True, attn_mask: Optional[torch.Tensor] = None
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:

        if any(isinstance(arg, dict) for arg in [num_heads, dropout]):
            raise ValueError('num_heads, dropout do not support weighted sampling.')

        # by default, kdim, vdim can be none
        if kdim is None:
            kdim = embed_dim
        if vdim is None:
            vdim = embed_dim

        qkv_same_embed_dim = kdim == embed_dim and vdim == embed_dim

        if getattr(self, 'batch_first', False):
            # for backward compatibility: v1.7 doesn't have batch_first
            query, key, value = [x.transpose(1, 0) for x in (query, key, value)]

        if isinstance(embed_dim, dict):
            used_embed_dim = self.embed_dim
        else:
            used_embed_dim = embed_dim

        embed_dim = _W(embed_dim)

        # in projection weights & biases has q, k, v weights concatenated together
        in_proj_bias = in_proj_weight = None
        if self.in_proj_bias is not None:
            in_proj_bias = _S(self.in_proj_bias)[self._to_proj_slice(embed_dim)]
        if self.in_proj_weight is not None:
            in_proj_weight = _S(self.in_proj_weight)[self._to_proj_slice(embed_dim), :embed_dim]

        bias_k = _S(self.bias_k)[:, :, :embed_dim] if self.bias_k is not None else None
        bias_v = _S(self.bias_v)[:, :, :embed_dim] if self.bias_v is not None else None
        out_proj_weight = _S(self.out_proj.weight)[:embed_dim, :embed_dim]
        out_proj_bias = _S(self.out_proj.bias)[:embed_dim] if self.out_proj.bias is not None else None

        if not qkv_same_embed_dim:
            kdim = _W(kdim)
            vdim = _W(vdim)

            q_proj = _S(self.q_proj_weight)[:embed_dim, :embed_dim]
            k_proj = _S(self.k_proj_weight)[:embed_dim]
            k_proj = _S(k_proj)[:, :kdim]
            v_proj = _S(self.v_proj_weight)[:embed_dim]
            v_proj = _S(v_proj)[:, :vdim]

            # The rest part is basically same as pytorch
            attn_output, attn_output_weights = F.multi_head_attention_forward(
                query, key, value, used_embed_dim, num_heads,
                in_proj_weight, in_proj_bias,
                bias_k, bias_v, self.add_zero_attn,
                dropout, out_proj_weight, out_proj_bias,
                training=self.training,
                key_padding_mask=key_padding_mask, need_weights=need_weights,
                attn_mask=attn_mask, use_separate_proj_weight=True,
                q_proj_weight=q_proj, k_proj_weight=k_proj, v_proj_weight=v_proj)
        else:
            attn_output, attn_output_weights = F.multi_head_attention_forward(
                query, key, value, used_embed_dim, num_heads,
                in_proj_weight, in_proj_bias,
                bias_k, bias_v, self.add_zero_attn,
                dropout, out_proj_weight, out_proj_bias,
                training=self.training,
                key_padding_mask=key_padding_mask, need_weights=need_weights,
                attn_mask=attn_mask)

        if getattr(self, 'batch_first', False):  # backward compatibility
            return attn_output.transpose(1, 0), attn_output_weights
        else:
            return attn_output, attn_output_weights


NATIVE_MIXED_OPERATIONS: List[Type[MixedOperation]] = [
    MixedLinear,
    MixedConv2d,
    MixedBatchNorm2d,
    MixedMultiHeadAttention,
]
