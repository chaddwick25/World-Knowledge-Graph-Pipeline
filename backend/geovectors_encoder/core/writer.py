import threading
import gzip
import queue
import gzip

"""
This class provides an asynchronous writer that runs in its own thread.
"""


class AsyncWrite(threading.Thread):

    def __init__(self, fname, compressed=False, encoder=None, db_callback=None):
        threading.Thread.__init__(self)
        self.q = queue.Queue()
        self.done = False
        self.fname = fname
        self.compressed = compressed
        self.encoder = encoder
        self.db_callback = db_callback

    def add_line(self, line):
        self.q.put(line)

    def qizes(self):
        return self.q.qsize()

    def open_file(self):
        if self.compressed:
            return gzip.open(self.fname, 'wb')
        else:
            return open(self.fname, 'w', encoding='utf-8')

    def transform_line(self, l):
        if self.encoder is not None:
            enc = self.encoder.encode_instance(l)
            if enc is None:
                return None
            l = [l[1], l[0]] + list(enc)
        return "\t".join(map(str, l))+"\n"

    def run(self):
        if self.compressed:
            file_encoding = (lambda x: x.encode("utf-8"))
        else:
            file_encoding = (lambda x: x)

        with self.open_file() as fo:
            while not self.done or not self.q.empty():
                while not self.q.empty():
                    l = self.q.get()
                    
                    # Capture raw data
                    raw_data = l # [id, type, tags, lat, lon] or similar
                    
                    # Encode and transform
                    vector = None
                    if self.encoder is not None:
                        vector = self.encoder.encode_instance(l)
                        if vector is None:
                            continue
                        l_transformed = [l[1], l[0]] + list(vector)
                    else:
                        l_transformed = l
                        
                    line_str = "\t".join(map(str, l_transformed)) + "\n"
                        
                    # Write to file
                    fo.write(file_encoding(line_str))
                    
                    # Execute DB callback if provided
                    if self.db_callback:
                        self.db_callback(raw_data, vector)

    def set_done(self):
        self.done = True
