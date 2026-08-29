def handler(request, *args, **kwargs):
    return {"statusCode": 200, "body": "hello from python"}

def app(request, *args, **kwargs):
    return handler(request, *args, **kwargs)
