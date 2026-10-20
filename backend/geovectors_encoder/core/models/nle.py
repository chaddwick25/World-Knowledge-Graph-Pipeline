import pickle
import numpy as np
import ntpath

from os.path import join, abspath, exists
from os import makedirs
from math import isnan
from .base import BaseModel

"""
This is class represents the neural location embedding model for OpenStreetMap entities.
"""


class WDWStore(dict):
    """
    A dictionary-based store for Weighted DeepWalk (WDW) embeddings.
    Includes metadata like n_components to satisfy NLEModel expectations.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_components = 100

def get_key_x_y(r):
    key = r['type'] + "_" + str(r['id'])
    x = r['lat']
    y = r['lon']
    return key, x, y


def get_dump_key_x_y(r):
    key = r[1]+"_"+str(r[0])
    x = r[3]
    y = r[4]
    return key, x, y


class NLEModel(BaseModel):

    def __init__(self, index_path, njobs,  db):
        BaseModel.__init__(self)
        self.index_path = abspath(index_path)
        if not exists(self.index_path):
            makedirs(self.index_path)

        self.njobs = njobs
        self.idx = None
        self.wdw = None
        self.db = db
        self.table_name = "geovectors_nle"

    def encode_pandas_instance(self, instance):
        key, x, y = get_key_x_y(instance[1])
        return self.encode_coords(key, x,y)

    def encode_instance(self, instance):
        key, x, y = get_dump_key_x_y(instance)
        return self.encode_coords(key, x, y)

    def encode_coords(self, key, x,y):
        if isnan(x) or isnan(y):
            return None
        else:
            vectors = []
            nearest_neighbors = self._get_nn(x, y, 50)

            dist_sum = 0
            for n in nearest_neighbors:
                #other_key = n.object
                other_key = n[0]

                if key == other_key:
                    continue

                #use distance in kilometers
                dist = np.log(1 + (1 / (n[1] / 1000)))
                dist_sum += dist

                node_enc = self.wdw.predict(other_key)
                vectors.append(node_enc * dist)

            if not vectors:
                return None
            enc = np.sum(vectors, axis=0) / dist_sum
            return enc
    def _get_nn(self, x, y, n):
        result = []
        point = "public.st_setsrid(public.st_makepoint("+str(x)+", "+str(y)+"), 4326)"
        nn_query =  "select osm_key, " +\
                    " public.st_distance(location::public.geography, " +\
                    point+"::public.geography) " +\
                    " from "+self._table_name()+" " +\
                    " where public.st_distance(location::public.geography, "+point+"::public.geography) > 0 " +\
                    " order by location OPERATOR(public.<->) "+point+" " +\
                    " limit "+str(n)+"; "

        try:
            conn = self.db.get_pool_connection()
            with conn.cursor() as cur:
                cur.execute(nn_query)
                rows = cur.fetchall()
                for r in rows:
                    result.append((r[0], r[1]))

            self.db.free_pool_connection(conn)
        except:
            print("Query")
            print(x, y,)
            print(nn_query)
            print("_____________")
            raise

        return result

    def _table_name(self):
        return ntpath.split(self.table_name)[1].replace(".", "_")

    def _wdw_path(self):
        return join(self.index_path, "wdw.pickle")

    def load_indexes(self):
        self.db.create_connection_pool()

        with open(self._wdw_path(), 'rb') as fi:
            self.wdw = pickle.load(fi)

        # Robust dimension setting: check for n_components attribute or fallback to 100
        n_comp = getattr(self.wdw, 'n_components', 100)
        super()._set_dimensions(n_comp)

    def save_model(self, path):

        with open(self._wdw_path(), 'wb') as fo:
            pickle.dump(self.wdw, fo)

    def destroy(self):
        self.db.close_connection_pool()
