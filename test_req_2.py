import urllib.request, urllib.error, json
url = "http://localhost:8000/api/save_script"
data = json.dumps({"cid": "thestick", "vid": "why_you_will_never_get_rich_an", "text": "This is a test script"}).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as response:
        print(response.read().decode())
except urllib.error.HTTPError as e:
    print(e.code)
    print(e.read().decode())
