"""Explicit owner/operator migration tooling; never a member RPC or runtime auto-import."""
import json
from pathlib import Path
from .common import dumps,now,number,text,body
from .contracts import NUTRIENTS

def load_food_seed(connection,path):
    seed=json.loads(Path(path).read_text(encoding="utf-8"))
    if seed.get("schema_version")!=1 or type(seed.get("items")) is not list: raise ValueError("unsupported_food_seed")
    for item in seed["items"]:
        food_id=text(item["id"],100); name=text(item["name"],200); category=text(item.get("category","food"),100)
        raw=body(item["nutrients"],NUTRIENTS)
        nutrients={key:number(raw.get(key),0,100000,True) for key in NUTRIENTS}
        source=item.get("source",seed.get("source"))
        if type(source) is not dict or not source.get("name") or not source.get("url"): raise ValueError("food_provenance_required")
        data={"nutrients":nutrients,"serving_grams":number(item.get("serving_grams"),0.1,20000,True),"source":source}
        for key in ("precision_notice","food_form","portion_description","approximate"):
            if key in item: data[key]=item[key]
        stamp=now()
        connection.execute("INSERT INTO expansion_foods(id,owner_user_id,name,category,payload_json,created_at,updated_at) VALUES (?,NULL,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,category=excluded.category,payload_json=excluded.payload_json,updated_at=excluded.updated_at",(food_id,name,category,dumps(data),stamp,stamp))
    return len(seed["items"])

def load_bundled_food_seeds(connection,root):
    return sum(load_food_seed(connection,Path(root)/filename) for filename in ('foods.json','gi-foods.json'))
