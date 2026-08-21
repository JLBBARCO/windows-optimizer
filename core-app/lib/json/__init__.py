import json

def read_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = f.read().strip()
        dictionary = json.loads(data) if data else {}
        return dictionary


def read_external_json (file):
    url_path = f'https://raw.githubusercontent.com/JLBBARCO/windows-optimizer/main/core-app/json/{file}.json'
    try:
        import requests
        response = requests.get(url_path, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception as fallback_error:
        return None