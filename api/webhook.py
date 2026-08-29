import json

def handler(request, *args, **kwargs):
    try:
        return {"statusCode": 200, "body": "OK - minimal"}
    except Exception:
        return {"statusCode": 200, "body": "OK"}

def app(request, *args, **kwargs):
    try:
        return handler(request, *args, **kwargs)
    except Exception:
        return {"statusCode": 200, "body": "OK"}
