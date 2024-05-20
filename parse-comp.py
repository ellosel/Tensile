import simdjson, yaml, time
from os import listdir
from os.path import isfile, join, splitext

class MyLoader(yaml.SafeLoader):
    def construct_mapping(self, *args, **kwargs):
        mapping = super().construct_mapping(*args, **kwargs)

        for key in list(mapping.keys()):
            # bool is a subclass of int
            #if not isinstance(key, bool) and isinstance(key, (int, float)):
            #    mapping[str(key)] = mapping.pop(key)
             if key.isdigit():
                 mapping[int(key)] = mapping.pop(key)

        return mapping

yamlfiles = sorted([f for f in listdir("yaml") if isfile(join("yaml", f))])
jsonfiles = sorted([f for f in listdir("json") if isfile(join("json", f))])

#parser = simdjson.Parser()

""" start = time.time()
for filename in jsonfiles[0:1]:
  print(filename)
  with open("json/" + filename, "r") as f:
    simdjson.load(f)
stop = time.time()
print("json time: ", stop-start)

start = time.time()
for filename in yamlfiles[0:1]:
  print(filename)  
  with open("yaml/" + filename, "r") as f:
    yaml.load(f, yaml.BaseLoader)
stop = time.time()
print("yaml time: ", stop-start) """


def int_keys_decoder(pairs):
    return {int(k): v for k, v in pairs}

for j, y in zip(jsonfiles[0:1], yamlfiles[0:1]):
  with open("json/" + j, "r") as f1, open("yaml/" + y, "r") as f2:
    d1 = simdjson.load(f1)
    d2 = yaml.load(f2, Loader=yaml.SafeLoader)
        
    if len(d1) != len(d2):
      print("Bad sizes: ", j, y)
    
    for i, l, r in zip(range(0,len(d1)), d1, d2):
      for j, k in zip(l, r):
        print(j)
        print(k, "\n")





