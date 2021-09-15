from numpy.core.fromnumeric import reshape
import torch

from torch.utils.data import Dataset, DataLoader
from tfrecord.torch.dataset import TFRecordDataset
import os
from os import path
import json
import numpy as np


from common import NodeType

device = torch.device('cuda')

class FlagSimpleDataset(Dataset):
    def __init__(self, path, split, add_targets=False, split_and_preprocess=False):
        self.path = path
        self.split = split
        self._add_targets = add_targets
        self._split_and_preprocess = split_and_preprocess
        '''
        self.add_targets = add_targets
        self.split_and_preprocess = split_and_preprocess
        '''
        try:
            with open(os.path.join(path, 'meta.json'), 'r') as fp:
                self.meta = json.loads(fp.read())
            self.shapes = {}
            self.dtypes = {}
            self.types = {}
            for key, field in self.meta['features'].items():
                self.shapes[key] = field['shape']
                self.dtypes[key] = field['dtype']
                self.types[key] = field['type']
        except FileNotFoundError as e:
            print(e)
            quit()
        tfrecord_path = path + split + ".tfrecord"
        # index is generated by tfrecord2idx
        index_path = path + split + ".idx"
        tf_dataset = TFRecordDataset(tfrecord_path, index_path, None)
        # loader and iter(loader) have size 1000, which is the number of all training trajectories
        loader = torch.utils.data.DataLoader(tf_dataset, batch_size=1)
        # use list to make list from iterable so that the order of elements is ensured
        self.dataset = list(iter(loader))

    def __len__(self):
        # flag simple dataset contains 1000 trajectories, each trajectory contains 400 steps
        if self.split == 'train':
            return 1000
        elif self.split == 'valid':
            return 100

    def __getitem__(self, idx):
        sample = self.dataset[idx]
        trajectory = {}
        # decode bytes into corresponding dtypes
        for key, value in sample.items():
            raw_data = value.numpy().tobytes()
            mature_data = np.frombuffer(raw_data, dtype=getattr(np, self.dtypes[key]))
            mature_data = torch.from_numpy(mature_data).to(device)
            reshaped_data = torch.reshape(mature_data, self.shapes[key])
            if self.types[key] == 'static':
                reshaped_data = torch.tile(reshaped_data, (self.meta['trajectory_length'], 1, 1))
            elif self.types[key] == 'dynamic_varlen':
               pass
            elif self.types[key] != 'dynamic':
                raise ValueError('invalid data format')
            trajectory[key] = reshaped_data

        '''
        if self.add_targets is not None:
            trajectory = self.add_targets(trajectory)
        if self.split_and_preprocess is not None:
            trajectory = self.split_and_preprocess(trajectory)
        '''
        if self._add_targets:
            trajectory = self.add_targets()(trajectory)
        if self._split_and_preprocess:
            trajectory = self.split_and_preprocess()(trajectory)
        
        # print("trajectory type in flag_dataset", type(trajectory))
        return trajectory

    def add_targets(self):
        """Adds target and optionally history fields to dataframe."""
        fields = 'world_pos'
        add_history = True
        def fn(trajectory):
            out = {}
            for key, val in trajectory.items():
                out[key] = val[1:-1]
                if key in fields:
                    if add_history:
                        out['prev|' + key] = val[0:-2]
                    out['target|' + key] = val[2:]
            return out

        return fn

    def split_and_preprocess(self):
        """Splits trajectories into frames, and adds training noise."""
        noise_field = 'world_pos'
        noise_scale = 0.003
        noise_gamma = 0.1
        def add_noise(frame):
            zero_size = torch.zeros(frame[noise_field].size(), dtype=torch.float32).to(device)
            noise = torch.normal(zero_size, std=noise_scale).to(device)
            other = torch.Tensor([NodeType.NORMAL.value]).to(device)
            mask = torch.eq(frame['node_type'], other.int())[:, 0]
            mask = torch.stack((mask, mask, mask), dim=1)
            noise = torch.where(mask, noise, torch.zeros_like(noise))
            frame[noise_field] += noise
            frame['target|' + noise_field] += (1.0 - noise_gamma) * noise
            return frame

        def element_operation(trajectory):
            world_pos = trajectory['world_pos']
            mesh_pos = trajectory['mesh_pos']
            node_type = trajectory['node_type']
            cells = trajectory['cells']
            target_world_pos = trajectory['target|world_pos']
            prev_world_pos = trajectory['prev|world_pos']
            trajectory_steps = []
            for i in range(399):
                wp = world_pos[i]
                mp = mesh_pos[i]
                twp = target_world_pos[i]
                nt = node_type[i]
                c = cells[i]
                pwp = prev_world_pos[i]
                trajectory_step = {'world_pos': wp, 'mesh_pos': mp, 'node_type': nt, 'cells': c,
                                   'target|world_pos': twp, 'prev|world_pos': pwp}
                noisy_trajectory_step = add_noise(trajectory_step)
                trajectory_steps.append(noisy_trajectory_step)
            return trajectory_steps
        return element_operation

# code to check whether custom dataset work as expected

# add timer to measure execution time
'''
import time
start_time = time.time()
'''
'''
num_workers = 1
batch_size = 2
flag_simple_dataset = DataLoader(FlagSimpleDataset(path='../../../mgn_dataset/flag_simple/', split='train'), batch_size=batch_size, num_workers=num_workers)
print('flag_simple_dataset size is ' + str(sum(1 for e in flag_simple_dataset)))
for example1_key, example1_value in next(iter(flag_simple_dataset)).items():
    print("example1_key is: ", example1_key)
    print("example1_value size is: ", example1_value.size())
'''
    # print("example1_key size is: ", example1_key.size())
    # print("example1_value is: ", example1_value)
'''
    for example2 in example1:
        print("example2 is: ", example2)
        for key, value in example2.items():
            print(str(key) + ": " + str(value))
            print()
'''
'''
for key, value in next(iter(flag_simple_dataset)).items():
    print(str(key) + ": " + str(value))
    print()
'''
'''

# print("Execution time for flag simple dataset: ", time.time() - start_time)
'''
