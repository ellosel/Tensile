import yaml
import json


from os import listdir
from os.path import isfile, join, splitext

onlyfiles = [f for f in listdir("yaml") if isfile(join("yaml", f))]

for f in onlyfiles:
  with open("yaml/" + f, 'r') as yaml_in, open("json/" + splitext(f)[0] + ".json", "w") as json_out:
    yaml_object = yaml.safe_load(yaml_in)
    json.dump(yaml_object, json_out, indent=2, sort_keys=True)