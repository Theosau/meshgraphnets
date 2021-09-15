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
"""Model for FlagSimple."""

import torch
from torch import nn as nn
import torch.nn.functional as F

import common
import normalization
import encode_process_decode

device = torch.device('cuda')

class Model(nn.Module):
  """Model for static cloth simulation."""
  def __init__(self, params, model, output_normalizer):
    super(Model, self).__init__()
    self._params = params
    # self._output_normalizer = normalization.Normalizer(
    #    size=3, name='output_normalizer')
    self._output_normalizer = output_normalizer
    self._node_normalizer = normalization.Normalizer(
        size=3+common.NodeType.SIZE, name='node_normalizer')
    self._edge_normalizer = normalization.Normalizer(
        size=7, name='edge_normalizer')  # 2D coord + 3D coord + 2*length = 7

    self.learned_model = model

  def _build_graph(self, inputs, is_training):
    """Builds input graph."""
    world_pos = torch.squeeze(inputs['world_pos'], dim=0)
    prev_world_pos = torch.squeeze(inputs['prev|world_pos'], dim=0)
    node_type = torch.squeeze(inputs['node_type'], dim=0)
    velocity = world_pos - prev_world_pos
    node_type = F.one_hot(node_type[:, 0].to(torch.int64), common.NodeType.SIZE)

    node_features = torch.cat((velocity, node_type), dim=-1)

    cells = torch.squeeze(inputs['cells'], dim=0)
    senders, receivers = common.triangles_to_edges(cells)

    mesh_pos = torch.squeeze(inputs['mesh_pos'], dim=0)
    relative_world_pos = (torch.index_select(input=world_pos, dim=0, index=senders) -
                          torch.index_select(input=world_pos, dim=0, index=receivers))
    relative_mesh_pos = (torch.index_select(mesh_pos, 0, senders) -
                         torch.index_select(mesh_pos, 0, receivers))
    edge_features = torch.cat((
        relative_world_pos,
        torch.norm(relative_world_pos, dim=-1, keepdim=True),
        relative_mesh_pos,
        torch.norm(relative_mesh_pos, dim=-1, keepdim=True)), dim=-1)

    mesh_edges = encode_process_decode.EdgeSet(
        name='mesh_edges',
        # features=self._edge_normalizer(edge_features, is_training),
        features=edge_features,
        receivers=receivers,
        senders=senders)
    return encode_process_decode.MultiGraph(
        # node_features=self._node_normalizer(node_features, is_training),
        node_features=node_features,
        edge_sets=[mesh_edges])

  def forward(self, inputs, is_training):
    graph = self._build_graph(inputs, is_training=is_training)
    if is_training:
        return self.learned_model(graph)
    else:
        return self._update(inputs, self.learned_model(graph))


  def _update(self, inputs, per_node_network_output):
    """Integrate model outputs."""
    # return per_node_network_output

    # acceleration = self._output_normalizer.inverse(per_node_network_output)
    acceleration = per_node_network_output
    # integrate forward
    cur_position = torch.squeeze(inputs['world_pos'], dim=0)
    prev_position = torch.squeeze(inputs['prev|world_pos'], dim=0)
    position = 2 * cur_position + acceleration - prev_position
    # print(position)
    return position
