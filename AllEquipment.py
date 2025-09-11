#Pulls Samsara Equip. Data
import os
import requests

url = "https://api.samsara.com/fleet/equipment"

# Load token from environment variable
api_token = os.getenv("SAMSARA_API_TOKEN")

headers = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_token}"
}

response = requests.get(url, headers=headers)
print(response.text)
