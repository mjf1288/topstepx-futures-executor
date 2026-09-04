"""Query ProjectX for active MGC + MCL contracts so we know the correct IDs."""
import os
from dotenv import load_dotenv
load_dotenv()
from topstep_api import from_env
import requests

api = from_env()
headers = {'Authorization': f'Bearer {api.get_session_token()}', 'Content-Type': 'application/json'}

for root, exchange in [("MGC", "COMEX"), ("MCL", "NYMEX")]:
    print(f"\n═══ {root} contracts ═══")
    r = requests.post(
        f"{api.base_url}/Contract/search",
        json={"searchText": root, "live": False},
        headers=headers,
        timeout=15,
    )
    for c in r.json().get("contracts", [])[:10]:
        print(f"  {c.get('id')}  name={c.get('name')}  active={c.get('activeContract')}  exp={c.get('lastTradingDate')}")
