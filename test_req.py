import urllib.request, json
url = "http://localhost:8000/api/save_script"
data = json.dumps({"cid": "thestick", "vid": "why_you_will_never_get_rich_an", "text": "This is a test script"}).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as response:
    print(response.read().decode())
