# Lint as: python3
# pylint: disable=g-bad-file-header
# Copyright 2020 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Core learned graph net model."""

import collections
from math import ceil
from collections import OrderedDict
import functools
import torch
from torch import nn as nn
import torch.nn.functional as F
import torch_scatter

EdgeSet = collections.namedtuple('EdgeSet', ['name', 'features', 'senders',
                                             'receivers'])
MultiGraph = collections.namedtuple('Graph', ['node_features', 'edge_sets'])

device = torch.device('cuda')


class LazyMLP(nn.Module):
    def __init__(self, output_sizes):
        super().__init__()
        num_layers = len(output_sizes)
        self._layers_ordered_dict = OrderedDict()
        for index, output_size in enumerate(output_sizes):
            self._layers_ordered_dict["linear_" + str(index)] = nn.LazyLinear(output_size)
            if index < (num_layers - 1):
                self._layers_ordered_dict["relu_" + str(index)] = nn.ReLU()
        self.layers = nn.Sequential(self._layers_ordered_dict)

    def forward(self, input):
        input = input.to(device)
        y = self.layers(input)
        return y


class GraphNetBlock(nn.Module):
    """Multi-Edge Interaction Network with residual connections."""

    def __init__(self, model_fn, output_size, message_passing_aggregator):
        super().__init__()
        self.edge_model = model_fn(output_size)
        self.node_model = model_fn(output_size)
        self.message_passing_aggregator = message_passing_aggregator

    def _update_edge_features(self, node_features, edge_set):
        """Aggregrates node features, and applies edge function."""
        senders = edge_set.senders.to(device)
        receivers = edge_set.receivers.to(device)
        sender_features = torch.index_select(input=node_features, dim=0, index=senders)
        receiver_features = torch.index_select(input=node_features, dim=0, index=receivers)
        features = [sender_features, receiver_features, edge_set.features]
        features = torch.cat(features, dim=-1)
        return self.edge_model(features)

    '''
    def _update_node_features_mp_helper(self, features, receivers, add_intermediate):
        for index, feature_tensor in enumerate(features):
            des_index = receivers[index]
            add_intermediate[des_index].add(feature_tensor)
        return add_intermediate
    '''

    def unsorted_segment_operation(self, data, segment_ids, num_segments, operation):
        """
        Computes the sum along segments of a tensor. Analogous to tf.unsorted_segment_sum.

        :param data: A tensor whose segments are to be summed.
        :param segment_ids: The segment indices tensor.
        :param num_segments: The number of segments.
        :return: A tensor of same data type as the data argument.
        """
        assert all([i in data.shape for i in segment_ids.shape]), "segment_ids.shape should be a prefix of data.shape"

        # segment_ids is a 1-D tensor repeat it to have the same shape as data
        if len(segment_ids.shape) == 1:
            s = torch.prod(torch.tensor(data.shape[1:])).long().to(device)
            segment_ids = segment_ids.repeat_interleave(s).view(segment_ids.shape[0], *data.shape[1:]).to(device)

        assert data.shape == segment_ids.shape, "data.shape and segment_ids.shape should be equal"

        shape = [num_segments] + list(data.shape[1:])
        result = torch.zeros(*shape)
        if operation == 'add':
            result = torch_scatter.scatter_add(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'max':
            result, _ = torch_scatter.scatter_max(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'mean':
            result = torch_scatter.scatter_mean(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'min':
            result, _ = torch_scatter.scatter_min(data.float(), segment_ids, dim=0, dim_size=num_segments)
        result = result.type(data.dtype)
        return result

    def _update_node_features(self, node_features, edge_sets):
        """Aggregrates edge features, and applies node function."""
        num_nodes = node_features.shape[0]
        features = [node_features]
        for edge_set in edge_sets:
            features.append(self.unsorted_segment_operation(edge_set.features, edge_set.receivers, num_nodes, operation=self.message_passing_aggregator))
            '''
            add_intermediate = torch.zeros(num_nodes, edge_set.features.shape[1])
            for index, feature_tensor in enumerate(edge_set.features.to('cpu')):
              des_index = edge_set.receivers[index].to('cpu')
              add_intermediate[des_index].add(feature_tensor)
            features.append(add_intermediate)
            '''
            '''
            # use multiprocessing to decrease execution time
            
            payload_per_core = edge_set.features.shape[0] // self._cpu_cores
            argument_list = []
            for i in range(self._cpu_cores - 1):
              i = i * payload_per_core
              add_intermediate = torch.zeros(num_nodes, edge_set.features.shape[1]).to(device=cuda0)
              argument_list.append((edge_set.features[i : i + payload_per_core, : ], edge_set.receivers[i : i + payload_per_core], add_intermediate))
            i = (self._cpu_cores - 1) * payload_per_core
            add_intermediate = torch.zeros(num_nodes, edge_set.features.shape[1]).to(device=cuda0)
            argument_list.append((edge_set.features[i : , : ], edge_set.receivers[i : ], add_intermediate))
            result_iterable = self._update_node_features_thread_pool.starmap(self._update_node_features_mp_helper, argument_list)
            result = torch.zeros(num_nodes, edge_set.features.shape[1]).to(device=cuda0)
            for add_intermediate in result_iterable:
              result.add(add_intermediate)
            features.append(result)
            '''
        features = torch.cat(features, dim=-1)
        return self.node_model(features)

    def forward(self, graph):
        """Applies GraphNetBlock and returns updated MultiGraph."""
        # apply edge functions
        new_edge_sets = []
        for edge_set in graph.edge_sets:
            updated_features = self._update_edge_features(graph.node_features, edge_set)
            new_edge_sets.append(edge_set._replace(features=updated_features))

        # apply node function
        new_node_features = self._update_node_features(graph.node_features, new_edge_sets)

        # add residual connections
        new_node_features += graph.node_features
        new_edge_sets = [es._replace(features=es.features + old_es.features)
                         for es, old_es in zip(new_edge_sets, graph.edge_sets)]
        return MultiGraph(new_node_features, new_edge_sets)


class Encoder(nn.Module):
    """Encodes node and edge features into latent features."""

    def __init__(self, make_mlp, latent_size):
        super().__init__()
        self._make_mlp = make_mlp
        self._latent_size = latent_size
        self.node_model = self._make_mlp(latent_size)
        self.mesh_edge_model = self._make_mlp(latent_size)
        '''
        for _ in graph.edge_sets:
          edge_model = make_mlp(latent_size)
          self.edge_models.append(edge_model)
        '''

    def forward(self, graph):
        node_latents = self.node_model(graph.node_features)
        new_edges_sets = []

        for index, edge_set in enumerate(graph.edge_sets):
            feature = edge_set.features
            latent = self.mesh_edge_model(feature)
            new_edges_sets.append(edge_set._replace(features=latent))
        return MultiGraph(node_latents, new_edges_sets)


class Decoder(nn.Module):
    """Decodes node features from graph."""
    # decoder = self._make_mlp(self._output_size, layer_norm=False)
    # return decoder(graph.node_features)

    """Encodes node and edge features into latent features."""

    def __init__(self, make_mlp, output_size):
        super().__init__()
        self.model = make_mlp(output_size)

    def forward(self, graph):
        return self.model(graph.node_features)


class Processor(nn.Module):
    def __init__(self, make_mlp, output_size, message_passing_steps, message_passing_aggregator):
        super().__init__()
        self._submodules_ordered_dict = OrderedDict()
        for index in range(message_passing_steps):
            self._submodules_ordered_dict[str(index)] = GraphNetBlock(model_fn=make_mlp, output_size=output_size, message_passing_aggregator=message_passing_aggregator)
        self.submodules = nn.Sequential(self._submodules_ordered_dict)

    def forward(self, graph):
        return self.submodules(graph)


class GraphStructureWatcher(nn.Module):
    '''
    This class takes the nodes with the most influential feature (sum of square) and
    multiply them with output node features of processor
    The result then will be feed forward to decoder

    Option: choose whether to normalize the output node features of processor
    '''

    def __init__(self, make_mlp, output_size, normalize_high_rank_nodes):
        super().__init__()
        self.reduce_model = make_mlp(output_size)
        self.expand_model = make_mlp(output_size)
        if normalize_high_rank_nodes:
            self.expand_model = nn.Sequential(nn.LayerNorm(normalized_shape=3), self.expand_model)

    def forward(self, latent_graph, graph):
        # get high_rank_percentage percent of the graph node features with the highest velocity
        high_rank_percentage = 0.01
        velocity_matrix = graph.node_features[:, 0:3]
        sort_by_velocity = torch.square(velocity_matrix)
        sort_by_velocity = torch.sum(sort_by_velocity, dim=-1)
        _, sort_indices = torch.sort(sort_by_velocity, dim=0, descending=True)

        # exclude the nodes outside high_rank_percentage from softmax
        high_rank_number = ceil(sort_indices.shape[0] * high_rank_percentage)
        sort_indices = sort_indices[0:high_rank_number]
        high_rank_tensor = torch.zeros_like(velocity_matrix)
        high_rank_tensor[sort_indices] = velocity_matrix[sort_indices]
        high_rank_tensor = self.expand_model(high_rank_tensor)
        high_rank_tensor = F.softmax(high_rank_tensor)

        node_features = latent_graph.node_features
        node_features = torch.transpose(node_features, 0, 1)
        node_result = torch.matmul(high_rank_tensor, node_features)
        node_result = torch.transpose(node_result, 0, 1)
        node_result = self.reduce_model(node_result)
        latent_graph = latent_graph._replace(node_features=node_result)
        return latent_graph


class EncodeProcessDecode(nn.Module):
    """Encode-Process-Decode GraphNet model."""

    def __init__(self,
                 output_size,
                 latent_size,
                 num_layers,
                 message_passing_steps, message_passing_aggregator):
        super().__init__()
        self._latent_size = latent_size
        self._output_size = output_size
        self._num_layers = num_layers
        self._message_passing_steps = message_passing_steps
        self._message_passing_aggregator = message_passing_aggregator
        self.encoder = Encoder(make_mlp=self._make_mlp, latent_size=self._latent_size)
        self.processor = Processor(make_mlp=self._make_mlp, output_size=self._latent_size,
                                   message_passing_steps=self._message_passing_steps,
                                   message_passing_aggregator=self._message_passing_aggregator)
        self.graph_structure_watcher = GraphStructureWatcher(make_mlp=self._make_mlp, output_size=self._latent_size,
                                                             normalize_high_rank_nodes=True)
        self.decoder = Decoder(make_mlp=functools.partial(self._make_mlp, layer_norm=False),
                               output_size=self._output_size)

    def _make_mlp(self, output_size, layer_norm=True):
        """Builds an MLP."""
        widths = [self._latent_size] * self._num_layers + [output_size]
        network = LazyMLP(widths)
        if layer_norm:
            network = nn.Sequential(network, nn.LayerNorm(normalized_shape=widths[-1]))
        return network

    def forward(self, graph):
        """Encodes and processes a multigraph, and returns node features."""
        latent_graph = self.encoder(graph)
        latent_graph = self.processor(latent_graph)
        latent_graph = self.graph_structure_watcher(latent_graph, graph)
        return self.decoder(latent_graph)
