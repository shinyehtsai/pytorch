from __future__ import annotations

import copy
import functools

import itertools
import operator
from typing import Any, Callable, Dict, List, Optional, Set

import torch
import torch._dynamo as torchdynamo
import torch.nn.functional as F

from torch.ao.quantization._pt2e.graph_utils import find_sequential_partitions

from torch.ao.quantization._pt2e.quantizer.utils import (
    _annotate_input_qspec_map,
    _annotate_output_qspec,
    get_act_qspec,
    get_bias_qspec,
    get_weight_qspec,
)
from torch.ao.quantization.fake_quantize import FusedMovingAvgObsFakeQuantize
from torch.ao.quantization.observer import (
    HistogramObserver,
    MinMaxObserver,
    MovingAverageMinMaxObserver,
    MovingAveragePerChannelMinMaxObserver,
    PerChannelMinMaxObserver,
    PlaceholderObserver,
)
from torch.ao.quantization.qconfig import _ObserverOrFakeQuantizeConstructor

from torch.fx import Node
from torch.fx.passes.utils.source_matcher_utils import get_source_partitions

from .quantizer import (
    OperatorConfig,
    OperatorPatternType,
    QuantizationAnnotation,
    QuantizationConfig,
    QuantizationSpec,
    Quantizer,
)


__all__ = [
    "QNNPackQuantizer",
    "get_symmetric_quantization_config",
]

_QUANT_CONFIG_TO_ANNOTATOR = {}


def _mark_nodes_as_annotated(nodes: List[Node]):
    for node in nodes:
        if node is not None:
            if "quantization_annotation" not in node.meta:
                node.meta["quantization_annotation"] = QuantizationAnnotation()
            node.meta["quantization_annotation"]._annotated = True


def _get_dynamo_graph(function: Callable, inputs) -> torch.fx.Graph:
    gm, _ = torchdynamo.export(function, *inputs, aten_graph=True)
    gm.graph.eliminate_dead_code()
    return gm.graph


def _get_linear_patterns(input_size: List[int]):
    in_channels = input_size[-1]
    out_channels = 8  # hard coding but this should not matter
    weight = torch.ones((out_channels, in_channels))
    bias = torch.ones((out_channels,))
    act = torch.ones(input_size)

    def linear_op(act, weight, bias=None):
        return F.linear(act, weight, bias)

    pattern_w_bias = _get_dynamo_graph(linear_op, (act, weight, bias))
    pattern_wo_bias = _get_dynamo_graph(linear_op, (act, weight))
    return [pattern_w_bias, pattern_wo_bias]


def register_annotator(quantization_configs: List[QuantizationConfig]):
    def decorator(fn: Callable):
        for quantization_config in quantization_configs:
            if quantization_config in _QUANT_CONFIG_TO_ANNOTATOR:
                raise KeyError(
                    f"Annotator for quantization config {quantization_config} is already registered"
                )
            _QUANT_CONFIG_TO_ANNOTATOR[quantization_config] = functools.partial(
                fn, config=quantization_config
            )

    return decorator


def supported_symmetric_quantized_operators() -> Dict[str, List[OperatorPatternType]]:
    supported_operators: Dict[str, List[OperatorPatternType]] = {
        # Both conv and linear should be able to handle relu + hardtanh fusion since
        # those are clamp ops
        "conv2d": [
            [torch.nn.Conv2d, torch.nn.ReLU],
            [torch.nn.Conv2d, F.relu],
            [F.conv2d, torch.nn.ReLU],
            [F.conv2d, F.relu],
        ],
        "linear": [[torch.nn.Linear], [F.linear]],
        "add": [[torch.add]],
        "maxpool2d": [[torch.nn.MaxPool2d], [F.max_pool2d]],
        "hardtanh": [[torch.nn.Hardtanh], [F.hardtanh]],
        "mean": [[torch.mean]],
        "adaptive_avgpool2d": [
            [torch.nn.AdaptiveAvgPool2d],
            [F.adaptive_avg_pool2d],
        ],
    }
    return copy.deepcopy(supported_operators)


def get_supported_symmetric_config_and_operators() -> List[OperatorConfig]:
    supported_config_and_operators: List[OperatorConfig] = []
    for quantization_config in [
        get_symmetric_quantization_config(),
        get_symmetric_quantization_config(is_qat=True),
        get_symmetric_quantization_config(is_per_channel=True),
        get_symmetric_quantization_config(is_per_channel=True, is_qat=True),
    ]:
        ops = supported_symmetric_quantized_operators()
        for op_string, pattern_list in ops.items():
            supported_config_and_operators.append(
                OperatorConfig(quantization_config, pattern_list)
            )
    return copy.deepcopy(supported_config_and_operators)


@functools.lru_cache
def get_symmetric_quantization_config(
    is_per_channel: bool = False,
    is_qat: bool = False,
):
    act_observer_or_fake_quant_ctr: _ObserverOrFakeQuantizeConstructor = (
        FusedMovingAvgObsFakeQuantize if is_qat else HistogramObserver
    )

    act_quantization_spec = QuantizationSpec(
        dtype=torch.int8,
        quant_min=-128,
        quant_max=127,
        qscheme=torch.per_tensor_affine,
        is_dynamic=False,
        observer_or_fake_quant_ctr=act_observer_or_fake_quant_ctr.with_args(
            eps=2**-12
        ),
    )
    qscheme = (
        torch.per_channel_symmetric if is_per_channel else torch.per_tensor_symmetric
    )
    weight_observer_or_fake_quant_ctr: _ObserverOrFakeQuantizeConstructor = (
        MinMaxObserver
    )
    if is_qat:
        weight_observer_or_fake_quant_ctr = FusedMovingAvgObsFakeQuantize
    elif is_per_channel:
        weight_observer_or_fake_quant_ctr = PerChannelMinMaxObserver

    extra_args: Dict[str, Any] = {"eps": 2**-12}
    if is_qat:
        if qscheme == torch.per_tensor_symmetric:
            extra_args["observer"] = MovingAverageMinMaxObserver
        else:
            extra_args["observer"] = MovingAveragePerChannelMinMaxObserver  # type: ignore[dict-item]
    weight_quantization_spec = QuantizationSpec(
        dtype=torch.int8,
        quant_min=-127,
        quant_max=127,
        qscheme=qscheme,
        ch_axis=0,
        is_dynamic=False,
        observer_or_fake_quant_ctr=weight_observer_or_fake_quant_ctr.with_args(
            **extra_args
        ),
    )

    bias_observer_or_fake_quant_ctr: _ObserverOrFakeQuantizeConstructor = (
        PlaceholderObserver
    )
    bias_quantization_spec = QuantizationSpec(
        dtype=torch.float, observer_or_fake_quant_ctr=bias_observer_or_fake_quant_ctr
    )
    quantization_config = QuantizationConfig(
        act_quantization_spec, weight_quantization_spec, bias_quantization_spec, is_qat
    )
    return quantization_config


def get_supported_config_and_operators() -> List[OperatorConfig]:
    return get_supported_symmetric_config_and_operators()


def _is_annotated(nodes: List[Node]):
    """
    Given a list of nodes (that represents an operator pattern),
    check if any of the node is annotated, return True if any of the node
    is annotated, otherwise return False
    """
    annotated = False
    for node in nodes:
        annotated = annotated or (
            "quantization_annotation" in node.meta
            and node.meta["quantization_annotation"]._annotated
        )
    return annotated


class QNNPackQuantizer(Quantizer):
    supported_config_and_operators = get_supported_config_and_operators()

    def __init__(self):
        super().__init__()
        self.global_config: QuantizationConfig = None  # type: ignore[assignment]
        self.operator_type_config: Dict[str, Optional[QuantizationConfig]] = {}

    @classmethod
    def get_supported_quantization_configs(cls) -> List[QuantizationConfig]:
        op_configs: Set[QuantizationConfig] = set({})
        for spec, _ in cls.supported_config_and_operators:
            op_configs.add(spec)
        return list(op_configs)

    @classmethod
    def get_supported_operator_for_quantization_config(
        cls, quantization_config: Optional[QuantizationConfig]
    ) -> List[OperatorPatternType]:
        if quantization_config is None:
            all_ops = []
            for _, ops in cls.supported_config_and_operators:
                all_ops.extend(ops)
            return all_ops

        for config, ops in cls.supported_config_and_operators:
            # note: this assumes each entry in cls.supported_spec_and_operators
            # corresponds to one spec, e.g. we don't have
            # [(spec1, op_list1), (spec1, op_list2), (spec2, op_list3)]
            # where the first and second entry have the same spec but did not
            # merge the op list
            if config == quantization_config:
                return ops
        return []

    def set_global(self, quantization_config: QuantizationConfig) -> QNNPackQuantizer:
        self.global_config = quantization_config
        return self

    def set_config_for_operator_type(
        self, operator_type: str, quantization_config: QuantizationConfig
    ) -> QNNPackQuantizer:
        self.operator_type_config[operator_type] = quantization_config
        return self

    def annotate(self, model: torch.fx.GraphModule) -> torch.fx.GraphModule:
        """just handling global spec for now"""
        global_config = self.global_config
        # _QUANT_CONFIG_TO_ANNOTATOR[global_config](self, model)
        # TODO: validate that global_config is supported
        self.annotate_symmetric_config(model, global_config)

        return model

    # @register_annotator(
    #     [
    #         get_symmetric_quantization_config(is_per_channel=False, is_qat=False),
    #         get_symmetric_quantization_config(is_per_channel=False, is_qat=True),
    #         get_symmetric_quantization_config(is_per_channel=True, is_qat=True),
    #         get_symmetric_quantization_config(is_per_channel=True, is_qat=False),
    #     ]
    # )
    def annotate_symmetric_config(
        self, model: torch.fx.GraphModule, config: QuantizationConfig
    ) -> torch.fx.GraphModule:
        # annotate the nodes from last to first since the matching is in the reversed order
        # and fusion operator patterns (conv - relu) can get matched before single operator pattern (conv)
        # and we will mark the matched node with "_annoated" so fusion operator pattern
        # can take precedence over single operator pattern in this way
        self._annotate_linear(model, config)
        if config.is_qat:
            self._annotate_conv2d_bn_relu(model, config)
            self._annotate_conv2d_bn(model, config)
        self._annotate_conv2d_relu(model, config)
        self._annotate_conv2d(model, config)
        self._annotate_maxpool2d(model, config)
        self._annotate_add_relu(model, config)
        self._annotate_add(model, config)
        self._annotate_hardtanh(model, config)
        self._annotate_mean(model, config)
        self._annotate_adaptive_avg_pool2d(model, config)
        return model

    def _annotate_conv2d_bn(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        """
        Find Conv2d + batchnorm parititions
        Note: This is only used for QAT. In PTQ, batchnorm should already be fused into the conv.
        """
        fused_partitions = find_sequential_partitions(
            gm, [torch.nn.Conv2d, torch.nn.BatchNorm2d]
        )
        for fused_partition in fused_partitions:
            conv_partition, bn_partition = fused_partition
            if len(conv_partition.output_nodes) > 1:
                raise ValueError("conv partition has more than one output node")
            conv_node = conv_partition.output_nodes[0]
            conv_node_users = list(conv_node.users.keys())
            if len(conv_node_users) > 1:
                raise ValueError(
                    "Conv node must be consumed by BN only for it to be fusable."
                )
            if len(bn_partition.output_nodes) > 1:
                raise ValueError("BatchNorm partition has more than one output node")
            bn_output_node = bn_partition.output_nodes[0]

            if _is_annotated([bn_output_node, conv_node]):
                continue

            input_qspec_map = {}
            input_act = conv_node.args[0]
            assert isinstance(input_act, Node)
            input_qspec_map[input_act] = get_act_qspec(quantization_config)

            weight = conv_node.args[1]
            assert isinstance(weight, Node)
            input_qspec_map[weight] = get_weight_qspec(quantization_config)

            bias = conv_node.args[2]
            if isinstance(bias, Node):
                input_qspec_map[bias] = get_bias_qspec(quantization_config)

            conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map, _annotated=True
            )

            bn_output_node.meta["quantization_annotation"] = QuantizationAnnotation(
                output_qspec=get_act_qspec(quantization_config),  # type: ignore[arg-type]
                _annotated=True,
            )
            nodes_to_mark_annotated = list(conv_partition.nodes)
            nodes_to_mark_annotated.extend(list(bn_partition.nodes))
            _mark_nodes_as_annotated(nodes_to_mark_annotated)

    def _annotate_conv2d_bn_relu(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        """
        Find Conv2d + batchnorm + relu parititions
        Note: This is only used for QAT. In PTQ, batchnorm should already be fused into the conv.
        """
        fused_partitions = find_sequential_partitions(
            gm, [torch.nn.Conv2d, torch.nn.BatchNorm2d, torch.nn.ReLU]
        )
        for fused_partition in fused_partitions:
            conv_partition, bn_partition, relu_partition = fused_partition
            if len(relu_partition.output_nodes) > 1:
                raise ValueError("Relu partition has more than one output node")
            relu_node = relu_partition.output_nodes[0]
            if len(conv_partition.output_nodes) > 1:
                raise ValueError("conv partition has more than one output node")
            conv_node = conv_partition.output_nodes[0]
            conv_node_users = list(conv_node.users.keys())
            if len(conv_node_users) > 1:
                raise ValueError(
                    "Conv node must be consumed by BN only for it to be fusable."
                )
            if len(bn_partition.output_nodes) > 1:
                raise ValueError("BatchNorm partition has more than one output node")
            bn_output_node = bn_partition.output_nodes[0]

            if _is_annotated([relu_node, bn_output_node, conv_node]):
                continue

            input_qspec_map = {}
            input_act = conv_node.args[0]
            assert isinstance(input_act, Node)
            input_qspec_map[input_act] = get_act_qspec(quantization_config)

            weight = conv_node.args[1]
            assert isinstance(weight, Node)
            input_qspec_map[weight] = get_weight_qspec(quantization_config)

            bias = conv_node.args[2]
            if isinstance(bias, Node):
                input_qspec_map[bias] = get_bias_qspec(quantization_config)

            conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map, _annotated=True
            )

            relu_node.meta["quantization_annotation"] = QuantizationAnnotation(
                output_qspec=get_act_qspec(quantization_config),  # type: ignore[arg-type]
                _annotated=True,
            )
            nodes_to_mark_annotated = list(conv_partition.nodes)
            nodes_to_mark_annotated.extend(list(bn_partition.nodes))
            nodes_to_mark_annotated.extend(list(relu_partition.nodes))
            _mark_nodes_as_annotated(nodes_to_mark_annotated)

    def _annotate_conv2d_relu(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        fused_partitions = find_sequential_partitions(
            gm, [torch.nn.Conv2d, torch.nn.ReLU]
        )
        for fused_partition in fused_partitions:
            conv_partition, relu_partition = fused_partition
            if len(relu_partition.output_nodes) > 1:
                raise ValueError("Relu partition has more than one output node")
            relu_node = relu_partition.output_nodes[0]
            if len(conv_partition.output_nodes) > 1:
                raise ValueError("conv partition has more than one output node")
            conv_node = conv_partition.output_nodes[0]

            if not isinstance(conv_node, Node):
                raise ValueError(f"{conv_node} is not a Node")
            if (
                conv_node.op != "call_function"
                or conv_node.target != torch.ops.aten.convolution.default
            ):
                raise ValueError(f"{conv_node} is not an aten conv2d operator")
            if relu_node.op != "call_function" or relu_node.target not in [
                torch.ops.aten.relu.default,
                torch.ops.aten.relu_.default,
            ]:
                raise ValueError(f"{relu_node} is not an aten relu operator")

            if _is_annotated([relu_node, conv_node]):
                continue

            input_qspec_map = {}
            input_act = conv_node.args[0]
            assert isinstance(input_act, Node)
            input_qspec_map[input_act] = get_act_qspec(quantization_config)

            weight = conv_node.args[1]
            assert isinstance(weight, Node)
            input_qspec_map[weight] = get_weight_qspec(quantization_config)

            bias = conv_node.args[2]
            if isinstance(bias, Node):
                input_qspec_map[bias] = get_bias_qspec(quantization_config)

            conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map, _annotated=True
            )
            relu_node.meta["quantization_annotation"] = QuantizationAnnotation(
                output_qspec=get_act_qspec(quantization_config),  # type: ignore[arg-type]
                _annotated=True,
            )

    def _annotate_conv2d(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        conv_partitions = get_source_partitions(
            gm.graph, [torch.nn.Conv2d, torch.nn.functional.conv2d]
        )
        conv_partitions = list(itertools.chain(*conv_partitions.values()))
        for conv_partition in conv_partitions:
            if len(conv_partition.output_nodes) > 1:
                raise ValueError("conv partition has more than one output node")
            conv_node = conv_partition.output_nodes[0]
            if (
                conv_node.op != "call_function"
                or conv_node.target != torch.ops.aten.convolution.default
            ):
                raise ValueError(f"{conv_node} is not an aten conv2d operator")
            # skip annotation if it is already annotated
            if _is_annotated([conv_node]):
                continue

            input_qspec_map = {}
            input_act = conv_node.args[0]
            assert isinstance(input_act, Node)
            input_qspec_map[input_act] = get_act_qspec(quantization_config)

            weight = conv_node.args[1]
            assert isinstance(weight, Node)
            input_qspec_map[weight] = get_weight_qspec(quantization_config)

            bias = conv_node.args[2]
            if isinstance(bias, Node):
                input_qspec_map[bias] = get_bias_qspec(quantization_config)

            conv_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map,
                output_qspec=get_act_qspec(quantization_config),
                _annotated=True,
            )

    def _annotate_linear(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        module_partitions = get_source_partitions(
            gm.graph, [torch.nn.Linear, torch.nn.functional.linear]
        )
        act_qspec = get_act_qspec(quantization_config)
        weight_qspec = get_weight_qspec(quantization_config)
        bias_qspec = get_bias_qspec(quantization_config)
        for module_or_fn_type, partitions in module_partitions.items():
            if module_or_fn_type == torch.nn.Linear:
                for p in partitions:
                    act_node = p.input_nodes[0]
                    output_node = p.output_nodes[0]
                    weight_node = None
                    bias_node = None
                    for node in p.params:
                        weight_or_bias = getattr(gm, node.target)  # type: ignore[arg-type]
                        if weight_or_bias.ndim == 2:  # type: ignore[attr-defined]
                            weight_node = node
                        if weight_or_bias.ndim == 1:  # type: ignore[attr-defined]
                            bias_node = node
                    if weight_node is None:
                        raise ValueError("No weight found in Linear pattern")
                    # find use of act node within the matched pattern
                    act_use_node = None
                    for node in p.nodes:
                        if node in act_node.users:  # type: ignore[union-attr]
                            act_use_node = node
                            break
                    if act_use_node is None:
                        raise ValueError(
                            "Could not find an user of act node within matched pattern."
                        )
                    if _is_annotated([act_use_node]) is False:  # type: ignore[list-item]
                        _annotate_input_qspec_map(
                            act_use_node,
                            act_node,
                            act_qspec,
                        )
                    if bias_node and _is_annotated([bias_node]) is False:
                        _annotate_output_qspec(bias_node, bias_qspec)
                    if _is_annotated([weight_node]) is False:  # type: ignore[list-item]
                        _annotate_output_qspec(weight_node, weight_qspec)
                    if _is_annotated([output_node]) is False:
                        _annotate_output_qspec(output_node, act_qspec)
                    nodes_to_mark_annotated = list(p.nodes)
                    _mark_nodes_as_annotated(nodes_to_mark_annotated)

    def _annotate_maxpool2d(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        module_partitions = get_source_partitions(
            gm.graph, [torch.nn.MaxPool2d, torch.nn.functional.max_pool2d]
        )
        maxpool_partitions = list(itertools.chain(*module_partitions.values()))
        for maxpool_partition in maxpool_partitions:
            output_node = maxpool_partition.output_nodes[0]
            maxpool_node = None
            for n in maxpool_partition.nodes:
                if n.target == torch.ops.aten.max_pool2d_with_indices.default:
                    maxpool_node = n
            if _is_annotated([output_node, maxpool_node]):  # type: ignore[list-item]
                continue

            input_act = maxpool_node.args[0]  # type: ignore[union-attr]
            assert isinstance(input_act, Node)

            act_qspec = get_act_qspec(quantization_config)
            maxpool_node.meta["quantization_annotation"] = QuantizationAnnotation(  # type: ignore[union-attr]
                input_qspec_map={
                    input_act: act_qspec,
                },
                _annotated=True,
            )
            output_node.meta["quantization_annotation"] = QuantizationAnnotation(
                output_qspec=act_qspec,
                _input_output_share_observers=True,
                _annotated=True,
            )

    def _annotate_input_out_obs_sharing_op(
        self,
        op: Callable,
        gm: torch.fx.GraphModule,
        quantization_config: QuantizationConfig,
    ) -> None:
        module_partitions = get_source_partitions(
            gm.graph,
            [op],
        )
        partitions = list(itertools.chain(*module_partitions.values()))
        for partition in partitions:
            io_obs_sharing_node = partition.output_nodes[0]
            if _is_annotated([io_obs_sharing_node]):
                continue

            input_act = io_obs_sharing_node.args[0]
            assert isinstance(input_act, Node)

            act_qspec = get_act_qspec(quantization_config)
            io_obs_sharing_node.meta[
                "quantization_annotation"
            ] = QuantizationAnnotation(
                input_qspec_map={
                    input_act: act_qspec,
                },
                output_qspec=act_qspec,
                _input_output_share_observers=True,
                _annotated=True,
            )

    def _annotate_hardtanh(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        self._annotate_input_out_obs_sharing_op(
            torch.nn.modules.Hardtanh, gm, quantization_config
        )

    def _annotate_mean(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        self._annotate_input_out_obs_sharing_op(torch.mean, gm, quantization_config)

    def _annotate_adaptive_avg_pool2d(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        self._annotate_input_out_obs_sharing_op(
            torch.nn.AdaptiveAvgPool2d, gm, quantization_config
        )

    def _annotate_add_relu(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        fused_partitions = find_sequential_partitions(gm, [torch.add, torch.nn.ReLU])
        for fused_partition in fused_partitions:
            add_partition, relu_partition = fused_partition
            if len(relu_partition.output_nodes) > 1:
                raise ValueError("Relu partition has more than one output node")
            relu_node = relu_partition.output_nodes[0]
            if len(add_partition.output_nodes) > 1:
                raise ValueError("add partition has more than one output node")
            add_node = add_partition.output_nodes[0]

            if _is_annotated([relu_node, add_node]):
                continue

            act_qspec = get_act_qspec(quantization_config)

            input_qspec_map = {}
            input_act0 = add_node.args[0]
            if isinstance(input_act0, Node):
                input_qspec_map[input_act0] = act_qspec

            input_act1 = add_node.args[1]
            if isinstance(input_act1, Node):
                input_qspec_map[input_act1] = act_qspec

            add_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map,
                _annotated=True,
            )
            relu_node.meta["quantization_annotation"] = QuantizationAnnotation(
                output_qspec=act_qspec,
                _annotated=True,
            )

    def _annotate_add(
        self, gm: torch.fx.GraphModule, quantization_config: QuantizationConfig
    ) -> None:
        add_partitions = get_source_partitions(gm.graph, [operator.add, torch.add])
        add_partitions = list(itertools.chain(*add_partitions.values()))
        for add_partition in add_partitions:
            add_node = add_partition.output_nodes[0]
            if _is_annotated([add_node]):
                continue

            act_qspec = get_act_qspec(quantization_config)

            input_qspec_map = {}
            input_act0 = add_node.args[0]
            if isinstance(input_act0, Node):
                input_qspec_map[input_act0] = act_qspec

            input_act1 = add_node.args[1]
            if isinstance(input_act1, Node):
                input_qspec_map[input_act1] = act_qspec

            add_node.meta["quantization_annotation"] = QuantizationAnnotation(
                input_qspec_map=input_qspec_map,
                output_qspec=act_qspec,
                _annotated=True,
            )

    def validate(self, model: torch.fx.GraphModule) -> None:
        pass

    @classmethod
    def get_supported_operators(cls) -> List[OperatorConfig]:
        return cls.supported_config_and_operators
