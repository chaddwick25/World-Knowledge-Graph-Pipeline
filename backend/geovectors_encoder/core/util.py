import numpy as np
from tqdm import tqdm
from multiprocessing import Process
import osmium
import setproctitle
from argparse import Namespace

"""
This file contains multiple utility functions.
"""
def read_samples(path):
    names = ['country', 'id', 'type', 'tags', 'lat', 'lon']
    dtypes = { 'country': 'object', 'id': 'int64',
               'type': 'object', 'tags': 'object', 'lat': 'float64', 'lon': 'float64'}
    data = pd.read_csv(path, sep='\t', names=names, dtype=dtypes)

    # drop duplicates from overlapping datasets
    data = data.sort_values(["id", "type", "lat", "lon"], na_position='last')
    data = data.drop_duplicates(subset=['id', 'type'], keep='first')
    return data

def read_db_config(path):
    result = Namespace()
    with open(path, 'r', encoding='utf-8') as fi:
        for l in fi:
            k,v = l.split("=")
            k = k.strip()
            v = v.strip()
            setattr(result, k, v)
    return result


def add_to_dict(dict, key, value):
    if key not in dict:
        dict[key] = set()
    if value not in dict[key]:
        dict[key].add(value)


def add_node_to_geom_dicts(n, relation_ids, target_dict):
    if n.location.valid():
        lat = n.location.lat
        lon = n.location.lon
        for t in relation_ids:
            add_to_dict(target_dict, t, (lat, lon))


def run_on_snapshots(f, args, targets, njobs):
    running_tasks = []
    open_tasks = []

    for t in targets:
        p = Process(target=f, args=args[t])
        open_tasks.append(p)

    pbar = tqdm(total=len(targets))

    while len(running_tasks) < njobs and len(open_tasks) > 0:
        p = open_tasks.pop()
        p.start()
        running_tasks.append(p)

    while True:
        for p in running_tasks:
            p.join(timeout=0)
            if not p.is_alive():
                running_tasks.remove(p)
                pbar.update(1)
                if len(open_tasks) > 0:
                    new_p = open_tasks.pop()
                    new_p.start()
                    running_tasks.append(new_p)

        if len(running_tasks) == 0:
            break

    pbar.close()


class SampleHandler(osmium.SimpleHandler):
    def __init__(self,
                 target_nodes,
                 target_ways,
                 target_relations,
                 entire_dump=False,
                 writer=None,
                 chunk_size=20000,
                 max_dict_size=100000):
        osmium.SimpleHandler.__init__(self)
        self.target_nodes = target_nodes
        self.target_ways = target_ways
        self.target_relations = target_relations
        self.sampled_nodes = []
        self.sampled_ways = {}
        self.way_nodes = {}
        self.sampled_relations = {}
        self.node_index = 0
        self.way_index = 0
        self.relation_index = 0
        self.relation_ways = {}
        self.relation_nodes = {}
        self.relation_relations = {}
        self.entire_dump = entire_dump
        self.writer = writer
        self.chunk_size = chunk_size
        self.node_count = 0
        self.max_dict_size = max_dict_size

    def node(self, n):
        self.node_index += 1
        if (self.entire_dump or self.node_index in self.target_nodes) and len(n.tags) > 0:
            tags = [(t.k, t.v) for t in n.tags]
            lat = n.location.lat
            lon = n.location.lon
            record = [n.id, 'node', tags, lat, lon]
            if self.writer is not None:
                self.writer.add_line(record)
                self.node_count += 1
                # Flush periodically to prevent memory buildup
                if self.node_count % self.chunk_size == 0:
                    if hasattr(self.writer.storage, 'flush'):
                        self.writer.storage.flush()
                        self.node_count = 0
            else:
                self.sampled_nodes.append(record)

    def way(self, w):
        self.way_index += 1
        if (self.entire_dump or self.way_index in self.target_ways) and len(w.tags) > 0:
            tags = [(t.k, t.v) for t in w.tags]
            record = [w.id, 'way', tags]
            self.sampled_ways[w.id] = record

            for n in w.nodes:
                add_to_dict(self.way_nodes, n.ref, w.id)
                # Prevent unbounded growth of way_nodes dict
                if len(self.way_nodes) > self.max_dict_size:
                    self.way_nodes = dict(list(self.way_nodes.items())[-self.max_dict_size//2:])

        if (self.entire_dump or self.way_index in self.target_ways):
            for n in w.nodes:
                # We need to track which ways need node coordinates
                # This is just a placeholder to ensure the key exists
                if w.id not in self.relation_ways:
                    self.relation_ways[w.id] = set()
                # Prevent unbounded growth
                if len(self.relation_ways) > self.max_dict_size:
                    self.relation_ways = dict(list(self.relation_ways.items())[-self.max_dict_size//2:])

    def relation(self, r):
        self.relation_index += 1
        if (self.entire_dump or self.relation_index in self.target_relations) and len(r.tags) > 0:
            tags = [(t.k, t.v) for t in r.tags]
            record = [r.id, 'relation', tags]
            self.sampled_relations[r.id] = record

            for m in r.members:
                if m.type == 'n':
                    add_to_dict(self.relation_nodes, m.ref, r.id)
                    if len(self.relation_nodes) > self.max_dict_size:
                        self.relation_nodes = dict(list(self.relation_nodes.items())[-self.max_dict_size//2:])
                if m.type == 'w':
                    add_to_dict(self.relation_ways, m.ref, r.id)
                    if len(self.relation_ways) > self.max_dict_size:
                        self.relation_ways = dict(list(self.relation_ways.items())[-self.max_dict_size//2:])
                if m.type == 'r':
                    add_to_dict(self.relation_relations, m.ref, r.id)
                    if len(self.relation_relations) > self.max_dict_size:
                        self.relation_relations = dict(list(self.relation_relations.items())[-self.max_dict_size//2:])

            self.sampled_relations[r.id] = record
        self.relation_index += 1


def check_node(n, relation_ids, target_dict):
    if n.location.valid():
        lat = n.location.lat
        lon = n.location.lon
        for t in relation_ids:
            add_to_dict(target_dict, t, (lat, lon))


class DependencyGeomHandler(osmium.SimpleHandler):
    def __init__(self,
                 way_coords,
                 relation_coords,
                 way_nodes,
                 relation_nodes,
                 relation_ways,
                 relation_relations):
        osmium.SimpleHandler.__init__(self)
        # Take ownership of the dicts directly — no deepcopy.
        # The handler consumes keys (deletes resolved entries) and may add
        # new keys (when a way/relation member is discovered).  The caller
        # snapshots the original key sets before each pass so it can
        # distinguish old-unresolved keys from newly-discovered keys
        # after the PBF read, without doubling memory via deepcopy.
        self.way_nodes = way_nodes
        self.relation_nodes = relation_nodes
        self.relation_ways = relation_ways
        self.relation_relations = relation_relations
        self.way_coords = way_coords
        self.relation_coords = relation_coords

    def node(self, n):
        if n.id in self.way_nodes:
            check_node(n, self.way_nodes[n.id], self.way_coords)

        if n.id in self.relation_nodes:
            check_node(n, self.relation_nodes[n.id], self.relation_coords)
            del self.relation_nodes[n.id]

    def way(self, w):
        if w.id in self.relation_ways:
            for n in w.nodes:
                for r in self.relation_ways[w.id]:
                    add_to_dict(self.relation_nodes, n.ref, r)

            del self.relation_ways[w.id]

    def relation(self, r):
        if r.id in self.relation_relations:
            for m in r.members:
                for r_parent in self.relation_relations[r.id]:
                    if m.type == 'n':
                        add_to_dict(self.relation_nodes, m.ref, r_parent)

                    if m.type == 'w':
                        add_to_dict(self.relation_ways, m.ref, r_parent)

                    if m.type == 'r':
                        add_to_dict(self.relation_relations, m.ref, r_parent)

            del self.relation_relations[r.id]


def coords_to_center(coords):
    if len(coords) == 0:
        center = [float('nan'), float('nan')]
    else:
        center = np.mean(coords, axis=0)
    return center


def concat_data(sample_dict, coord_dict):
    result = []
    for id in sample_dict:
        record = sample_dict[id]

        if id in coord_dict:
            coords = coord_dict[id]
        else:
            coords = []

        center = coords_to_center(list(coords))

        record.append(center[0])
        record.append(center[1])

        result.append(tuple(record))
    return result


def read_from_snapshot(path, targets=None, writer=None, max_runs=float('inf'), chunk_size=20000):
    """
    Streaming version that processes OSM entities in chunks to prevent OOM.
    Similar to WorldKG StreamingOsmExtractor pattern.
    """
    if targets is not None:
        target_nodes, target_ways, target_relations = targets
        sample_handler = SampleHandler(target_nodes, target_ways, target_relations, chunk_size=chunk_size)
    else:
        sample_handler = SampleHandler([], [], [], entire_dump=True, writer=writer, chunk_size=chunk_size)

    setproctitle.setproctitle("Sample handler"+path)

    # Phase 1: Process nodes and collect way/relation dependencies
    sample_handler.apply_file(path)
    
    # If writer is provided, flush nodes immediately
    if writer is not None and hasattr(writer.storage, 'flush'):
        writer.storage.flush()
    
    # resolve dependencies
    way_nodes = sample_handler.way_nodes
    relation_nodes = sample_handler.relation_nodes
    relation_ways = sample_handler.relation_ways
    relation_relations = sample_handler.relation_relations

    n_data = sample_handler.sampled_nodes
    w_data = sample_handler.sampled_ways
    r_data = sample_handler.sampled_relations
    way_coords = {}
    relation_coords = {}

    current_run = 0
    while (len(way_nodes) > 0 or len(relation_nodes) > 0 or len(relation_ways) > 0 or len(relation_relations)) \
            and current_run < max_runs:
        # Snapshot original key sets so we can distinguish old-unresolved
        # keys from newly-discovered keys after the PBF pass — replaces the
        # former deepcopy() which doubled memory for no correctness benefit.
        # A set of int keys is orders of magnitude smaller than a full copy
        # of {int: set(int)} dicts.
        old_relation_nodes_keys = set(relation_nodes.keys())
        old_relation_ways_keys = set(relation_ways.keys())
        old_relation_relations_keys = set(relation_relations.keys())

        dep_handler = DependencyGeomHandler(way_coords,
                                            relation_coords,
                                            way_nodes,
                                            relation_nodes,
                                            relation_ways,
                                            relation_relations)
        setproctitle.setproctitle("Dependency handler pass"+str(current_run) + " " + path)

        dep_handler.apply_file(path)
        # way_nodes is read-only in the handler (never added to), so all
        # remaining keys are unresolved — drop them.
        way_nodes = {}
        # Keep only NEW keys (discovered during this pass via way/relation
        # member expansion).  Old keys that weren't consumed are unresolved
        # and dropped — same behavior as the former remove_old_key().
        relation_nodes = {k: v for k, v in dep_handler.relation_nodes.items()
                          if k not in old_relation_nodes_keys}
        relation_ways = {k: v for k, v in dep_handler.relation_ways.items()
                         if k not in old_relation_ways_keys}
        relation_relations = {k: v for k, v in dep_handler.relation_relations.items()
                              if k not in old_relation_relations_keys}
        current_run += 1

    w_data = concat_data(w_data, way_coords)
    del way_coords

    r_data = concat_data(r_data, relation_coords)
    del relation_coords

    del sample_handler
    del dep_handler

    return n_data, w_data, r_data
